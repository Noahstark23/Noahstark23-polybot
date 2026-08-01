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
    # reportes del agente web); M2 consenso va con m2_, M3 neg-risk con m3_.
    # OJO unidad de m2_theoretical: para el consenso es EV (apuesta direccional,
    # se puede perder), no PnL de arbitraje como en m1/m3.
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
        ["m2_funnel_cycles", "m2_signals_recorded", "m2_theoretical_ev_usd"],
    )
    _fill(
        "SELECT date(cycle_ts), COUNT(*), COALESCE(SUM(edges_recorded), 0), "
        "ROUND(COALESCE(SUM(theoretical_pnl_usd), 0), 4) FROM funnel_snapshots "
        "WHERE cycle_ts >= :cutoff AND motor = 'motor_3' GROUP BY 1",
        ["m3_funnel_cycles", "m3_edges_recorded", "m3_theoretical_pnl_usd"],
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


@app.get("/stats/ofi")
async def stats_ofi(days: int = 30) -> dict[str, Any]:
    """
    Señales medidas del Motor 4 (OFI, read-only). UNIDADES: `zscore` es
    adimensional (columna propia — lección Kalshi 2026-07-28); los moves en
    `_pp` firmados desde la presión (>0 = el precio siguió a la presión =
    momentum; <0 = contrarian). La tabla NO tiene |z| < z_min por diseño: es
    el umbral del detector, no un agujero de datos. La PREGUNTA del gate es
    la mediana de move60_pp: si es consistentemente != 0, hay tesis.
    """
    days = max(1, min(days, 120))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    engine = get_engine()
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                "SELECT COUNT(*), ROUND(AVG(move30_pp), 4), ROUND(AVG(move60_pp), 4), "
                "ROUND(MIN(move60_pp), 4), ROUND(MAX(move60_pp), 4), "
                "SUM(CASE WHEN move60_pp > 0 THEN 1 ELSE 0 END), "
                "ROUND(AVG(ABS(zscore)), 2) "
                "FROM ofi_signals WHERE created_at >= :cutoff"
            ),
            {"cutoff": cutoff},
        ).one()
        n = totals[0] or 0
        # Mediana por SQL (SQLite sin percentile): fila del medio ordenada
        median60 = None
        if n:
            median60 = conn.execute(
                text(
                    "SELECT move60_pp FROM ofi_signals WHERE created_at >= :cutoff "
                    "ORDER BY move60_pp LIMIT 1 OFFSET :mid"
                ),
                {"cutoff": cutoff, "mid": n // 2},
            ).scalar()
    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "signals_measured": n,
        "move30_pp_avg": totals[1],
        "move60_pp_avg": totals[2],
        "move60_pp_min": totals[3],
        "move60_pp_max": totals[4],
        "move60_pp_median": round(median60, 4) if median60 is not None else None,
        "followed_pressure": totals[5] or 0,  # move60 > 0 (momentum)
        "zscore_abs_avg": totals[6],
        "note": (
            "Motor 4 (OFI) en SHADOW. moves en pp FIRMADOS desde la presion "
            "(>0 momentum, <0 contrarian) — la mediana de move60_pp es la "
            "pregunta del gate. zscore es adimensional; no hay filas con "
            "|z| < z_min por diseno del detector (no es un agujero de datos). "
            "Referencia Kalshi M8: p50 +3.18pp con n=130."
        ),
    }


@app.get("/stats/spillover")
async def stats_spillover(days: int = 30) -> dict[str, Any]:
    """
    Ventanas medidas del Motor 5 (spillover neg-risk, read-only). UNIDADES:
    todo en `_pp`, follow FIRMADO desde la dirección esperada (inversa del
    salto): follow > 0 = la hermana ajustó como la conservación de
    probabilidad predice. La pregunta del gate: mediana de follow120_pp.
    """
    days = max(1, min(days, 120))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    engine = get_engine()
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                "SELECT COUNT(*), ROUND(AVG(trigger_move_pp), 4), "
                "ROUND(AVG(follow60_pp), 4), ROUND(AVG(follow120_pp), 4), "
                "SUM(CASE WHEN follow120_pp > 0 THEN 1 ELSE 0 END), "
                "COUNT(DISTINCT neg_risk_market_id) "
                "FROM spillover_windows WHERE created_at >= :cutoff"
            ),
            {"cutoff": cutoff},
        ).one()
        n = totals[0] or 0
        median120 = None
        if n:
            median120 = conn.execute(
                text(
                    "SELECT follow120_pp FROM spillover_windows WHERE created_at >= :cutoff "
                    "ORDER BY follow120_pp LIMIT 1 OFFSET :mid"
                ),
                {"cutoff": cutoff, "mid": n // 2},
            ).scalar()
    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "windows_measured": n,
        "trigger_move_pp_avg": totals[1],
        "follow60_pp_avg": totals[2],
        "follow120_pp_avg": totals[3],
        "follow120_pp_median": round(median120, 4) if median120 is not None else None,
        "followed_expectation": totals[4] or 0,
        "groups_distinct": totals[5] or 0,
        "note": (
            "Motor 5 (spillover) en SHADOW. follow en pp FIRMADO desde la "
            "direccion esperada (inversa del salto del trigger): >0 = la "
            "hermana ajusto como la conservacion de probabilidad predice. "
            "La mediana de follow120_pp es la pregunta del gate."
        ),
    }


