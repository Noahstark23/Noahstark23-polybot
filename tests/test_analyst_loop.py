"""Tests del analyst_loop (agregación y veredicto — funciones puras + persistencia)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlmodel import select

from src.analytics.analyst_loop import (
    DayMetrics,
    aggregate_day,
    generate_verdict,
    verdict_from_metrics,
)
from src.db.engine import get_session
from src.db.models import AnalystVerdict, FunnelSnapshot, OrderbookEvent

TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


def _add_funnel(**kw):
    defaults = {
        "markets_evaluated": 10,
        "edges_detected": 2,
        "edges_phantom": 0,
        "edges_risk_blocked": 0,
        "edges_recorded": 2,
        "theoretical_pnl_usd": 0.5,
        "ws_connected": True,
        "skips_json": json.dumps({"below_min_edge": 8}),
    }
    defaults.update(kw)
    with get_session() as s:
        s.add(FunnelSnapshot(**defaults))


class TestAggregateDay:
    def test_dia_vacio(self, initialized_db):
        m = aggregate_day(TODAY)
        assert m.cycles == 0
        assert m.gaps == 0

    def test_agrega_funnels(self, initialized_db):
        _add_funnel()
        _add_funnel(ws_connected=False, edges_recorded=1, theoretical_pnl_usd=0.25)
        m = aggregate_day(TODAY)
        assert m.cycles == 2
        assert m.edges_recorded == 3
        assert m.theoretical_pnl_usd == 0.75
        assert m.ws_uptime_pct == 50.0
        assert m.skips == {"below_min_edge": 16}

    def test_cuenta_gaps(self, initialized_db):
        with get_session() as s:
            s.add(OrderbookEvent(token_id="1", event_type="book", is_gap=False))
            s.add(OrderbookEvent(token_id="1", event_type="price_change", is_gap=True))
        m = aggregate_day(TODAY)
        assert m.orderbook_events == 2
        assert m.gaps == 1


class TestVerdict:
    def test_no_data(self):
        v, s = verdict_from_metrics(DayMetrics(date="2026-01-01"))
        assert v == "no_data"

    def test_healthy(self):
        m = DayMetrics(
            date="2026-01-01", cycles=100, ws_uptime_pct=99.0,
            edges_detected=10, edges_phantom=1, orderbook_events=1000, gaps=0,
        )
        v, _ = verdict_from_metrics(m)
        assert v == "healthy"

    def test_degraded_por_ws(self):
        m = DayMetrics(date="2026-01-01", cycles=10, ws_uptime_pct=50.0)
        v, s = verdict_from_metrics(m)
        assert v == "degraded" and "WS uptime" in s

    def test_degraded_por_gaps(self):
        m = DayMetrics(
            date="2026-01-01", cycles=10, ws_uptime_pct=100.0,
            orderbook_events=100, gaps=5,
        )
        v, s = verdict_from_metrics(m)
        assert v == "degraded" and "gaps" in s

    def test_degraded_por_fantasmas(self):
        m = DayMetrics(
            date="2026-01-01", cycles=10, ws_uptime_pct=100.0,
            edges_detected=10, edges_phantom=8,
        )
        v, s = verdict_from_metrics(m)
        assert v == "degraded" and "fantasma" in s


class TestGenerateVerdict:
    def test_persiste_y_es_idempotente(self, initialized_db):
        _add_funnel()
        generate_verdict(TODAY)
        generate_verdict(TODAY)  # upsert, no duplica
        with get_session() as s:
            rows = s.exec(select(AnalystVerdict).where(AnalystVerdict.date == TODAY)).all()
            assert len(rows) == 1
            assert rows[0].verdict == "healthy"
            assert json.loads(rows[0].metrics_json)["cycles"] == 1


class TestDigest:
    def test_build_digest_sin_telegram(self, initialized_db):
        from src.monitoring.telegram_alerts import build_digest

        _add_funnel()
        generate_verdict(TODAY)
        text = build_digest()
        assert "Polybot" in text and "SHADOW" in text
        assert "healthy" in text
