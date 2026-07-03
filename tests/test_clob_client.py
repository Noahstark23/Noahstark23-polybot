"""Tests del cliente CLOB (transport mockeado — sin red)."""
from __future__ import annotations

import json

import httpx
import pytest

from src.clients.polymarket_clob import ClobApiError, PolymarketClobClient
from src.utils.config import Settings

MARKETS_PAGE = {
    "data": [
        {
            "condition_id": "0xcond1",
            "question": "¿Pasa X?",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "neg_risk": False,
            "minimum_tick_size": "0.01",
            "active": True,
            "tokens": [
                {"token_id": "111", "outcome": "Yes"},
                {"token_id": "222", "outcome": "No"},
            ],
        }
    ],
    "next_cursor": "LTE=",
    "count": 1,
}

BOOK = {
    "market": "0xcond1",
    "asset_id": "111",
    "hash": "abc",
    "bids": [{"price": "0.45", "size": "100"}],
    "asks": [{"price": "0.55", "size": "80"}],
}


def _mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/markets" or path == "/sampling-markets":
            return httpx.Response(200, json=MARKETS_PAGE)
        if path == "/markets/0xcond1":
            return httpx.Response(200, json=MARKETS_PAGE["data"][0])
        if path == "/book":
            assert request.url.params["token_id"] == "111"
            return httpx.Response(200, json=BOOK)
        if path == "/midpoint":
            return httpx.Response(200, json={"mid": "0.50"})
        if path == "/":
            return httpx.Response(200, text="OK")
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture()
def clob(isolated_env):
    settings = Settings(_env_file=None)
    return PolymarketClobClient(settings, transport=_mock_transport())


class TestLecturasPublicas:
    async def test_get_ok(self, clob):
        assert await clob.get_ok() is True

    async def test_get_markets(self, clob):
        page = await clob.get_markets()
        assert len(page["data"]) == 1
        assert page["data"][0]["condition_id"] == "0xcond1"

    async def test_get_market(self, clob):
        m = await clob.get_market("0xcond1")
        assert m["tokens"][0]["token_id"] == "111"

    async def test_get_orderbook(self, clob):
        book = await clob.get_orderbook("111")
        assert book["bids"][0]["price"] == "0.45"
        assert book["asks"][0]["size"] == "80"

    async def test_error_no_2xx(self, clob):
        with pytest.raises(ClobApiError) as exc:
            await clob._get("/inexistente")
        assert exc.value.status_code == 404


class TestGatesDeSeguridad:
    async def test_post_order_bloqueado_sin_trading(self, clob):
        """post_order JAMÁS funciona con TRADING_ENABLED=false."""
        with pytest.raises(RuntimeError, match="TRADING_ENABLED=false"):
            await clob.post_order({"fake": "order"})

    async def test_l2_headers_exige_credenciales(self, clob):
        assert not clob.has_l2_credentials
        with pytest.raises(RuntimeError, match="Sin credenciales L2"):
            clob._l2_headers("GET", "/balance-allowance")

    def test_l2_headers_firma_hmac(self, isolated_env):
        from base64 import urlsafe_b64encode

        settings = Settings(
            _env_file=None,
            CLOB_API_KEY="key",
            CLOB_SECRET=urlsafe_b64encode(b"secret-bytes-1234").decode(),
            CLOB_PASSPHRASE="pass",
        )
        client = PolymarketClobClient(settings, transport=_mock_transport())
        headers = client._l2_headers("GET", "/balance-allowance")
        assert headers["POLY_API_KEY"] == "key"
        assert headers["POLY_PASSPHRASE"] == "pass"
        assert headers["POLY_SIGNATURE"]  # HMAC presente
        assert json.loads(json.dumps(headers))  # serializable
