"""Tests del cliente WS (parsing puro) y del servicio de data capture."""
from __future__ import annotations

import json

from sqlmodel import select

from src.clients.polymarket_ws import PolymarketWsClient, parse_frames
from src.db.engine import get_session
from src.db.models import MarketSnapshot, OrderbookEvent
from src.strategies.data_capture import DataCaptureService, extract_binary_market

MARKET_PAYLOAD = {
    "condition_id": "0xc1",
    "question": "¿Sube mañana?",
    "end_date_iso": "2026-12-31T00:00:00Z",
    "neg_risk": False,
    "minimum_tick_size": "0.01",
    "active": True,
    "tokens": [
        {"token_id": "111", "outcome": "Yes"},
        {"token_id": "222", "outcome": "No"},
    ],
}


class TestParseFrames:
    def test_dict_unico(self):
        assert parse_frames(json.dumps({"event_type": "book"})) == [{"event_type": "book"}]

    def test_lista(self):
        assert len(parse_frames(json.dumps([{"a": 1}, {"b": 2}]))) == 2

    def test_pong_y_basura(self):
        assert parse_frames("PONG") == []
        assert parse_frames("") == []
        assert parse_frames("no-json") == []

    def test_bytes(self):
        assert parse_frames(json.dumps({"x": 1}).encode()) == [{"x": 1}]


class TestSubscribePayload:
    async def _noop(self, event):
        pass

    def test_market_channel(self, isolated_env):
        ws = PolymarketWsClient("market", self._noop, asset_ids=["111", "222"])
        payload = ws._subscribe_payload()
        assert payload == {"assets_ids": ["111", "222"], "type": "market"}
        assert ws.url.endswith("/ws/market")

    def test_user_channel_lleva_auth(self, isolated_env):
        isolated_env.setenv("CLOB_API_KEY", "k")
        isolated_env.setenv("CLOB_SECRET", "s")
        isolated_env.setenv("CLOB_PASSPHRASE", "p")
        import src.utils.config as cfg

        cfg.reset_settings_for_testing()
        ws = PolymarketWsClient("user", self._noop, markets=["0xc1"])
        payload = ws._subscribe_payload()
        assert payload["auth"]["apiKey"] == "k"
        assert payload["type"] == "user"


class TestExtractBinaryMarket:
    def test_extrae_binario(self):
        m = extract_binary_market(MARKET_PAYLOAD)
        assert m.condition_id == "0xc1"
        assert m.token_id_yes == "111" and m.token_id_no == "222"
        assert m.tick_size == 0.01

    def test_rechaza_no_binario(self):
        bad = dict(MARKET_PAYLOAD, tokens=[{"token_id": "1", "outcome": "A"}])
        assert extract_binary_market(bad) is None

    def test_rechaza_cerrado(self):
        assert extract_binary_market(dict(MARKET_PAYLOAD, closed=True)) is None
        assert extract_binary_market(dict(MARKET_PAYLOAD, active=False)) is None


class TestDataCapturePersistencia:
    async def test_eventos_se_persisten(self, initialized_db):
        svc = DataCaptureService()
        await svc.on_ws_event(
            {
                "event_type": "book",
                "asset_id": "111",
                "hash": "h1",
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "80"}],
            }
        )
        await svc.flush_events()
        with get_session() as s:
            rows = s.exec(select(OrderbookEvent)).all()
            assert len(rows) == 1
            assert rows[0].token_id == "111"
            assert rows[0].best_bid == 0.45
            assert rows[0].is_gap is False

    async def test_snapshot_mercados(self, initialized_db):
        svc = DataCaptureService()
        m = extract_binary_market(MARKET_PAYLOAD)
        svc.watched = {m.condition_id: m}
        await svc.on_ws_event(
            {
                "event_type": "book",
                "asset_id": "111",
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "80"}],
            }
        )
        assert svc.snapshot_markets() == 1
        with get_session() as s:
            snap = s.exec(select(MarketSnapshot)).one()
            assert snap.condition_id == "0xc1"
            assert snap.best_ask_yes == 0.55
            assert snap.best_ask_no is None  # el token NO no tiene libro aún
