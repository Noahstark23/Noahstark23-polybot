"""
Data capture (F1): stream del WS market channel -> SQLite.

Flujo:
    1. Descubrir mercados: WATCHED_CONDITION_IDS o los primeros N binarios activos
       de /sampling-markets (los que tienen rewards suelen ser los líquidos).
    2. Suscribir el WS market channel a los token_ids de esos mercados.
    3. Cada evento pasa por OrderbookManager (libros locales + gaps) y se
       persiste como OrderbookEvent.
    4. Cada SNAPSHOT_INTERVAL se persiste un MarketSnapshot por mercado con
       best bid/ask de ambos tokens (esto alimenta al motor 1 y al analyst).
    5. Instrumentación en BotState: ws_connected, markets_watched.

El gate F1 exige >=48h de captura continua con gaps/60s ~ 0.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger

from src.api.health import BotState
from src.clients.polymarket_clob import PolymarketClobClient
from src.clients.polymarket_ws import PolymarketWsClient
from src.db.engine import get_session
from src.db.models import MarketSnapshot, OrderbookEvent
from src.marketdata.orderbook_manager import OrderbookManager
from src.utils.config import Settings, get_settings

SNAPSHOT_INTERVAL_SEC = 60.0
DISCOVERY_REFRESH_SEC = 3600.0


@dataclass
class WatchedMarket:
    condition_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    end_date_iso: str | None
    neg_risk: bool
    tick_size: float


def extract_binary_market(market: dict) -> WatchedMarket | None:
    """Convierte el payload de /markets en WatchedMarket (sólo binarios Yes/No)."""
    tokens = market.get("tokens") or []
    if len(tokens) != 2:
        return None
    yes = next((t for t in tokens if (t.get("outcome") or "").lower() == "yes"), None)
    no = next((t for t in tokens if (t.get("outcome") or "").lower() == "no"), None)
    if not yes or not no or not yes.get("token_id") or not no.get("token_id"):
        return None
    if market.get("active") is False or market.get("closed") is True:
        return None
    return WatchedMarket(
        condition_id=market.get("condition_id") or "",
        question=(market.get("question") or "")[:500],
        token_id_yes=str(yes["token_id"]),
        token_id_no=str(no["token_id"]),
        end_date_iso=market.get("end_date_iso"),
        neg_risk=bool(market.get("neg_risk")),
        tick_size=float(market.get("minimum_tick_size") or 0.01),
    )


class DataCaptureService:
    """Servicio de captura: WS -> OrderbookManager -> SQLite."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.books = OrderbookManager()
        self.watched: dict[str, WatchedMarket] = {}  # condition_id -> market
        self._event_buffer: list[dict] = []
        self._buffer_lock = asyncio.Lock()

    # ==================================================
    # Descubrimiento de mercados
    # ==================================================

    async def discover_markets(self, clob: PolymarketClobClient) -> None:
        wanted = self.settings.watched_condition_ids
        markets: list[WatchedMarket] = []
        if wanted:
            for cid in wanted:
                try:
                    m = extract_binary_market(await clob.get_market(cid))
                    if m:
                        markets.append(m)
                except Exception:
                    logger.exception(f"No pude cargar mercado {cid}")
        else:
            cursor = ""
            while len(markets) < self.settings.MAX_WATCHED_MARKETS:
                page = await clob.get_sampling_markets(cursor)
                for raw in page.get("data") or []:
                    m = extract_binary_market(raw)
                    if m:
                        markets.append(m)
                    if len(markets) >= self.settings.MAX_WATCHED_MARKETS:
                        break
                cursor = page.get("next_cursor") or "LTE="
                if cursor == "LTE=":
                    break
        self.watched = {m.condition_id: m for m in markets if m.condition_id}
        BotState.markets_watched = len(self.watched)
        logger.info(f"Data capture: observando {len(self.watched)} mercados")

    @property
    def token_ids(self) -> list[str]:
        out = []
        for m in self.watched.values():
            out.extend([m.token_id_yes, m.token_id_no])
        return out

    # ==================================================
    # Persistencia
    # ==================================================

    async def on_ws_event(self, event: dict) -> None:
        summary = self.books.process_event(event)
        if summary is None:
            return
        async with self._buffer_lock:
            self._event_buffer.append(summary)
            if len(self._event_buffer) >= 200:
                self._flush_events_locked()

    def _flush_events_locked(self) -> None:
        if not self._event_buffer:
            return
        rows = [OrderbookEvent(**e) for e in self._event_buffer]
        self._event_buffer = []
        with get_session() as s:
            for r in rows:
                s.add(r)

    async def flush_events(self) -> None:
        async with self._buffer_lock:
            self._flush_events_locked()

    def snapshot_markets(self) -> int:
        """Persiste un MarketSnapshot por mercado observado. Devuelve cuántos."""
        count = 0
        with get_session() as s:
            for m in self.watched.values():
                yes = self.books.get_book(m.token_id_yes)
                no = self.books.get_book(m.token_id_no)
                s.add(
                    MarketSnapshot(
                        condition_id=m.condition_id,
                        question=m.question,
                        end_date_iso=m.end_date_iso,
                        neg_risk=m.neg_risk,
                        tick_size=m.tick_size,
                        token_id_yes=m.token_id_yes,
                        token_id_no=m.token_id_no,
                        best_ask_yes=yes.best_ask.price if yes and yes.best_ask else None,
                        best_ask_no=no.best_ask.price if no and no.best_ask else None,
                        best_bid_yes=yes.best_bid.price if yes and yes.best_bid else None,
                        best_bid_no=no.best_bid.price if no and no.best_bid else None,
                    )
                )
                count += 1
        return count

    # ==================================================
    # Loop principal del servicio
    # ==================================================

    async def run(self, stop_event: asyncio.Event) -> None:
        async with PolymarketClobClient(self.settings) as clob:
            await self.discover_markets(clob)
            if not self.watched:
                logger.warning("Data capture: 0 mercados para observar — reintento en 5min")
                import contextlib

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=300)
                return

            ws = PolymarketWsClient(
                channel="market",
                on_event=self.on_ws_event,
                settings=self.settings,
                asset_ids=self.token_ids,
            )
            ws_task = asyncio.create_task(ws.run(stop_event))

            last_snapshot = 0.0
            last_discovery = asyncio.get_event_loop().time()
            try:
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                        break
                    except TimeoutError:
                        pass

                    BotState.ws_connected = ws.connected
                    BotState.last_cycle_at = datetime.now(UTC)
                    now = asyncio.get_event_loop().time()

                    await self.flush_events()

                    if now - last_snapshot >= SNAPSHOT_INTERVAL_SEC:
                        n = self.snapshot_markets()
                        last_snapshot = now
                        logger.debug(
                            f"Snapshots: {n} mercados | eventos={self.books.events_processed} "
                            f"gaps={self.books.gaps_detected} reconexiones={ws.reconnections}"
                        )

                    if now - last_discovery >= DISCOVERY_REFRESH_SEC:
                        # Refresh de mercados (los cerrados salen; requiere re-suscribir)
                        last_discovery = now
                        logger.info("Data capture: refresh de descubrimiento pendiente de reinicio de WS")
            finally:
                BotState.ws_connected = False
                ws_task.cancel()
                await asyncio.gather(ws_task, return_exceptions=True)
                await self.flush_events()


def create_service():
    """Factory para el runner. Publica la instancia en el registry compartido."""
    from src.marketdata import registry

    service = DataCaptureService()
    registry.set_capture(service)
    return service.run
