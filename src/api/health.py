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
from datetime import UTC, datetime, timedelta
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


@app.get("/stats/daily")
async def stats_daily(days: int = 30) -> dict[str, Any]:
    """
    Conteos diarios de telemetría (read-only) — para verificar continuidad de
    captura por HTTP sin terminal (lo consume el agente web). Por día UTC:
    snapshots de mercado, eventos de libro, gaps, ciclos del motor, edges
    shadow, PnL teórico y veredicto del analyst.
    """
    # NOTA de sargabilidad (incidente Kalshi 2026-07-28: consultar el endpoint
    # hermano CONGELÓ el bot): `WHERE date(col) >= x` envuelve la columna en una
    # función y ANULA su índice → full scan. El date() se queda en el
    # SELECT/GROUP BY; el WHERE compara la columna DESNUDA — los timestamps se
    # guardan como ISO, así que el orden lexicográfico contra 'YYYY-MM-DD'
    # coincide con el cronológico. Regla: nunca envolver la columna del WHERE.
    days = max(1, min(days, 120))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    engine = get_engine()

    per_day: dict[str, dict[str, Any]] = {}

    def _fill(sql: str, keys: list[str]) -> None:
        with engine.connect() as conn:
            for row in conn.execute(text(sql), {"cutoff": cutoff}):
                day = str(row[0])
                bucket = per_day.setdefault(day, {})
                for i, key in enumerate(keys, start=1):
                    bucket[key] = row[i]

    _fill(
        "SELECT date(captured_at), COUNT(*) FROM market_snapshots "
        "WHERE captured_at >= :cutoff GROUP BY 1",
        ["market_snapshots"],
    )
    _fill(
        "SELECT date(received_at), COUNT(*), COALESCE(SUM(is_gap), 0) FROM orderbook_events "
        "WHERE received_at >= :cutoff GROUP BY 1",
        ["orderbook_events", "gaps"],
    )
    # Funnel POR MOTOR (lección de la auditoría Kalshi 07-18: el agregado
    # enmascara). Las claves viejas siguen siendo el motor 1 (compat con los
    # reportes del agente web); el motor 2 va con prefijo m2_.
    _fill(
        "SELECT date(cycle_ts), COUNT(*), COALESCE(SUM(edges_recorded), 0), "
        "ROUND(COALESCE(SUM(theoretical_pnl_usd), 0), 4) FROM funnel_snapshots "
        "WHERE cycle_ts >= :cutoff AND COALESCE(motor, 'motor_1') = 'motor_1' GROUP BY 1",
        ["funnel_cycles", "edges_recorded", "theoretical_pnl_usd"],
    )
    _fill(
        "SELECT date(cycle_ts), COUNT(*), COALESCE(SUM(edges_recorded), 0), "
        "ROUND(COALESCE(SUM(theoretical_pnl_usd), 0), 4) FROM funnel_snapshots "
        "WHERE cycle_ts >= :cutoff AND motor = 'motor_2' GROUP BY 1",
        ["m2_funnel_cycles", "m2_edges_recorded", "m2_theoretical_pnl_usd"],
    )
    _fill(
        "SELECT date, verdict FROM analyst_verdicts WHERE date >= :cutoff GROUP BY 1",
        ["verdict"],
    )

    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "daily": dict(sorted(per_day.items())),
    }


