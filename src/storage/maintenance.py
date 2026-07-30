"""
Mantenimiento de storage: retención de telemetría + guard de disco.

Nace del incidente 2026-07-11: "database or disk is full" en data_capture a los
8 días de captura — la lección "nada sin tope" del bot Kalshi (su incidente fue
57GB de orderbook_events), repetida acá. Reglas:

    - Toda tabla de telemetría tiene retención (días, configurable).
    - Guard de disco de lazo cerrado:
        libre < DISK_WARN_FREE_GB  -> warning + poda normal inmediata
        libre < DISK_MIN_FREE_GB   -> poda AGRESIVA (retención /4, mínimo 1 día)
                                      + RiskEvent + BotState.disk_low
    - La telemetría se sacrifica; la captura/detección NUNCA se gatea por disco.
    - Tras podar: wal_checkpoint(TRUNCATE) para devolver espacio del WAL.

Corre como servicio del runner cada MAINTENANCE_INTERVAL_SECONDS.
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlmodel import delete, text

from src.api.health import BotState
from src.db.engine import get_engine, get_session
from src.db.models import (
    ConsensusSignal,
    FunnelSnapshot,
    MarketSnapshot,
    MultiEdgeWindow,
    OrderbookEvent,
    RiskEvent,
)
from src.utils.config import Settings, get_settings

# (modelo, campo timestamp, nombre del setting de retención)
RETENTION_TABLES = [
    (OrderbookEvent, OrderbookEvent.received_at, "RETENTION_ORDERBOOK_EVENTS_DAYS"),
    (MarketSnapshot, MarketSnapshot.captured_at, "RETENTION_MARKET_SNAPSHOTS_DAYS"),
    (FunnelSnapshot, FunnelSnapshot.cycle_ts, "RETENTION_FUNNEL_SNAPSHOTS_DAYS"),
    # Motores 2 y 3 nacen CON retención (nada sin tope: toda tabla nueva entra
    # acá en el mismo commit en que se crea, no cuando explota).
    (MultiEdgeWindow, MultiEdgeWindow.detected_at, "RETENTION_MULTI_EDGE_WINDOWS_DAYS"),
    (ConsensusSignal, ConsensusSignal.detected_at, "RETENTION_CONSENSUS_SIGNALS_DAYS"),
]


def disk_free_gb(path: Path) -> float:
    """GB libres en el filesystem que contiene `path`."""
    probe = path if path.exists() else path.parent
    usage = shutil.disk_usage(probe)
    return usage.free / 1024**3


def _db_path(settings: Settings) -> Path:
    raw = settings.DATABASE_URL.split("///", 1)[-1]
    return Path(raw).expanduser()


def prune_telemetry(settings: Settings | None = None, aggressive: bool = False) -> dict[str, int]:
    """
    Borra filas más viejas que la retención configurada. En modo agresivo la
    retención se divide por 4 (mínimo 1 día). Devuelve {tabla: filas_borradas}.
    """
    settings = settings or get_settings()
    deleted: dict[str, int] = {}
    now = datetime.now(UTC).replace(tzinfo=None)  # SQLite guarda naive UTC
    with get_session() as s:
        for model, ts_field, retention_key in RETENTION_TABLES:
            days = getattr(settings, retention_key)
            if aggressive:
                days = max(1, days // 4)
            cutoff = now - timedelta(days=days)
            result = s.exec(delete(model).where(ts_field < cutoff))  # type: ignore[call-overload]
            deleted[model.__tablename__] = result.rowcount or 0
    return deleted


def checkpoint_wal() -> None:
    """Trunca el WAL para devolver espacio al filesystem tras una poda."""
    engine = get_engine()
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


def run_maintenance_cycle(settings: Settings | None = None) -> dict:
    """Un ciclo: medir disco, decidir modo, podar, checkpoint. Devuelve resumen."""
    settings = settings or get_settings()
    db_path = _db_path(settings)
    free = disk_free_gb(db_path)

    aggressive = free < settings.DISK_MIN_FREE_GB
    warn = free < settings.DISK_WARN_FREE_GB

    BotState.disk_free_gb = round(free, 2)
    BotState.disk_low = aggressive

    deleted = prune_telemetry(settings, aggressive=aggressive)
    total = sum(deleted.values())
    if total:
        checkpoint_wal()

    if aggressive:
        msg = (
            f"Disco CRÍTICO: {free:.2f}GB libres < {settings.DISK_MIN_FREE_GB}GB — "
            f"poda agresiva de telemetría ({deleted}). La captura sigue."
        )
        logger.critical(msg)
        with get_session() as s:
            s.add(
                RiskEvent(
                    event_type="disk_low",
                    severity="critical",
                    message=msg[:1000],
                )
            )
    elif warn:
        logger.warning(
            f"Disco bajo: {free:.2f}GB libres < {settings.DISK_WARN_FREE_GB}GB — "
            f"poda normal ({total} filas)"
        )
    elif total:
        logger.info(f"Retención: {deleted} (disco libre {free:.2f}GB)")

    return {"disk_free_gb": round(free, 2), "aggressive": aggressive, "deleted": deleted}


async def maintenance_service(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    logger.info(
        f"Mantenimiento cada {settings.MAINTENANCE_INTERVAL_SECONDS}s — retención "
        f"events={settings.RETENTION_ORDERBOOK_EVENTS_DAYS}d "
        f"snapshots={settings.RETENTION_MARKET_SNAPSHOTS_DAYS}d "
        f"funnel={settings.RETENTION_FUNNEL_SNAPSHOTS_DAYS}d; "
        f"guard {settings.DISK_WARN_FREE_GB}/{settings.DISK_MIN_FREE_GB}GB"
    )
    while not stop_event.is_set():
        try:
            run_maintenance_cycle(settings)
        except Exception:
            logger.exception("Ciclo de mantenimiento falló (sigo)")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.MAINTENANCE_INTERVAL_SECONDS
            )
            return
        except TimeoutError:
            continue


def create_service():
    return maintenance_service
