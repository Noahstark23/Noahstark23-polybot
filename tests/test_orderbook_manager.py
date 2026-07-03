"""Tests del OrderbookManager (libros locales + detección de gaps)."""
from __future__ import annotations

from src.marketdata.orderbook_manager import OrderbookManager

BOOK_EVENT = {
    "event_type": "book",
    "asset_id": "111",
    "hash": "h1",
    "bids": [{"price": "0.45", "size": "100"}, {"price": "0.40", "size": "50"}],
    "asks": [{"price": "0.55", "size": "80"}, {"price": "0.60", "size": "30"}],
}


def make_manager_with_book() -> OrderbookManager:
    m = OrderbookManager()
    m.process_event(BOOK_EVENT)
    return m


class TestSnapshot:
    def test_book_crea_libro(self):
        m = make_manager_with_book()
        book = m.get_book("111")
        assert book is not None and book.synced
        assert book.best_bid.price == 0.45 and book.best_bid.size == 100
        assert book.best_ask.price == 0.55 and book.best_ask.size == 80
        assert book.spread == 0.1
        assert book.last_hash == "h1"

    def test_resumen_para_persistir(self):
        m = OrderbookManager()
        summary = m.process_event(BOOK_EVENT)
        assert summary["token_id"] == "111"
        assert summary["event_type"] == "book"
        assert summary["best_bid"] == 0.45
        assert summary["is_gap"] is False


class TestPriceChange:
    def test_aplica_deltas(self):
        m = make_manager_with_book()
        m.process_event(
            {
                "event_type": "price_change",
                "asset_id": "111",
                "hash": "h2",
                "changes": [
                    {"price": "0.46", "side": "BUY", "size": "20"},  # nuevo best bid
                    {"price": "0.55", "side": "SELL", "size": "0"},  # borra best ask
                ],
            }
        )
        book = m.get_book("111")
        assert book.best_bid.price == 0.46
        assert book.best_ask.price == 0.60
        assert book.last_hash == "h2"
        assert m.gaps_detected == 0

    def test_size_cero_borra_nivel(self):
        m = make_manager_with_book()
        m.process_event(
            {
                "event_type": "price_change",
                "asset_id": "111",
                "changes": [{"price": "0.40", "side": "BUY", "size": "0"}],
            }
        )
        assert 0.40 not in m.get_book("111").bids


class TestGaps:
    def test_price_change_sin_book_es_gap(self):
        m = OrderbookManager()
        summary = m.process_event(
            {
                "event_type": "price_change",
                "asset_id": "999",
                "changes": [{"price": "0.5", "side": "BUY", "size": "10"}],
            }
        )
        assert summary["is_gap"] is True
        assert m.gaps_detected == 1
        assert "999" in m.tokens_needing_resync()

    def test_resync_limpia_pendientes(self):
        m = OrderbookManager()
        m.process_event(
            {"event_type": "price_change", "asset_id": "999", "changes": []}
        )
        assert m.tokens_needing_resync() == {"999"}
        assert m.tokens_needing_resync() == set()  # ya drenado

    def test_book_resincroniza(self):
        m = OrderbookManager()
        m.process_event({"event_type": "price_change", "asset_id": "111", "changes": []})
        m.process_event(BOOK_EVENT)
        assert m.get_book("111").synced
        assert m.tokens_synced == 1


class TestOtrosEventos:
    def test_tick_size_change(self):
        m = make_manager_with_book()
        m.process_event(
            {"event_type": "tick_size_change", "asset_id": "111", "new_tick_size": "0.001"}
        )
        assert m.get_book("111").tick_size == 0.001

    def test_evento_desconocido_se_ignora(self):
        m = OrderbookManager()
        assert m.process_event({"event_type": "otra_cosa", "asset_id": "1"}) is None

    def test_evento_sin_token_se_ignora(self):
        m = OrderbookManager()
        assert m.process_event({"event_type": "book"}) is None
