"""Tests del discovery de mercados (pivote de universo 2026-07-23)."""
from __future__ import annotations

import json

import httpx
import pytest

from src.clients.polymarket_clob import PolymarketClobClient
from src.strategies.data_capture import DataCaptureService
from src.utils.config import Settings


def _market(cid: str, active: bool = True) -> dict:
    return {
        "condition_id": cid,
        "question": f"¿{cid}?",
        "active": active,
        "minimum_tick_size": "0.01",
        "tokens": [
            {"token_id": f"{cid}-y", "outcome": "Yes"},
            {"token_id": f"{cid}-n", "outcome": "No"},
        ],
    }


def _paged_transport(sampling_pages: list[list[dict]], all_pages: list[list[dict]]):
    """Mock del CLOB con paginación cursor-based para ambos endpoints."""

    def _serve(pages: list[list[dict]], cursor: str) -> httpx.Response:
        idx = int(cursor) if cursor else 0
        data = pages[idx] if idx < len(pages) else []
        next_cursor = str(idx + 1) if idx + 1 < len(pages) else "LTE="
        return httpx.Response(200, json={"data": data, "next_cursor": next_cursor})

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("next_cursor", "")
        if request.url.path == "/sampling-markets":
            return _serve(sampling_pages, cursor)
        if request.url.path == "/markets":
            return _serve(all_pages, cursor)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _service(source: str, max_watched: int = 3) -> tuple[DataCaptureService, Settings]:
    settings = Settings(
        _env_file=None,
        MARKET_DISCOVERY_SOURCE=source,
        MAX_WATCHED_MARKETS=max_watched,
        DISCOVERY_MAX_PAGES=10,
    )
    svc = DataCaptureService(settings)
    return svc, settings


SAMPLING = [[_market("s1"), _market("s2")]]
ALL = [
    [_market("s1"), _market("t1")],  # s1 también aparece en /markets
    [_market("t2"), _market("t3", active=False)],
    [_market("t4"), _market("t5")],
]


class TestDiscoverySampling:
    async def test_source_sampling_usa_rewards(self, isolated_env):
        svc, settings = _service("sampling")
        clob = PolymarketClobClient(settings, transport=_paged_transport(SAMPLING, ALL))
        await svc.discover_markets(clob)
        assert set(svc.watched) == {"s1", "s2"}


class TestDiscoveryAllRecent:
    async def test_excluye_sampling_y_toma_los_mas_nuevos(self, isolated_env):
        svc, settings = _service("all_recent", max_watched=3)
        clob = PolymarketClobClient(settings, transport=_paged_transport(SAMPLING, ALL))
        await svc.discover_markets(clob)
        # candidatos long-tail: t1, t2, t4, t5 (s1 excluido, t3 inactivo)
        # últimos 3 descubiertos = t2, t4, t5
        assert set(svc.watched) == {"t2", "t4", "t5"}

    async def test_inactivos_no_entran(self, isolated_env):
        svc, settings = _service("all_recent", max_watched=10)
        clob = PolymarketClobClient(settings, transport=_paged_transport(SAMPLING, ALL))
        await svc.discover_markets(clob)
        assert "t3" not in svc.watched
        assert "s1" not in svc.watched

    async def test_config_invalida_rechazada(self, isolated_env):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(_env_file=None, MARKET_DISCOVERY_SOURCE="magico")

    def test_default_sigue_sampling(self, isolated_env):
        assert Settings(_env_file=None).MARKET_DISCOVERY_SOURCE == "sampling"
        assert json.dumps({"ok": True})  # sanity


class TestWatchedExplicitoManda:
    async def test_watched_ids_ignora_source(self, isolated_env):
        settings = Settings(
            _env_file=None,
            MARKET_DISCOVERY_SOURCE="all_recent",
            WATCHED_CONDITION_IDS="s1",
        )
        svc = DataCaptureService(settings)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/markets/s1":
                return httpx.Response(200, json=_market("s1"))
            return httpx.Response(404, json={})

        clob = PolymarketClobClient(settings, transport=httpx.MockTransport(handler))
        await svc.discover_markets(clob)
        assert set(svc.watched) == {"s1"}
