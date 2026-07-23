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
