"""
Health server FastAPI.

Endpoints:
    GET /health         liveness — el proceso está vivo
    GET /ready          readiness — DB conectada e inicializada
    GET /status         snapshot (env, uptime, ws_connected, motores, funnel)
    POST /admin/pause   pausa manual del trading/shadow (F2+)
    POST /admin/resume  levanta la pausa manual
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from loguru import logger
from sqlmodel import text

from src.db.engine import get_engine
from src.utils.config import get_settings


class BotState:
    """
    Estado global observable por el health server.
    El runner y los servicios (data capture, motor) escriben aquí.
    """

    started_at: datetime = datetime.now(UTC)
    db_initialized: bool = False
    ws_connected: bool = False
    motors: list[str] = []
    shadow_mode: bool = True
    is_paused: bool = False
    pause_reason: str | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_cycle_at: datetime | None = None
    markets_watched: int = 0
    disk_free_gb: float | None = None
    disk_low: bool = False

    @classmethod
    def uptime_seconds(cls) -> float:
        return (datetime.now(UTC) - cls.started_at).total_seconds()

    @classmethod
    def record_error(cls, message: str) -> None:
        cls.last_error = message[:500]
        cls.last_error_at = datetime.now(UTC)

    @classmethod
    def fresh_error(cls, ttl_seconds: int) -> tuple[str | None, datetime | None]:
        """
        last_error con TTL: pasado el TTL deja de mostrarse (un error sticky de
        días enmascara errores nuevos — deuda documentada del bot Kalshi que se
        confirmó acá: un 'disk full' del 07-11 seguía colgado el 07-23).
        """
        if cls.last_error is None or cls.last_error_at is None:
            return None, None
        age = (datetime.now(UTC) - cls.last_error_at).total_seconds()
        if age > ttl_seconds:
            return None, None
        return cls.last_error, cls.last_error_at


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Health server starting")
    yield
    logger.info("Health server stopping")


app = FastAPI(
    title="polybot health",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Coolify usa esto para decidir reinicios."""
    return {
        "status": "ok",
        "uptime_seconds": BotState.uptime_seconds(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness: DB responde y está inicializada. 503 si no."""
    checks: dict[str, Any] = {}
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {type(exc).__name__}"
        raise HTTPException(status_code=503, detail={"ready": False, "checks": checks}) from exc

    checks["db_initialized"] = BotState.db_initialized
    if not BotState.db_initialized:
        raise HTTPException(status_code=503, detail={"ready": False, "checks": checks})

    return {"ready": True, "checks": checks}


@app.get("/status")
async def status() -> dict[str, Any]:
    """Snapshot de estado del bot."""
    settings = get_settings()
    error, error_at = BotState.fresh_error(settings.LAST_ERROR_TTL_SECONDS)
    return {
        "env": settings.POLYMARKET_ENV,
        "trading_enabled": settings.TRADING_ENABLED,
        "shadow_mode": BotState.shadow_mode,
        "capital_usd": settings.ACTIVE_CAPITAL_USD,
        "uptime_seconds": BotState.uptime_seconds(),
        "started_at": BotState.started_at.isoformat(),
        "db_initialized": BotState.db_initialized,
        "ws_connected": BotState.ws_connected,
        "motors": BotState.motors,
        "markets_watched": BotState.markets_watched,
        "last_cycle_at": BotState.last_cycle_at.isoformat() if BotState.last_cycle_at else None,
        "is_paused": BotState.is_paused,
        "pause_reason": BotState.pause_reason,
        "disk_free_gb": BotState.disk_free_gb,
        "disk_low": BotState.disk_low,
        "last_error": error,
        "last_error_at": error_at.isoformat() if error_at else None,
    }


@app.post("/admin/pause")
async def admin_pause(reason: str = "manual") -> dict[str, Any]:
    """Pausa manual (motor deja de evaluar; health sigue vivo)."""
    from datetime import timedelta

    from src.risk.manager import RiskManager

    rm = RiskManager()
    rm.pause(timedelta(hours=24), f"admin: {reason}")
    BotState.is_paused = True
    BotState.pause_reason = reason
    return {"paused": True, "reason": reason}


@app.post("/admin/resume")
async def admin_resume() -> dict[str, Any]:
    """Levanta la pausa manual. NO limpia el kill-switch (eso es del humano)."""
    from src.risk.manager import RiskManager

    rm = RiskManager()
    if rm.kill_switch_active():
        raise HTTPException(
            status_code=409,
            detail="Kill-switch activo: sólo se limpia con scripts/clear_kill_switch.py (humano).",
        )
    rm.resume()
    BotState.is_paused = False
    BotState.pause_reason = None
    return {"paused": False}
