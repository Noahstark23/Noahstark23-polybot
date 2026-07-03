"""
OrderbookManager — libro local por token_id alimentado por el WS market channel
(port de orderbook_manager_v2 del repo base).

Eventos que procesa:
    book               snapshot completo (resetea el libro y el hash)
    price_change       delta de niveles (exige haber visto un book antes -> si no, GAP)
    tick_size_change   cambio de tick del mercado
    last_trade_price   informativo (no muta el libro)

Detección de gaps: un price_change sin book previo, o con hash de libro previo
que no coincide con el que trae el evento, marca gap y pide re-snapshot.
Instrumentación: contadores de eventos y gaps para el funnel (gaps/60s ~ 0 es
parte del gate F1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class TokenBook:
    """Libro local de un token (bids/asks por precio)."""

    token_id: str
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_hash: str | None = None
    tick_size: float = 0.01
    synced: bool = False  # True tras recibir un snapshot "book"
    last_update: datetime | None = None

    @property
    def best_bid(self) -> BookLevel | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return BookLevel(p, self.bids[p])

    @property
    def best_ask(self) -> BookLevel | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return BookLevel(p, self.asks[p])

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return round(ba.price - bb.price, 6)


class OrderbookManager:
    """Mantiene los libros locales y contabiliza gaps."""

    def __init__(self) -> None:
        self._books: dict[str, TokenBook] = {}
        self.events_processed = 0
        self.gaps_detected = 0
        self._gap_tokens: set[str] = set()  # tokens que necesitan re-snapshot

    def get_book(self, token_id: str) -> TokenBook | None:
        return self._books.get(token_id)

    @property
    def tokens_synced(self) -> int:
        return sum(1 for b in self._books.values() if b.synced)

    def tokens_needing_resync(self) -> set[str]:
        """Tokens marcados con gap — data_capture pide snapshot REST y re-sincroniza."""
        pending = self._gap_tokens
        self._gap_tokens = set()
        return pending

    # ==================================================
    # Procesamiento de eventos WS
    # ==================================================

    def process_event(self, event: dict) -> dict | None:
        """
        Procesa un evento del market channel. Devuelve un resumen para persistir
        (o None si el evento no es relevante).
        """
        event_type = event.get("event_type") or event.get("type")
        token_id = str(event.get("asset_id") or event.get("token_id") or "")
        if not event_type or not token_id:
            return None

        self.events_processed += 1
        book = self._books.setdefault(token_id, TokenBook(token_id=token_id))
        is_gap = False

        if event_type == "book":
            self._apply_snapshot(book, event)
        elif event_type == "price_change":
            is_gap = not self._apply_price_change(book, event)
        elif event_type == "tick_size_change":
            book.tick_size = float(event.get("new_tick_size") or book.tick_size)
        elif event_type == "last_trade_price":
            pass  # informativo
        else:
            return None

        if is_gap:
            self.gaps_detected += 1
            self._gap_tokens.add(token_id)
            book.synced = False
            logger.warning(f"GAP detectado en token {token_id[:16]}… — re-snapshot pendiente")

        book.last_update = datetime.now(UTC)
        bb, ba = book.best_bid, book.best_ask
        return {
            "token_id": token_id,
            "event_type": event_type,
            "best_bid": bb.price if bb else None,
            "best_ask": ba.price if ba else None,
            "bid_depth": bb.size if bb else None,
            "ask_depth": ba.size if ba else None,
            "book_hash": book.last_hash,
            "is_gap": is_gap,
        }

    @staticmethod
    def _apply_snapshot(book: TokenBook, event: dict) -> None:
        book.bids = {
            float(lvl["price"]): float(lvl["size"])
            for lvl in event.get("bids") or event.get("buys") or []
        }
        book.asks = {
            float(lvl["price"]): float(lvl["size"])
            for lvl in event.get("asks") or event.get("sells") or []
        }
        book.last_hash = event.get("hash")
        book.synced = True

    @staticmethod
    def _apply_price_change(book: TokenBook, event: dict) -> bool:
        """Aplica deltas. Devuelve False si hay gap (sin snapshot previo)."""
        if not book.synced:
            return False
        changes = event.get("changes") or []
        # formato price_change v2: {"changes": [{"price","side","size"}...], "hash": ...}
        for ch in changes:
            price = float(ch["price"])
            size = float(ch["size"])
            side = (ch.get("side") or "").upper()
            levels = book.bids if side == "BUY" else book.asks
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size
        if event.get("hash"):
            book.last_hash = event["hash"]
        return True
