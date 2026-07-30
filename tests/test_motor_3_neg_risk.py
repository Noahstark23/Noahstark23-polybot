"""
Tests del Motor 3 (neg-risk multi-outcome) en SHADOW.

Cubren la math pura (ambas direcciones), el pipeline de filtros con el
anti-fantasma, el de-dupe anti-flood, el funnel con motor=motor_3, el
discovery de grupos (incluido el guard de grupos incompletos — el riesgo #1
de este universo) y la migración de funnel_snapshots.motor.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from src.db.engine import get_session
from src.db.models import FunnelSnapshot, MultiEdgeWindow
from src.marketdata import registry
from src.math.fees import multi_outcome_edge
from src.motor_3_neg_risk.engine import LegQuote, NegRiskEngine
from src.strategies.data_capture import DataCaptureService, WatchedMarket
from src.utils.config import get_settings

# =====================================================
# Math pura
# =====================================================


class TestMultiOutcomeEdge:
    def test_buy_yes_all_gross(self):
        # 3 outcomes: 0.30 + 0.30 + 0.30 = 0.90 -> paga 1.00
        e = multi_outcome_edge([0.30, 0.30, 0.30], "buy_yes_all", fee_rate_bps=0,
                               slippage_buffer=0.0)
        assert e.payout_per_set == 1.0
        assert e.gross_edge_pct == pytest.approx((0.10 / 0.90) * 100, abs=1e-3)
        assert e.net_edge_pct == e.gross_edge_pct  # sin fees ni slippage

    def test_buy_no_all_gross(self):
        # 3 outcomes: comprar NO a 0.60 c/u = 1.80 -> pagan N-1 = 2.00
        e = multi_outcome_edge([0.60, 0.60, 0.60], "buy_no_all", fee_rate_bps=0,
                               slippage_buffer=0.0)
        assert e.payout_per_set == 2.0
        assert e.gross_edge_pct == pytest.approx((0.20 / 1.80) * 100, abs=1e-3)

    def test_edge_es_pct_del_capital_comprometido(self):
        """La unidad: % del costo del set, NO unidades de precio x100.
        Sin normalizar, buy_no_all de N patas (costo ~N-1) inflaría el numero."""
        yes = multi_outcome_edge([0.45, 0.45], "buy_yes_all", 0, 0.0)
        no = multi_outcome_edge([0.55, 0.55], "buy_no_all", 0, 0.0)
        # mismo profit absoluto (0.10) pero capital distinto (0.90 vs 1.10)
        assert yes.gross_edge_pct > no.gross_edge_pct

    def test_fees_y_slippage_por_pata(self):
        """N patas = N oportunidades de slippage: el buffer escala con las legs."""
        e2 = multi_outcome_edge([0.45, 0.45], "buy_yes_all", 0, slippage_buffer=0.002)
        e4 = multi_outcome_edge([0.22, 0.22, 0.23, 0.23], "buy_yes_all", 0,
                                slippage_buffer=0.002)
        assert e4.fees_per_set == pytest.approx(0.008)
        assert e2.fees_per_set == pytest.approx(0.004)
        assert e2.costs_pct > 0

    def test_fee_clob_formula_oficial(self):
        # fee = bps/10000 * min(p, 1-p) por contrato, por pata
        e = multi_outcome_edge([0.30, 0.30, 0.30], "buy_yes_all", fee_rate_bps=100,
                               slippage_buffer=0.0)
        assert e.fees_per_set == pytest.approx(3 * 0.01 * 0.30)

    def test_direccion_invalida(self):
        with pytest.raises(ValueError):
            multi_outcome_edge([0.5, 0.5], "sell_all", 0)

    def test_menos_de_dos_patas(self):
        with pytest.raises(ValueError):
            multi_outcome_edge([0.5], "buy_yes_all", 0)


# =====================================================
# Pipeline del engine
# =====================================================


@pytest.fixture()
def engine(initialized_db):
    return NegRiskEngine(get_settings())


def _legs(asks_yes, asks_no=None, depth=50.0):
    asks_no = asks_no or [None] * len(asks_yes)
    return [
        LegQuote(
            condition_id=f"0xc{i}",
            ask_yes=ay,
            ask_no=an,
            depth_yes=depth,
            depth_no=depth,
        )
        for i, (ay, an) in enumerate(zip(asks_yes, asks_no, strict=True))
    ]


class TestPipeline:
    def test_pata_sin_libro_no_evalua(self, engine):
        """Un set se compra ENTERO: sumar solo las patas con libro fabrica edge."""
        ev = engine.evaluate_group("g1", _legs([0.30, 0.30, None]), "buy_yes_all")
        assert ev.status == "leg_no_book"
        assert ev.window is None

    def test_grupo_eficiente_below_min_edge(self, engine):
        ev = engine.evaluate_group("g1", _legs([0.34, 0.33, 0.33]), "buy_yes_all")
        assert ev.status == "below_min_edge"

    def test_edge_valido_shadow_recorded(self, engine):
        # 0.32+0.32+0.32 = 0.96 -> ~3.5% neto: pasa
        ev = engine.evaluate_group("g1", _legs([0.32, 0.32, 0.32]), "buy_yes_all")
        assert ev.status == "shadow_recorded"
        assert ev.window.risk_approved is True
        assert ev.window.theoretical_pnl_usd > 0
        assert ev.window.legs == 3

    def test_anti_fantasma_grupo_incompleto(self, engine):
        """El caso que importa: 2 patas de un evento de 5 suman 0.40 y 'rinden'
        150% — es un grupo incompleto, no plata (la 3ra red del discovery)."""
        ev = engine.evaluate_group("g1", _legs([0.20, 0.20]), "buy_yes_all")
        assert ev.status == "edge_too_high"
        assert ev.window.status == "edge_too_high"

    def test_liquidez_manda_la_pata_mas_fina(self, engine):
        legs = _legs([0.32, 0.32, 0.32])
        legs[1].depth_yes = 3.0  # < MIN_LIQUIDITY_CONTRACTS=10
        ev = engine.evaluate_group("g1", legs, "buy_yes_all")
        assert ev.status == "low_liquidity"

    def test_buy_no_all_tambien_evalua(self, engine):
        # NO a 0.63 c/u en 3 patas = 1.89 -> pagan 2.00 -> ~5.2% bruto
        ev = engine.evaluate_group(
            "g1", _legs([0.5, 0.5, 0.5], asks_no=[0.63, 0.63, 0.63]), "buy_no_all"
        )
        assert ev.status == "shadow_recorded"
        assert ev.window.payout_per_set == 2.0


# =====================================================
# Tick: funnel por motor + de-dupe
# =====================================================


def _watched(gid: str, n: int, cid_prefix: str) -> list[WatchedMarket]:
    return [
        WatchedMarket(
            condition_id=f"{cid_prefix}{i}",
            question=f"outcome {i}",
            token_id_yes=f"{cid_prefix}{i}y",
            token_id_no=f"{cid_prefix}{i}n",
            end_date_iso=None,
            neg_risk=True,
            tick_size=0.01,
            neg_risk_market_id=gid,
        )
        for i in range(n)
    ]


class _Book:
    def __init__(self, ask: float, size: float = 50.0):
        self.synced = True
        self.best_ask = type("L", (), {"price": ask, "size": size})()


class _FakeBooks:
    def __init__(self, asks: dict[str, float]):
        self._asks = asks

    def get_book(self, token_id: str):
        ask = self._asks.get(token_id)
        return _Book(ask) if ask is not None else None


@pytest.fixture()
def capture_with_group(initialized_db):
    svc = DataCaptureService(get_settings())
    markets = _watched("grp-1", 3, "0xa")
    svc.watched = {m.condition_id: m for m in markets}
    svc.books = _FakeBooks(
        {
            "0xa0y": 0.32, "0xa1y": 0.32, "0xa2y": 0.32,  # buy_yes_all: 3.5% neto
            "0xa0n": 0.70, "0xa1n": 0.70, "0xa2n": 0.70,  # buy_no_all: negativo
        }
    )
    registry.set_capture(svc)
    yield svc
    registry.set_capture(None)


class TestTick:
    def test_tick_graba_ventana_y_funnel_motor_3(self, engine, capture_with_group):
        summary = engine.tick()
        assert summary["groups_evaluated"] == 1
        assert summary["edges_recorded"] == 1  # solo buy_yes_all
        # asserts DENTRO de la sesión (los objetos expiran al commit)
        with get_session() as s:
            windows = list(s.exec(select(MultiEdgeWindow)))
            funnels = list(s.exec(select(FunnelSnapshot)))
            assert len(windows) == 1
            assert windows[0].direction == "buy_yes_all"
            assert windows[0].neg_risk_market_id == "grp-1"
            assert len(funnels) == 1
            assert funnels[0].motor == "motor_3"

    def test_dedupe_oportunidad_identica_no_reescribe(self, engine, capture_with_group):
        """Anti-flood (incidente Kalshi 13M filas/día): el MISMO arb tick tras
        tick es UNA fila, no una por tick."""
        engine.tick()
        summary2 = engine.tick()
        assert summary2["skips"].get("dedup_unchanged") == 1
        with get_session() as s:
            windows = list(s.exec(select(MultiEdgeWindow)))
        assert len(windows) == 1  # el funnel sí registra ambos ciclos

    def test_dedupe_libera_cuando_el_edge_cambia(self, engine, capture_with_group):
        engine.tick()
        capture_with_group.books._asks["0xa0y"] = 0.31  # se movió el libro
        engine.tick()
        with get_session() as s:
            windows = list(s.exec(select(MultiEdgeWindow)))
        assert len(windows) == 2

    def test_sin_capture_no_rompe(self, engine, initialized_db):
        registry.set_capture(None)
        summary = engine.tick()
        assert summary["skips"] == {"no_groups": 1}

    def test_grupo_chico_se_saltea(self, engine, initialized_db):
        svc = DataCaptureService(get_settings())
        markets = _watched("grp-2", 2, "0xb")  # < MOTOR_3_MIN_LEGS=3
        svc.watched = {m.condition_id: m for m in markets}
        svc.books = _FakeBooks({})
        registry.set_capture(svc)
        try:
            summary = engine.tick()
        finally:
            registry.set_capture(None)
        assert summary["groups_evaluated"] == 0
        assert summary["skips"].get("group_too_small") == 1


# =====================================================
# Discovery de grupos
# =====================================================


def _raw_market(cid: str, gid: str, active: bool = True) -> dict:
    return {
        "condition_id": cid,
        "question": f"q {cid}",
        "neg_risk": True,
        "neg_risk_market_id": gid,
        "active": active,
        "closed": False,
        "tokens": [
            {"outcome": "Yes", "token_id": f"{cid}y"},
            {"outcome": "No", "token_id": f"{cid}n"},
        ],
    }


class _FakeClob:
    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self._i = 0

    async def get_markets(self, cursor: str = ""):
        page = self._pages[self._i] if self._i < len(self._pages) else []
        self._i += 1
        done = self._i >= len(self._pages)
        return {"data": page, "next_cursor": "LTE=" if done else f"c{self._i}"}


class TestDiscoveryNegRisk:
    @pytest.mark.asyncio
    async def test_agrupa_y_descarta_grupos_chicos(self, isolated_env):
        isolated_env.setenv("MARKET_DISCOVERY_SOURCE", "neg_risk")
        import src.utils.config as config_module

        config_module.reset_settings_for_testing()
        svc = DataCaptureService(config_module.get_settings())
        pages = [[
            _raw_market("0x1", "gA"), _raw_market("0x2", "gA"), _raw_market("0x3", "gA"),
            _raw_market("0x4", "gB"), _raw_market("0x5", "gB"),  # 2 patas < min 3
        ]]
        markets = await svc._discover_neg_risk(_FakeClob(pages))
        assert {m.neg_risk_market_id for m in markets} == {"gA"}
        assert len(markets) == 3

    @pytest.mark.asyncio
    async def test_grupo_con_pata_inextraible_se_descarta_entero(self, isolated_env):
        """EL guard del edge fantasma: si una pata del grupo no se pudo extraer
        (cerrada/malformada), las restantes suman < 1 trivialmente. El grupo
        entero se descarta — no se observa un evento al que le falta una pata."""
        isolated_env.setenv("MARKET_DISCOVERY_SOURCE", "neg_risk")
        import src.utils.config as config_module

        config_module.reset_settings_for_testing()
        svc = DataCaptureService(config_module.get_settings())
        pages = [[
            _raw_market("0x1", "gA"), _raw_market("0x2", "gA"),
            _raw_market("0x3", "gA", active=False),  # pata muerta
            _raw_market("0x4", "gB"), _raw_market("0x5", "gB"), _raw_market("0x6", "gB"),
        ]]
        markets = await svc._discover_neg_risk(_FakeClob(pages))
        assert {m.neg_risk_market_id for m in markets} == {"gB"}

    @pytest.mark.asyncio
    async def test_paginacion_cortada_descarta_todo(self, isolated_env):
        """Si el cap de páginas corta antes de agotar /markets, un grupo puede
        tener patas en páginas nunca vistas: 0 grupos > grupos mentirosos."""
        isolated_env.setenv("MARKET_DISCOVERY_SOURCE", "neg_risk")
        isolated_env.setenv("DISCOVERY_MAX_PAGES", "1")
        import src.utils.config as config_module

        config_module.reset_settings_for_testing()
        svc = DataCaptureService(config_module.get_settings())
        pages = [
            [_raw_market("0x1", "gA"), _raw_market("0x2", "gA"), _raw_market("0x3", "gA")],
            [_raw_market("0x4", "gA")],  # página que el cap nunca ve
        ]
        markets = await svc._discover_neg_risk(_FakeClob(pages))
        assert markets == []

    def test_neg_risk_groups_property(self, isolated_env):
        import src.utils.config as config_module

        svc = DataCaptureService(config_module.get_settings())
        ms = _watched("grp-9", 3, "0xd") + [
            WatchedMarket(
                condition_id="0xsolo", question="binario suelto", token_id_yes="sy",
                token_id_no="sn", end_date_iso=None, neg_risk=False, tick_size=0.01,
            )
        ]
        svc.watched = {m.condition_id: m for m in ms}
        groups = svc.neg_risk_groups
        assert set(groups) == {"grp-9"}
        assert len(groups["grp-9"]) == 3


# =====================================================
# Migración + config
# =====================================================


class TestMigracionYConfig:
    def test_migracion_agrega_motor_a_funnel_existente(self, isolated_env, tmp_path):
        """Simula la DB productiva: funnel_snapshots SIN la columna motor
        -> init_db la agrega con default motor_1 sin tocar las filas."""
        import sqlite3

        import src.db.engine as db_engine

        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE funnel_snapshots (id INTEGER PRIMARY KEY, cycle_ts TIMESTAMP, "
            "markets_evaluated INTEGER, skips_json TEXT, edges_detected INTEGER, "
            "edges_phantom INTEGER, edges_risk_blocked INTEGER, edges_recorded INTEGER, "
            "theoretical_pnl_usd FLOAT, exposure_pct FLOAT, ws_connected BOOLEAN, "
            "cycle_latency_ms FLOAT)"
        )
        conn.execute(
            "INSERT INTO funnel_snapshots (cycle_ts, markets_evaluated) VALUES ('2026-07-01', 5)"
        )
        conn.commit()
        conn.close()

        db_engine.init_db()  # create_all + apply_migrations

        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(funnel_snapshots)")}
        assert "motor" in cols
        row = conn.execute("SELECT motor FROM funnel_snapshots").fetchone()
        conn.close()
        assert row[0] == "motor_1"  # la fila vieja quedó atribuida al motor 1

        db_engine.init_db()  # idempotente

    def test_motor_3_default_apagado(self, settings):
        """Mergear no cambia nada: el motor 2 nace apagado y se enciende por
        env var en Coolify (mismo patrón que el pivote de universo)."""
        assert settings.MOTOR_3_NEG_RISK_ENABLED is False
        assert settings.MARKET_DISCOVERY_SOURCE == "sampling"
        assert settings.MOTOR_3_MIN_LEGS == 3