@app.get("/stats/consensus")
async def stats_consensus(days: int = 30) -> dict[str, Any]:
    """
    Señales del Motor 2 (consenso de sportsbooks, read-only). UNIDAD: los edge
    van en `_pp` = PUNTOS de probabilidad (fair − ask, ×100) — NO son el "% del
    capital" de motor 1/3, y `theoretical_ev_usd` es valor esperado de una
    apuesta DIRECCIONAL, no PnL de arbitraje. La unidad viaja con el dato
    (lección Kalshi 2026-07-28).
    """
    days = max(1, min(days, 120))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    engine = get_engine()

    by_status: dict[str, int] = {}
    recorded_stats: dict[str, Any] = {}
    with engine.connect() as conn:
        for row in conn.execute(
            text(
                "SELECT status, COUNT(*) FROM consensus_signals "
                "WHERE detected_at >= :cutoff GROUP BY 1"
            ),
            {"cutoff": cutoff},
        ):
            by_status[str(row[0])] = row[1]
        rec = conn.execute(
            text(
                "SELECT COUNT(*), ROUND(AVG(net_edge_pp), 4), ROUND(MAX(net_edge_pp), 4), "
                "ROUND(AVG(books_count), 1), ROUND(COALESCE(SUM(theoretical_ev_usd), 0), 4) "
                "FROM consensus_signals WHERE detected_at >= :cutoff "
                "AND status = 'shadow_recorded'"
            ),
            {"cutoff": cutoff},
        ).one()
        recorded_stats = {
            "count": rec[0],
            "net_edge_pp_avg": rec[1],
            "net_edge_pp_max": rec[2],
            "books_count_avg": rec[3],
            "theoretical_ev_usd": rec[4],
        }
        top = [
            {
                "detected_at": str(row[0]),
                "question": row[1],
                "side": row[2],
                "subject_team": row[3],
                "fair_prob": row[4],
                "market_ask": row[5],
                "net_edge_pp": row[6],
                "books": row[7],
            }
            for row in conn.execute(
                text(
                    "SELECT detected_at, substr(question, 1, 80), side, subject_team, "
                    "fair_prob, market_ask, ROUND(net_edge_pp, 4), books_count "
                    "FROM consensus_signals WHERE detected_at >= :cutoff "
                    "AND status = 'shadow_recorded' ORDER BY net_edge_pp DESC LIMIT 10"
                ),
                {"cutoff": cutoff},
            )
        ]

    from src.clients.odds_api import OddsApiClient

    return {
        "days_requested": days,
        "cutoff": cutoff,
        "generated_at": datetime.now(UTC).isoformat(),
        "by_status": by_status,
        "shadow_recorded": recorded_stats,
        "top_10_recorded": top,
        "odds_api_quota_remaining": OddsApiClient.quota_remaining,
        "odds_api_quota_breaker_active": OddsApiClient.quota_breaker_active(),
        "note": (
            "Motor 2 (consenso sportsbooks) en SHADOW: detecta, no ejecuta. "
            "Edges en PUNTOS de probabilidad (pp), EV != PnL (apuesta "
            "direccional). Contexto a priori: esta misma tesis perdio -$432 "
            "reales en Kalshi — esto es el re-test barato en otro venue. "
            "edge_too_high = partido mal emparejado o cuotas stale, no señal."
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
            "Motor 3 (neg-risk) en SHADOW: detecta, no ejecuta. net_edge_pct = "
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
