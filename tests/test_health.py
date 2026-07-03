"""Tests del health server (F0: /health /ready /status; F2+: /admin)."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from src.api.health import BotState, app


@pytest.fixture()
def client(initialized_db):
    BotState.db_initialized = True
    BotState.is_paused = False
    with TestClient(app) as c:
        yield c
    BotState.db_initialized = False


class TestHealth:
    def test_health_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_ready_200_con_db(self, client):
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["ready"] is True

    def test_ready_503_sin_init(self, client):
        BotState.db_initialized = False
        assert client.get("/ready").status_code == 503

    def test_status_campos_gate_f0(self, client):
        body = client.get("/status").json()
        assert "started_at" in body
        assert body["ws_connected"] is False  # gate F0: ws_connected:false
        assert body["env"] == "paper"
        assert body["trading_enabled"] is False
        assert body["shadow_mode"] is True


class TestAdmin:
    def test_pause_y_resume(self, client):
        r = client.post("/admin/pause", params={"reason": "test"})
        assert r.status_code == 200 and r.json()["paused"] is True
        r2 = client.post("/admin/resume")
        assert r2.status_code == 200 and r2.json()["paused"] is False

    def test_resume_no_limpia_kill_switch(self, client):
        from src.risk.manager import RiskManager

        RiskManager().activate_kill_switch("test")
        assert client.post("/admin/resume").status_code == 409
