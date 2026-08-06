"""
Tests del Motor 5 (spillover neg-risk) en SHADOW — port del M9 de Kalshi.

Cubren: detección del salto (umbral, ventana, cooldown anti-retrigger), la
medición del follow-through de las hermanas firmada desde la dirección
ESPERADA (inversa del salto), el descarte con book caído, los topes y
/stats/spillover.
"""
from __future__ import annotations

import pytest
from sqlmodel import select
from starlette.testclient import TestClient

from src.api.health import BotState, app
from src.db.engine import get_session
from src.db.models import SpilloverWindow
from src.marketdata import registry
from src.motor_5_spillover.engine import SpilloverEngine
from src.strategies.data_capture import DataCaptureService, WatchedMarket
from src.utils.config import get_settings


def _watched(gid: str, n: int, prefix: str) -> list[WatchedMarket]:
    return [
        WatchedMarket(
            condition_id=f"{prefix}{i}",
            question=f"outcome {i}",
            token_id_yes=f"{prefix}{i}y",
            token_id_no=f"{prefix}{i}n",
            end_date_iso=None,
            neg_risk=True,
            tick_size=0.01,
            neg_risk_market_id=gid,
        )
        for i in range(n)
    ]


class _MidBooks:
    """Capture fake: mids controlables por token (None = book caído)."""

    def __init__(self, mids: dict[str, float]):
        self.mids = mids

    def _mid_of(self, token_id):
        return self.mids.get(token_id)


@pytest.fixture()
def capture_group(initialized_db):
    svc = DataCaptureService(get_settings())
    markets = _watched("g1", 3, "0xs")
    svc.watched = {m.condition_id: m for m in markets}
    mids = {"0xs0y": 0.40, "0xs1y": 0.35, "0xs2y": 0.25}
    fake = _MidBooks(mids)
    svc._mid_of = fake._mid_of  # inyecta mids controlables
    registry.set_capture(svc)
    yield svc, mids
    registry.set_capture(None)


class TestDetectJump:
    def test_salto_sobre_umbral_dispara_una_vez(self, initialized_db):
        eng = SpilloverEngine(get_settings())
        assert eng.detect_jump("tk", 0.40, now=0.0) is None  # primera foto
        assert eng.detect_jump("tk", 0.41, now=5.0) is None  # +1pp < 5pp
        jump = eng.detect_jump("tk", 0.47, now=10.0)  # +7pp desde 0.40
        assert jump == pytest.approx(7.0)
        # cooldown: el mismo salto no re-dispara en el tick siguiente
        assert eng.detect_jump("tk", 0.47, now=15.0) is None

    def test_ventana_desliza(self, initialized_db):
        eng = SpilloverEngine(get_settings())
        eng.detect_jump("tk", 0.40, now=0.0)
        # 200s después la historia vieja salió de la ventana de 60s
        assert eng.detect_jump("tk", 0.47, now=200.0) is None  # sin referencia


class TestTickYMedicion:
    def test_trigger_mide_hermanas_y_firma_esperada(self, capture_group):
        svc, mids = capture_group
        eng = SpilloverEngine(get_settings())
        eng.tick(now=0.0)  # primera foto de mids
        mids["0xs0y"] = 0.47  # salto +7pp de la pata 0
        s1 = eng.tick(now=10.0)
        assert s1["triggers"] == 1
        # las hermanas AJUSTAN a la baja (como la conservación predice)
        mids["0xs1y"] = 0.31  # −4pp → follow esperado POSITIVO
        eng.tick(now=75.0)  # madura follow60
        eng.tick(now=135.0)  # madura follow120 y persiste
        with get_session() as s:
            rows = list(s.exec(select(SpilloverWindow)))
            assert len(rows) == 2  # una por hermana
            r1 = next(r for r in rows if r.sibling_condition_id == "0xs1")
            assert r1.trigger_move_pp == pytest.approx(7.0)
            assert r1.follow120_pp == pytest.approx(4.0)  # bajó 4pp, firmado +
            r2 = next(r for r in rows if r.sibling_condition_id == "0xs2")
            assert r2.follow120_pp == pytest.approx(0.0)  # no ajustó

    def test_salto_negativo_espera_ajuste_al_alza(self, capture_group):
        svc, mids = capture_group
        eng = SpilloverEngine(get_settings())
        eng.tick(now=0.0)
        mids["0xs0y"] = 0.33  # −7pp
        eng.tick(now=10.0)
        mids["0xs1y"] = 0.38  # subió 3pp → esperado, firmado +
        eng.tick(now=135.0)
        with get_session() as s:
            r1 = next(
                r
                for r in s.exec(select(SpilloverWindow))
                if r.sibling_condition_id == "0xs1"
            )
            assert r1.follow120_pp == pytest.approx(3.0)

    def test_book_caido_toda_la_gracia_descarta(self, capture_group):
        svc, mids = capture_group
        eng = SpilloverEngine(get_settings())
        eng.tick(now=0.0)
        mids["0xs0y"] = 0.47
        eng.tick(now=10.0)
        mids["0xs1y"] = None  # hermana sin book
        mids["0xs2y"] = None
        eng.tick(now=400.0)  # > MEASURE_GRACE
        assert eng.stats["dropped"] >= 2
        with get_session() as s:
            assert list(s.exec(select(SpilloverWindow))) == []

    def test_sin_grupos_no_rompe(self, initialized_db):
        registry.set_capture(None)
        eng = SpilloverEngine(get_settings())
        assert eng.tick(now=0.0)["groups"] == 0


def test_stats_spillover(initialized_db):
    BotState.db_initialized = True
    with get_session() as s:
        for follow in (-2.0, 1.0, 3.0):
            s.add(
                SpilloverWindow(
                    neg_risk_market_id="g1", trigger_condition_id="a",
                    sibling_condition_id="b", trigger_move_pp=6.0,
                    sibling_mid0=0.3, follow60_pp=follow / 2, follow120_pp=follow,
                )
            )
        s.commit()
    body = TestClient(app).get("/stats/spillover").json()
    assert body["windows_measured"] == 3
    assert body["follow120_pp_median"] == pytest.approx(1.0)
    assert body["followed_expectation"] == 2
    assert body["groups_distinct"] == 1


def test_stats_spillover_vacio(initialized_db):
    BotState.db_initialized = True
    body = TestClient(app).get("/stats/spillover").json()
    assert body["windows_measured"] == 0
