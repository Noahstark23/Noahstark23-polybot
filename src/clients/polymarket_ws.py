"""
Cliente WebSocket del CLOB de Polymarket (F1: market channel; F3: user channel).

    market: wss://ws-subscriptions-clob.polymarket.com/ws/market
            subscribe: {"assets_ids": [...], "type": "market"}
    user:   wss://ws-subscriptions-clob.polymarket.com/ws/user
            subscribe: {"auth": {apiKey, secret, passphrase}, "markets": [...], "type": "user"}

Diseño:
    - `parse_frames()` es función pura (testeable sin red).
    - Reconexión con backoff exponencial + jitter; keepalive "PING" cada 10s.
    - Cada reconexión incrementa `reconnections` (instrumentación del gate F1)
      y el consumidor debe re-snapshotear los libros (el WS manda "book" al
      subscribir, así que el snapshot llega solo).
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from loguru import logger

from src.utils.config import Settings, get_settings

PING_INTERVAL_SEC = 10.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0


def parse_frames(raw: str | bytes) -> list[dict[str, Any]]:
    """
    Un frame del WS puede traer un evento (dict) o una lista de eventos.
    Devuelve siempre lista de dicts. Frames no-JSON (p.ej. "PONG") -> [].
    """
    if isinstance(raw, bytes):
        raw = raw.decode()
    raw = raw.strip()
    if not raw or raw.upper() == "PONG":
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


class PolymarketWsClient:
    """Consumidor del WS con reconexión. `on_event` se llama por cada evento."""

    def __init__(
        self,
        channel: str,  # "market" | "user"
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        settings: Settings | None = None,
        asset_ids: list[str] | None = None,
        markets: list[str] | None = None,
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        assert channel in ("market", "user")
        self.settings = settings or get_settings()
        self.channel = channel
        self.on_event = on_event
        self.on_reconnect = on_reconnect
        self.asset_ids = asset_ids or []
        self.markets = markets or []
        self.connected = False
        self.reconnections = 0
        self.frames_received = 0

    @property
    def url(self) -> str:
        return f"{self.settings.CLOB_WS_URL}/ws/{self.channel}"

    def _subscribe_payload(self) -> dict[str, Any]:
        if self.channel == "market":
            return {"assets_ids": self.asset_ids, "type": "market"}
        return {
            "auth": {
                "apiKey": self.settings.CLOB_API_KEY,
                "secret": self.settings.CLOB_SECRET,
                "passphrase": self.settings.CLOB_PASSPHRASE,
            },
            "markets": self.markets,
            "type": "user",
        }

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop principal: conectar, subscribir, consumir; reconectar ante caída."""
        backoff = BACKOFF_INITIAL
        first = True
        while not stop_event.is_set():
            try:
                async with websockets.connect(self.url, max_size=2**22) as ws:
                    await ws.send(json.dumps(self._subscribe_payload()))
                    self.connected = True
                    if not first:
                        self.reconnections += 1
                        if self.on_reconnect is not None:
                            await self.on_reconnect()
                    first = False
                    backoff = BACKOFF_INITIAL
                    logger.info(
                        f"WS {self.channel} conectado ({len(self.asset_ids) or len(self.markets)} suscripciones)"
                    )
                    await self._consume(ws, stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"WS {self.channel} caído: {type(exc).__name__}: {exc}")
            finally:
                self.connected = False

            if stop_event.is_set():
                return
            delay = backoff + random.uniform(0, backoff / 2)
            logger.info(f"WS {self.channel}: reintento en {delay:.1f}s")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _consume(self, ws: Any, stop_event: asyncio.Event) -> None:
        ping_task = asyncio.create_task(self._keepalive(ws, stop_event))
        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except TimeoutError:
                    continue  # keepalive sigue corriendo; el server puede estar quieto
                self.frames_received += 1
                for event in parse_frames(raw):
                    try:
                        await self.on_event(event)
                    except Exception:
                        logger.exception("Error procesando evento WS (sigo consumiendo)")
        finally:
            ping_task.cancel()

    @staticmethod
    async def _keepalive(ws: Any, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(PING_INTERVAL_SEC)
            try:
                await ws.send("PING")
            except Exception:
                return
