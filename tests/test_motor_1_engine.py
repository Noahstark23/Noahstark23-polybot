"""Tests del motor 1 en SHADOW (filtros, anti-fantasma, dry-run, funnel)."""
from __future__ import annotations

import pytest
from sqlmodel import select

from src.db.engine import get_session
from src.db.models import EdgeWindow, FunnelSnapshot, Trade
from src.marketdata import registry
from src.motor_1_arbitrage.engine import ArbitrageEngine
from src.strategies.data_capture import DataCaptureService, WatchedMarket
from src.utils.config import get_settings


@pytest.fixture()
def engine(initialized_db):
    return ArbitrageEngine(get_settings())


def _eval(engine, ask_yes, ask_no, depth=50.0, depth_no=None):
    return engine.evaluate_market(
        condition_id="0xc1",
        token_id_yes="111",
        token_id_no="222",
        ask_yes=ask_yes,
        ask_no=ask_no,
        depth_yes=depth,
        depth_no=depth_no if depth_no is not None else depth,
    )


class TestFiltros:
    def test_sin_libros(self, engine):
        assert _eval(engine, None, 0.5).status == "no_books"

    def test_mercado_eficiente_below_min_edge(self, engine):
        # 0.50 + 0.50 = 1.00 -> edge 0
        assert _eval(engine, 0.50, 0.50).status == "below_min_edge"

    def test_edge_valido_shadow_recorded(self, engine):
        # 0.48+0.49=0.97 -> ~2.8% neto con buffer default
        ev = _eval(engine, 0.48, 0.49)
        assert ev.status == "shadow_recorded"
        assert ev.edge_window.risk_approved is True
        assert ev.edge_window.theoretical_size > 0
        assert ev.edge_window.theoretical_pnl_usd > 0

    def test_anti_fantasma_edge_too_high(self, engine):
        # 0.40+0.40=0.80 -> ~19.8% > MIN_EDGE_PCT_MAX (10%) -> FANTASMA, no ejecuta
        ev = _eval(engine, 0.40, 0.40)
        assert ev.status == "edge_too_high"
        assert ev.edge_window.status == "edge_too_high"

    def test_liquidez_insuficiente(self, engine):
        ev = _eval(engine, 0.48, 0.49, depth=5.0)  # < MIN_LIQUIDITY_CONTRACTS=10
        assert ev.status == "low_liquidity"

    def test_liquidez_usa_el_minimo_de_ambos_lados(self, engine):
        ev = _eval(engine, 0.48, 0.49, depth=100.0, depth_no=3.0)
        assert ev.status == "low_liquidity"

    def test_risk_blocked_con_kill_switch(self, engine):
        engine.risk.activate_kill_switch("test")
        ev = _eval(engine, 0.48, 0.49)
        assert ev.status == "risk_blocked"
        assert ev.edge_window.risk_approved is False


class TestSizing:
    def test_size_capado_por_5pct(self, engine):
        # liquidez enorme: el tope 5% de $100 = $5 manda -> size = 5/0.97 pares
        ev = _eval(engine, 0.48, 0.49, depth=10_000.0)
        assert ev.edge_window.theoretical_size == pytest.approx(5.0 / 0.97, abs=0.01)

    def test_size_capado_por_liquidez(self, engine):
        ev = _eval(engine, 0.10, 0.80, depth=12.0)  # cost/pair 0.90; 5/0.9=5.5 > 12? no: 12 > 5.5 -> tope $
        # con depth=12 y tope $5/0.9=5.55 pares, manda el tope de capital
        assert ev.edge_window.theoretical_size == pytest.approx(5.0 / 0.90, abs=0.01)

    def test_shadow_no_escribe_trades(self, engine):
        _eval(engine, 0.48, 0.49)
        with get_session() as s:
            assert s.exec(select(Trade)).all() == []


class TestTickYFunnel:
    def _capture_with_market(self):
        svc = DataCaptureService()
        m = WatchedMarket(
            condition_id="0xc1",
            question="q",
            token_id_yes="111",
            token_id_no="222",
            end_date_iso=None,
            neg_risk=False,
            tick_size=0.01,
        )
        svc.watched = {m.condition_id: m}
        svc.books.process_event(
            {
                "event_type": "book",
                "asset_id": "111",
                "bids": [],
                "asks": [{"price": "0.48", "size": "50"}],
            }
        )
        svc.books.process_event(
            {
                "event_type": "book",
                "asset_id": "222",
                "bids": [],
                "asks": [{"price": "0.49", "size": "50"}],
            }
        )
        return svc

    def test_tick_registra_edge_y_funnel(self, engine):
        registry.set_capture(self._capture_with_market())
        try:
            summary = engine.tick()
        finally:
            registry.set_capture(None)
        assert summary["markets_evaluated"] == 1
        assert summary["edges_recorded"] == 1
        with get_session() as s:
            w = s.exec(select(EdgeWindow)).one()
            assert w.status == "shadow_recorded"
            assert w.net_edge_pct > 1.0
            f = s.exec(select(FunnelSnapshot)).one()
            assert f.edges_recorded == 1
            assert f.markets_evaluated == 1
            assert f.cycle_latency_ms is not None

    def test_tick_sin_captura_no_rompe(self, engine):
        registry.set_capture(None)
        summary = engine.tick()
        assert summary["markets_evaluated"] == 0
        with get_session() as s:
            f = s.exec(select(FunnelSnapshot)).one()
            assert f.skips_json is not None
