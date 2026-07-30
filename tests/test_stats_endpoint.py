"""Tests del endpoint /stats/daily (continuidad de captura por HTTP)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from src.api.health import BotState, app
from src.db.engine import get_session
from src.db.models import AnalystVerdict, FunnelSnapshot, MarketSnapshot, OrderbookEvent


def _day(days_ago: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)


@pytest.fixture()
def client(initialized_db):
    BotState.db_initialized = True
    with get_session() as s:
        # hoy: 2 snapshots, 3 eventos (1 gap), 2 ciclos con 1 edge
        for _ in range(2):
            s.add(
                MarketSnapshot(
                    condition_id="0xc1", question="q", token_id_yes="1", token_id_no="2",
                    captured_at=_day(0),
                )
            )
        s.add(OrderbookEvent(token_id="1", event_type="book", received_at=_day(0)))
        s.add(OrderbookEvent(token_id="1", event_type="price_change", received_at=_day(0)))
        s.add(
            OrderbookEvent(
                token_id="1", event_type="price_change", is_gap=True, received_at=_day(0)
            )
        )
        s.add(FunnelSnapshot(cycle_ts=_day(0), edges_recorded=1, theoretical_pnl_usd=0.5))
        s.add(FunnelSnapshot(cycle_ts=_day(0)))
        # hace 2 días: sólo 1 snapshot (día "flaco"); ayer: NADA (agujero)
        s.add(
            MarketSnapshot(
                condition_id="0xc1", question="q", token_id_yes="1", token_id_no="2",
                captured_at=_day(2),
            )
        )
        s.add(AnalystVerdict(date=_day(0).strftime("%Y-%m-%d"), verdict="healthy", summary="ok"))
    with TestClient(app) as c:
        yield c
    BotState.db_initialized = False


class TestStatsDaily:
    def test_conteos_por_dia(self, client):
        body = client.get("/stats/daily").json()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert body["daily"][today]["market_snapshots"] == 2
        assert body["daily"][today]["orderbook_events"] == 3
        assert body["daily"][today]["gaps"] == 1
        assert body["daily"][today]["funnel_cycles"] == 2
        assert body["daily"][today]["edges_recorded"] == 1
        assert body["daily"][today]["theoretical_pnl_usd"] == 0.5
        assert body["daily"][today]["verdict"] == "healthy"

    def test_agujero_es_visible(self, client):
        """Un día sin captura simplemente NO aparece — eso ES el agujero."""
        body = client.get("/stats/daily").json()
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        two_ago = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
        assert yesterday not in body["daily"]
        assert body["daily"][two_ago]["market_snapshots"] == 1

    def test_param_days_acotado(self, client):
        assert client.get("/stats/daily", params={"days": 5000}).json()["days_requested"] == 120
        assert client.get("/stats/daily", params={"days": 0}).json()["days_requested"] == 1

    def test_db_vacia_no_rompe(self, initialized_db):
        BotState.db_initialized = True
        with TestClient(app) as c:
            body = c.get("/stats/daily").json()
        assert body["daily"] == {}
        BotState.db_initialized = False


# =====================================================
# Motor 2: breakdown por motor en /stats/daily + /stats/multi
# =====================================================


@pytest.fixture()
def client_con_m2(initialized_db):
    from src.db.models import MultiEdgeWindow

    BotState.db_initialized = True
    with get_session() as s:
        # motor 1: 2 ciclos, 1 edge; motor 2: 1 ciclo, 1 edge con PnL distinto
        s.add(FunnelSnapshot(cycle_ts=_day(0), edges_recorded=1, theoretical_pnl_usd=0.5))
        s.add(FunnelSnapshot(cycle_ts=_day(0)))
        s.add(
            FunnelSnapshot(
                motor="motor_3", cycle_ts=_day(0), edges_recorded=1, theoretical_pnl_usd=0.9
            )
        )
        s.add(
            MultiEdgeWindow(
                neg_risk_market_id="grp-1", direction="buy_yes_all", legs=3,
                cost_per_set=0.96, payout_per_set=1.0, fees_per_set=0.006,
                gross_edge_pct=4.17, net_edge_pct=3.54, min_leg_depth_contracts=40,
                theoretical_size_sets=5.0, theoretical_pnl_usd=0.17,
                status="shadow_recorded", detected_at=_day(0),
            )
        )
        s.add(
            MultiEdgeWindow(
                neg_risk_market_id="grp-2", direction="buy_yes_all", legs=2,
                cost_per_set=0.40, payout_per_set=1.0, fees_per_set=0.004,
                gross_edge_pct=150.0, net_edge_pct=149.0, min_leg_depth_contracts=40,
                theoretical_size_sets=0.0, theoretical_pnl_usd=0.0,
                status="edge_too_high", detected_at=_day(0),
            )
        )
        s.commit()
    return TestClient(app)


def test_stats_daily_separa_motores(client_con_m2):
    """El agregado enmascara (lección auditoría Kalshi 07-18): las claves viejas
    quedan como motor 1 (compat) y el motor 2 va aparte con prefijo m3_."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    body = client_con_m2.get("/stats/daily").json()
    bucket = body["daily"][today]
    assert bucket["funnel_cycles"] == 2  # motor 1 solamente
    assert bucket["theoretical_pnl_usd"] == 0.5
    assert bucket["m3_funnel_cycles"] == 1
    assert bucket["m3_theoretical_pnl_usd"] == 0.9


def test_stats_multi_distribucion_y_fantasmas(client_con_m2):
    body = client_con_m2.get("/stats/multi").json()
    d = body["by_direction"]["buy_yes_all"]
    assert d["windows_total"] == 2
    assert d["shadow_recorded"] == 1
    assert d["edge_too_high_fantasma"] == 1  # el grupo incompleto quedó CONTADO como fantasma
    assert d["theoretical_pnl_usd"] == 0.17
    # el top solo lista shadow_recorded: el fantasma de 149% NO aparece como oportunidad
    assert len(body["top_10_recorded"]) == 1
    assert body["top_10_recorded"][0]["net_edge_pct"] == 3.54


def test_stats_multi_db_vacia_no_rompe(initialized_db):
    BotState.db_initialized = True
    body = TestClient(app).get("/stats/multi").json()
    assert body["by_direction"] == {}
    assert body["top_10_recorded"] == []
