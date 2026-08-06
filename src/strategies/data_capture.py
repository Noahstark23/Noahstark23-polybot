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
    # Grupo neg-risk al que pertenece (multi-outcome). "" = binario suelto.
    neg_risk_market_id: str = ""


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
        neg_risk_market_id=str(market.get("neg_risk_market_id") or ""),
    )


class DataCaptureService:
    """Servicio de captura: WS -> OrderbookManager -> SQLite."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.books = OrderbookManager()
        self.watched: dict[str, WatchedMarket] = {}  # condition_id -> market
        self._event_buffer: list[dict] = []
        self._buffer_lock = asyncio.Lock()
        # Motor 4 (OFI shadow) va EMBEBIDO acá — se alimenta del mismo stream,
        # igual que el M8 de Kalshi vivía en su feed. Best-effort total
        # (Lección 7): un fallo del shadow JAMÁS rompe la captura.
        self._ofi_shadow = None
        if self.settings.MOTOR_4_OFI_ENABLED:
            from src.motor_4_ofi.shadow import OfiShadow

            self._ofi_shadow = OfiShadow(
                mid_fn=self._mid_of,
                window_sec=self.settings.MOTOR_4_WINDOW_SEC,
                z_min=self.settings.MOTOR_4_Z_MIN,
                min_baseline=self.settings.MOTOR_4_MIN_BASELINE,
                cooldown_sec=self.settings.MOTOR_4_COOLDOWN_SEC,
            )
            logger.info(
                f"Motor 4 (OFI) en SHADOW embebido en el capture — "
                f"z_min={self.settings.MOTOR_4_Z_MIN}, "
                f"baseline>={self.settings.MOTOR_4_MIN_BASELINE}, "
                f"cooldown={self.settings.MOTOR_4_COOLDOWN_SEC}s (NO ejecuta)"
            )

    def _mid_of(self, token_id: str) -> float | None:
        """Mid del token si el libro está sano (synced y con ambas puntas)."""
        book = self.books.get_book(token_id)
        if book is None or not book.synced or book.best_bid is None or book.best_ask is None:
            return None
        return (book.best_bid.price + book.best_ask.price) / 2.0

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
        elif self.settings.MARKET_DISCOVERY_SOURCE == "all_recent":
            markets = await self._discover_all_recent(clob)
        elif self.settings.MARKET_DISCOVERY_SOURCE == "neg_risk":
            markets = await self._discover_neg_risk(clob)
        else:
            markets = await self._discover_sampling(clob)
        self.watched = {m.condition_id: m for m in markets if m.condition_id}
        BotState.markets_watched = len(self.watched)
        logger.info(
            f"Data capture: observando {len(self.watched)} mercados "
            f"(source={self.settings.MARKET_DISCOVERY_SOURCE if not wanted else 'explicit'})"
        )

    async def _discover_sampling(self, clob: PolymarketClobClient) -> list[WatchedMarket]:
        """Universo original: mercados con rewards (los más líquidos/eficientes)."""
        markets: list[WatchedMarket] = []
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
        return markets

    async def _discover_all_recent(self, clob: PolymarketClobClient) -> list[WatchedMarket]:
        """
        Universo long-tail (pivote 2026-07-23): binarios activos de /markets,
        EXCLUYENDO los del set sampling (demostrados eficientes: 0 edges brutos
        en 21 días). Se pagina hasta DISCOVERY_MAX_PAGES y se quedan los
        ÚLTIMOS N descubiertos — asumiendo paginación por creación, los más
        nuevos, que es donde el libro todavía no está arbitrado.
        El shadow valida el supuesto: si los skips no_books dominan el funnel,
        el universo elegido no tiene libros vivos y se re-pivotea.
        """
        sampling_ids: set[str] = set()
        cursor = ""
        for _ in range(self.settings.DISCOVERY_MAX_PAGES):
            page = await clob.get_sampling_markets(cursor)
            for raw in page.get("data") or []:
                cid = raw.get("condition_id")
                if cid:
                    sampling_ids.add(cid)
            cursor = page.get("next_cursor") or "LTE="
            if cursor == "LTE=":
                break

        candidates: list[WatchedMarket] = []
        cursor = ""
        for _ in range(self.settings.DISCOVERY_MAX_PAGES):
            page = await clob.get_markets(cursor)
            for raw in page.get("data") or []:
                m = extract_binary_market(raw)
                if m and m.condition_id not in sampling_ids:
                    candidates.append(m)
            cursor = page.get("next_cursor") or "LTE="
            if cursor == "LTE=":
                break

        picked = candidates[-self.settings.MAX_WATCHED_MARKETS :]
        logger.info(
            f"Discovery all_recent: {len(candidates)} candidatos long-tail "
            f"({len(sampling_ids)} sampling excluidos) -> observando {len(picked)}"
        )
        return picked

    async def _discover_neg_risk(self, clob: PolymarketClobClient) -> list[WatchedMarket]:
        """
        Universo del Motor 2: GRUPOS neg-risk (eventos multi-outcome donde
        exactamente un outcome resuelve YES) con >= MOTOR_3_MIN_LEGS patas.

        EL RIESGO #1 DE ESTE UNIVERSO ES EL GRUPO INCOMPLETO: si una pata del
        evento no entra al grupo (cerrada, malformada, o fuera de las páginas
        recorridas), las patas restantes suman < 1 TRIVIALMENTE y el "edge" que
        el motor vería es fantasma puro — la versión multi-outcome del libro
        stale de Kalshi. Tres defensas, las tres deliberadas:
          1. Se cuentan TODOS los miembros crudos por grupo mientras se pagina;
             un grupo donde extraídos != crudos se DESCARTA entero.
          2. Grupos que tocan la última página recorrida podrían tener patas en
             páginas nunca vistas — si la paginación se cortó por el cap de
             DISCOVERY_MAX_PAGES (no por fin de datos), se descarta TODO el
             discovery parcial y se loguea (mejor 0 grupos que grupos mentirosos).
          3. El filtro anti-fantasma del engine (MIN_EDGE_PCT_MAX) queda como
             última red: un grupo incompleto produce edges enormes, no sutiles.
        """
        raw_counts: dict[str, int] = {}
        extracted: dict[str, list[WatchedMarket]] = {}
        cursor = ""
        exhausted = False
        for _ in range(self.settings.DISCOVERY_MAX_PAGES):
            page = await clob.get_markets(cursor)
            for raw in page.get("data") or []:
                gid = str(raw.get("neg_risk_market_id") or "")
                if not raw.get("neg_risk") or not gid:
                    continue
                raw_counts[gid] = raw_counts.get(gid, 0) + 1
                m = extract_binary_market(raw)
                if m and m.condition_id:
                    extracted.setdefault(gid, []).append(m)
            cursor = page.get("next_cursor") or "LTE="
            if cursor == "LTE=":
                exhausted = True
                break

        if not exhausted:
            logger.warning(
                f"Discovery neg_risk: paginación cortada por DISCOVERY_MAX_PAGES="
                f"{self.settings.DISCOVERY_MAX_PAGES} sin agotar /markets — grupos "
                "potencialmente incompletos, se descarta TODO (0 grupos > grupos mentirosos)"
            )
            return []

        min_legs = self.settings.MOTOR_3_MIN_LEGS
        complete = {
            gid: legs
            for gid, legs in extracted.items()
            if len(legs) == raw_counts.get(gid) and len(legs) >= min_legs
        }
        discarded = len(extracted) - len(complete)

        # Los grupos más nuevos primero (mismo supuesto del pivote all_recent:
        # la paginación va por creación y el libro nuevo está menos arbitrado),
        # capado por MAX_WATCHED_MARKETS en PATAS (es el presupuesto real de WS).
        markets: list[WatchedMarket] = []
        for gid in reversed(list(complete)):
            legs = complete[gid]
            if len(markets) + len(legs) > self.settings.MAX_WATCHED_MARKETS:
                break
            markets.extend(legs)

        groups_kept = len({m.neg_risk_market_id for m in markets})
        logger.info(
            f"Discovery neg_risk: {len(extracted)} grupos vistos, {discarded} descartados "
            f"(incompletos o < {min_legs} patas) -> observando {groups_kept} grupos / "
            f"{len(markets)} patas"
        )
        return markets

    @property
    def neg_risk_groups(self) -> dict[str, list[WatchedMarket]]:
        """Grupos multi-outcome observados: neg_risk_market_id -> patas (lo consume M3)."""
        groups: dict[str, list[WatchedMarket]] = {}
        for m in self.watched.values():
            if m.neg_risk_market_id:
                groups.setdefault(m.neg_risk_market_id, []).append(m)
        return {g: legs for g, legs in groups.items() if len(legs) >= 2}

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
        if self._ofi_shadow is not None:
            # Best-effort (el shadow ya traga sus propias excepciones): la
            # transición de top-of-book alimenta el OFI del Motor 4.
            from src.motor_4_ofi.detector import TopOfBook

            self._ofi_shadow.observe_top(
                summary["token_id"],
                TopOfBook(
                    bid=summary["best_bid"],
                    bid_size=summary["bid_depth"] or 0.0,
                    ask=summary["best_ask"],
                    ask_size=summary["ask_depth"] or 0.0,
                ),
            )
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
