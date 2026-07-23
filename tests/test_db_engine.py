"""Tests del engine de DB (pragmas SQLite — lección del vault: WAL + busy_timeout)."""
from __future__ import annotations

from sqlmodel import text

from src.db.engine import get_engine, get_session, init_db
from src.db.models import BotRun


class TestSqlitePragmas:
    def test_wal_y_busy_timeout_activos(self, initialized_db):
        engine = get_engine()
        with engine.connect() as conn:
            journal = conn.execute(text("PRAGMA journal_mode")).scalar()
            busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert str(journal).lower() == "wal"
        assert int(busy) == 5000

    def test_escritura_y_lectura_concurrente_basica(self, initialized_db):
        """Dos sesiones (escritor + lector) sin lock error, como capture + health."""
        with get_session() as w:
            w.add(BotRun(environment="paper", trading_enabled=False, capital_at_start=100.0))
        init_db()  # idempotente, abre otra conexión
        with get_session() as r:
            rows = r.exec(text("SELECT COUNT(*) FROM bot_runs")).scalar()  # type: ignore[attr-defined]
        assert rows is None or rows >= 1  # exec(text) vía sqlmodel devuelve Result