@app.get("/stats/edges")
async def stats_edges(days: int = 30) -> dict[str, Any]:
    """
    Distribución del edge BRUTO (1 - ask_YES - ask_NO) sobre market_snapshots
    (read-only). Responde la pregunta del gate F2 en rojo: ¿el mercado observado
    tiene edge por debajo del umbral, o directamente no tiene?
    Nota: es el edge bruto SIN costos; el umbral de shadow aplica sobre el neto.
    """
    days = max(1, min(days, 120))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    engine = get_engine()
    # WHERE sargable — misma regla que /stats/daily (el date(col) del original
    # anulaba el índice; con market_snapshots a 28.7k filas/día no dolía TODAVÍA,
    # pero es la misma bomba que congeló el bot Kalshi con 13M/día).
    base_where = (
        "FROM market_snapshots WHERE captured_at >= :cutoff "
        "AND best_ask_yes IS NOT NULL AND best_ask_no IS NOT NULL"
    )
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                "SELECT COUNT(*), "
                "ROUND(AVG(1 - best_ask_yes - best_ask_no), 5), "
                "ROUND(MAX(1 - best_ask_yes - best_ask_no), 5), "
                "ROUND(MIN(1 - best_ask_yes - best_ask_no), 5) " + base_where
            ),
            {"cutoff": cutoff},
        ).one()
        buckets = {
            "gross_gt_0": "1 - best_ask_yes - best_ask_no > 0",
            "gross_gt_0_5pct": "1 - best_ask_yes - best_ask_no > 0.005",
            "gross_gt_1pct": "1 - best_ask_yes - best_ask_no > 0.01",
            "gross_gt_2pct": "1 - best_ask_yes - best_ask_no > 0.02",
            "gross_gt_5pct": "1 - best_ask_yes - best_ask_no > 0.05",
        }
        counts = {
            name: conn.execute(
                text(f"SELECT COUNT(*) {base_where} AND {cond}"), {"cutoff": cutoff}
            ).scalar()
            for name, cond in buckets.items()
        }
        top = [
            {
                "captured_at": str(row[0]),
                "condition_id": row[1],
                "question": row[2],
                "gross_edge": row[3],
            }
            for row in conn.execute(
                text(
                    "SELECT captured_at, condition_id, substr(question, 1, 80), "
                    "ROUND(1 - best_ask_yes - best_ask_no, 5) AS g "
                    + base_where
                    + " ORDER BY g DESC LIMIT 10"
                ),
                {"cutoff": cutoff},
            )
        ]

    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "snapshots_with_both_asks": totals[0],
        "gross_edge_avg": totals[1],
        "gross_edge_max": totals[2],
        "gross_edge_min": totals[3],
        "counts_above": counts,
        "top_10_gross_edges": top,
        "note": (
            "Edge bruto sin costos. El shadow exige neto >= MIN_EDGE_PCT tras "
            "fees+slippage. counts_above en cero = el universo observado no "
            "presenta ineficiencia; counts_above>0 con edges_recorded=0 = el "
            "umbral/costos filtran todo (recalibrar por config)."
        ),
    }


@app.get("/stats/multi")
async def stats_multi(days: int = 30) -> dict[str, Any]:
    """
    Distribución del edge multi-outcome del Motor 2 (read-only, lo consume el
    agente web sin SQL). Por dirección: ventanas por status del pipeline,
    distribución del edge NETO y top-10. UNIDAD ÚNICA: net_edge_pct es % del
    capital comprometido por set — acá no conviven z-scores ni centavos
    (lección Kalshi 2026-07-28: la columna polimórfica envenena al lector).
    """
    days = max(1, min(days, 120))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    engine = get_engine()

    by_direction: dict[str, dict[str, Any]] = {}
    with engine.connect() as conn:
        for row in conn.execute(
            text(
                "SELECT direction, COUNT(*), "
                "SUM(CASE WHEN status = 'shadow_recorded' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'edge_too_high' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'low_liquidity' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'risk_blocked' THEN 1 ELSE 0 END), "
                "ROUND(AVG(net_edge_pct), 4), ROUND(MAX(net_edge_pct), 4), "
                "ROUND(AVG(legs), 1), "
                "ROUND(COALESCE(SUM(CASE WHEN status = 'shadow_recorded' "
                "THEN theoretical_pnl_usd ELSE 0 END), 0), 4) "
                "FROM multi_edge_windows WHERE detected_at >= :cutoff GROUP BY 1"
            ),
            {"cutoff": cutoff},
        ):
            by_direction[str(row[0])] = {
                "windows_total": row[1],
                "shadow_recorded": row[2] or 0,
                "edge_too_high_fantasma": row[3] or 0,
                "low_liquidity": row[4] or 0,
                "risk_blocked": row[5] or 0,
                "net_edge_pct_avg": row[6],
                "net_edge_pct_max": row[7],
                "legs_avg": row[8],
                "theoretical_pnl_usd": row[9],
            }
        top = [
            {
                "detected_at": str(row[0]),
                "neg_risk_market_id": row[1],
                "direction": row[2],
                "legs": row[3],
                "net_edge_pct": row[4],
                "status": row[5],
            }
            for row in conn.execute(
                text(
                    "SELECT detected_at, neg_risk_market_id, direction, legs, "
                    "ROUND(net_edge_pct, 4), status FROM multi_edge_windows "
                    "WHERE detected_at >= :cutoff AND status = 'shadow_recorded' "
                    "ORDER BY net_edge_pct DESC LIMIT 10"
                ),
                {"cutoff": cutoff},
            )
        ]

    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "by_direction": by_direction,
        "top_10_recorded": top,
        "note": (
            "Motor 2 (neg-risk) en SHADOW: detecta, no ejecuta. net_edge_pct = "
            "% del capital comprometido por set, neto de fees+slippage. "
            "edge_too_high_fantasma alto = grupos incompletos o libros stale, "
            "no oportunidad (anti-fantasma). El top solo lista shadow_recorded."
        ),
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
