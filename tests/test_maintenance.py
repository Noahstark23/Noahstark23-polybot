"""Tests de retención + guard de disco (incidente 2026-07-11: disk full)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from src.api.health import BotState
from src.db.engine import get_session
from src.db.models import FunnelSnapshot, MarketSnapshot, OrderbookEvent, RiskEvent
from src.storage.maintenance import (
    disk_free_gb,
    prune_telemetry,
    run_maintenance_cycle,
)
from src.utils.config import get_settings


def _add_event(days_ago: float) -> None:
    ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)
    with get_session() as s:
        s.add(OrderbookEvent(token_id="1", event_type="book", received_at=ts))


def _add_snapshot(days_ago: float) -> None:
    ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)
    with get_session() as s:
        s.add(
            MarketSnapshot(
                condition_id="0xc1",
                question="q",
                token_id_yes="1",
                token_id_no="2",
                captured_at=ts,
            )
        )


def _count(model) -> int:
    with get_session() as s:
        return len(s.exec(select(model)).all())


class TestPrune:
    def test_borra_viejo_conserva_nuevo(self, initialized_db):
        _add_event(days_ago=20)  # > 14d retención
        _add_event(days_ago=1)
        deleted = prune_telemetry(get_settings())
        assert deleted["orderbook_events"] == 1
        assert _count(OrderbookEvent) == 1

    def test_respeta_retencion_por_tabla(self, initialized_db):
        _add_snapshot(days_ago=20)  # < 30d retención de snapshots -> se queda
        _add_event(days_ago=20)  # > 14d retención de events -> se va
        deleted = prune_telemetry(get_settings())
        assert deleted["market_snapshots"] == 0
        assert deleted["orderbook_events"] == 1

    def test_agresivo_divide_retencion(self, initialized_db):
        _add_event(days_ago=5)  # 14d normal lo conserva; 14//4=3d agresivo lo borra
        assert prune_telemetry(get_settings())["orderbook_events"] == 0
        assert prune_telemetry(get_settings(), aggressive=True)["orderbook_events"] == 1

    def test_funnel_retencion_larga(self, initialized_db):
        ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=100)
        with get_session() as s:
            s.add(FunnelSnapshot(cycle_ts=ts))
        assert prune_telemetry(get_settings())["funnel_snapshots"] == 1


class TestDiskGuard:
    def test_disk_free_positivo(self, tmp_path):
        assert disk_free_gb(tmp_path) > 0

    def test_ciclo_normal_actualiza_botstate(self, initialized_db):
        summary = run_maintenance_cycle(get_settings())
        assert summary["disk_free_gb"] > 0
        assert BotState.disk_free_gb == summary["disk_free_gb"]
        # sandbox tiene disco de sobra -> no agresivo
        assert summary["aggressive"] is False
        assert BotState.disk_low is False

    def test_disco_critico_poda_agresiva_y_risk_event(self, initialized_db, monkeypatch):
        import src.utils.config as cfg

        # forzar el umbral crítico por encima del disco real
        monkeypatch.setenv("DISK_MIN_FREE_GB", "999999")
        monkeypatch.setenv("DISK_WARN_FREE_GB", "9999999")
        cfg.reset_settings_for_testing()
        _add_event(days_ago=5)  # sobrevive retención normal, cae en agresiva
        summary = run_maintenance_cycle(cfg.get_settings())
        assert summary["aggressive"] is True
        assert summary["deleted"]["orderbook_events"] == 1
        assert BotState.disk_low is True
        with get_session() as s:
            events = s.exec(select(RiskEvent).where(RiskEvent.event_type == "disk_low")).all()
            assert len(events) == 1
        BotState.disk_low = False


class TestLastErrorTtl:
    def test_error_fresco_se_muestra(self):
        BotState.record_error("boom")
        error, _ = BotState.fresh_error(ttl_seconds=3600)
        assert error == "boom"
        BotState.last_error = None
        BotState.last_error_at = None

    def test_error_viejo_se_oculta(self):
        BotState.last_error = "viejo"
        BotState.last_error_at = datetime.now(UTC) - timedelta(days=12)
        error, error_at = BotState.fresh_error(ttl_seconds=21600)
        assert error is None and error_at is None
        BotState.last_error = None
        BotState.last_error_at = None

    def test_status_aplica_ttl(self, initialized_db):
        from starlette.testclient import TestClient

        from src.api.health import app

        BotState.db_initialized = True
        BotState.last_error = "disk full de hace 12 dias"
        BotState.last_error_at = datetime.now(UTC) - timedelta(days=12)
        with TestClient(app) as client:
            body = client.get("/status").json()
        assert body["last_error"] is None
        assert "disk_free_gb" in body
        BotState.last_error = None
        BotState.last_error_at = None
        BotState.db_initialized = False
