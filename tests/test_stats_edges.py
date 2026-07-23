"""Tests de /stats/edges (distribución del edge bruto — diagnóstico del F2 rojo)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from src.api.health import BotState, app
from src.db.engine import get_session
from src.db.models import MarketSnapshot


def _snap(ask_yes: float | None, ask_no: float | None, days_ago: int = 0) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id="0xc1",
        question="¿pregunta de prueba?",
        token_id_yes="1",
        token_id_no="2",
        best_ask_yes=ask_yes,
        best_ask_no=ask_no,
        captured_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago),
    )


@pytest.fixture()
def client(initialized_db):
    BotState.db_initialized = True
    with get_session() as s:
        s.add(_snap(0.50, 0.51))  # gross -0.01 (mercado eficiente típico)
        s.add(_snap(0.50, 0.499))  # gross +0.001
        s.add(_snap(0.48, 0.49))  # gross +0.03
        s.add(_snap(None, 0.50))  # sin ask YES -> excluido
        s.add(_snap(0.40, 0.40, days_ago=200))  # fuera de ventana
    with TestClient(app) as c:
        yield c
    BotState.db_initialized = False


class TestStatsEdges:
    def test_distribucion(self, client):
        body = client.get("/stats/edges").json()
        assert body["snapshots_with_both_asks"] == 3
        assert body["gross_edge_max"] == pytest.approx(0.03)
        assert body["counts_above"]["gross_gt_0"] == 2
        assert body["counts_above"]["gross_gt_1pct"] == 1
        assert body["counts_above"]["gross_gt_5pct"] == 0

    def test_top_edges(self, client):
        body = client.get("/stats/edges").json()
        top = body["top_10_gross_edges"]
        assert top[0]["gross_edge"] == pytest.approx(0.03)
        assert top[0]["condition_id"] == "0xc1"

    def test_db_vacia_no_rompe(self, initialized_db):
        BotState.db_initialized = True
        with TestClient(app) as c:
            body = c.get("/stats/edges").json()
        assert body["snapshots_with_both_asks"] == 0
        assert body["top_10_gross_edges"] == []
        BotState.db_initialized = False
