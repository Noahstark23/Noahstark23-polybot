"""
DB engine + session factory.

`init_db()` crea tablas al arranque (idempotente en sqlite).
`get_session()` es un context manager con commit/rollback.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlmodel import Session, SQLModel, create_engine

# Importar TODOS los modelos para que SQLModel los registre antes de create_all
from src.db import models  # noqa: F401
from src.utils.config import get_settings

_engine = None


def get_engine():
    """Singleton del engine SQLModel."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        # SQLite necesita este arg para uso multi-thread (uvicorn + asyncio)
        if settings.DATABASE_URL.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            # Crear el directorio del archivo si no existe (local dev / volumen nuevo)
            raw_path = settings.DATABASE_URL.split("///", 1)[-1]
            if raw_path and raw_path != ":memory:":
                Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.DATABASE_URL,
            connect_args=connect_args,
            echo=False,
        )
        if settings.DATABASE_URL.startswith("sqlite"):
            _install_sqlite_pragmas(_engine)
    return _engine


def _install_sqlite_pragmas(engine) -> None:
    """
    SQLite con 2+ escritores concurrentes (data capture + motor + health)
    requiere WAL + busy_timeout — lección del bot Kalshi: el "0 errores" sin
    WAL era engañoso porque aún no había segundo escritor. Se aplica en cada
    conexión nueva del pool.
    """
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def reset_engine_for_testing() -> None:
    """Sólo para tests — fuerza recrear el engine con nueva config."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


# Migraciones ADD COLUMN para la DB productiva existente. create_all crea tablas
# NUEVAS con el schema actual pero NO altera tablas existentes (patrón heredado de
# botkalshi: SQLite sin historia de migración). ADD COLUMN nullable/con default es
# metadata-only en SQLite — instantáneo aun con la tabla grande. Idempotente.
_MIGRATIONS: list[tuple[str, str, str]] = [
    # Motor 2 (2026-07-28): discriminador de motor en el funnel — el agregado
    # enmascara (lección de la auditoría Kalshi 07-18).
    ("funnel_snapshots", "motor", "VARCHAR(20) DEFAULT 'motor_1'"),
]


def apply_migrations(engine) -> None:
    """ADD COLUMN pendientes en tablas que ya existen. Sólo SQLite."""
    if not get_settings().DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, column, col_type in _MIGRATIONS:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {r[1] for r in rows}
            if not existing or column in existing:
                continue  # tabla inexistente (la crea create_all) o ya migrada
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            logger.info(f"Migración: {table}.{column} agregada")


def init_db() -> None:
    """Crear tablas si no existen + migraciones ADD COLUMN. Idempotente."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    apply_migrations(engine)
    logger.info("DB inicializada")


@contextmanager
def get_session() -> Iterator[Session]:
    """
    Sesión con commit automático en éxito y rollback en excepción.

    Uso:
        with get_session() as s:
            s.add(obj)
    """
    engine = get_engine()
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
