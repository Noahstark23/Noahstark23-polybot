"""
Analyst loop (§7): OBSERVAR → ANALIZAR → RECORDAR.

Funciones PURAS de agregación (cero LLMs, cero I/O en la lógica) + un servicio
que genera el AnalystVerdict diario comparable. El humano lee el digest,
decide y recalibra vía env vars — el bot no auto-ajusta parámetros.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import func, select

from src.db.engine import get_session
from src.db.models import AnalystVerdict, FunnelSnapshot, OrderbookEvent

VERDICT_CHECK_INTERVAL_SEC = 600.0


@dataclass
class DayMetrics:
    """Métricas agregadas de un día (comparables día a día)."""

    date: str
    cycles: int = 0
    avg_markets_evaluated: float = 0.0
    edges_detected: int = 0
    edges_phantom: int = 0
    edges_risk_blocked: int = 0
    edges_recorded: int = 0
    theoretical_pnl_usd: float = 0.0
    orderbook_events: int = 0
    gaps: int = 0
    ws_uptime_pct: float = 0.0  # % de ciclos con WS conectado
    skips: dict[str, int] | None = None


def aggregate_day(date: str) -> DayMetrics:
    """Agrega FunnelSnapshots + EdgeWindows + OrderbookEvents de un día UTC."""
    day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    # SQLite guarda datetimes naive — comparar naive
    start_naive, end_naive = day_start.replace(tzinfo=None), day_end.replace(tzinfo=None)

    m = DayMetrics(date=date)
    with get_session() as s:
        funnels = s.exec(
            select(FunnelSnapshot).where(
                FunnelSnapshot.cycle_ts >= start_naive, FunnelSnapshot.cycle_ts < end_naive
            )
        ).all()
        m.cycles = len(funnels)
        if funnels:
            m.avg_markets_evaluated = round(
                sum(f.markets_evaluated for f in funnels) / len(funnels), 2
            )
            m.edges_detected = sum(f.edges_detected for f in funnels)
            m.edges_phantom = sum(f.edges_phantom for f in funnels)
            m.edges_risk_blocked = sum(f.edges_risk_blocked for f in funnels)
            m.edges_recorded = sum(f.edges_recorded for f in funnels)
            m.theoretical_pnl_usd = round(sum(f.theoretical_pnl_usd for f in funnels), 4)
            m.ws_uptime_pct = round(
                sum(1 for f in funnels if f.ws_connected) / len(funnels) * 100.0, 1
            )
            skips: dict[str, int] = {}
            for f in funnels:
                if f.skips_json:
                    for k, v in json.loads(f.skips_json).items():
                        skips[k] = skips.get(k, 0) + v
            m.skips = skips or None

        m.orderbook_events = s.exec(
            select(func.count()).select_from(OrderbookEvent).where(
                OrderbookEvent.received_at >= start_naive,
                OrderbookEvent.received_at < end_naive,
            )
        ).one()
        m.gaps = s.exec(
            select(func.count()).select_from(OrderbookEvent).where(
                OrderbookEvent.received_at >= start_naive,
                OrderbookEvent.received_at < end_naive,
                OrderbookEvent.is_gap == True,  # noqa: E712
            )
        ).one()
    return m


def verdict_from_metrics(m: DayMetrics) -> tuple[str, str]:
    """
    Regla PURA de veredicto (sin LLM):
        no_data   sin ciclos del motor en el día
        degraded  WS < 90% uptime, o gaps > 1% de eventos, o phantom > 50% de edges
        healthy   lo demás
    """
    if m.cycles == 0:
        return "no_data", f"{m.date}: sin ciclos del motor — captura o motor caídos."

    problems = []
    if m.ws_uptime_pct < 90.0:
        problems.append(f"WS uptime {m.ws_uptime_pct}% (<90%)")
    if m.orderbook_events > 0 and m.gaps / m.orderbook_events > 0.01:
        problems.append(f"gaps {m.gaps}/{m.orderbook_events} eventos (>1%)")
    if m.edges_detected > 0 and m.edges_phantom / m.edges_detected > 0.5:
        problems.append(
            f"{m.edges_phantom}/{m.edges_detected} edges fantasma (>50%) — revisar libros"
        )

    base = (
        f"{m.date}: {m.cycles} ciclos, {m.avg_markets_evaluated} mercados/ciclo, "
        f"{m.edges_recorded} edges shadow (${m.theoretical_pnl_usd} PnL teórico), "
        f"{m.edges_phantom} fantasma, {m.edges_risk_blocked} risk-blocked."
    )
    if problems:
        return "degraded", base + " PROBLEMAS: " + "; ".join(problems)
    return "healthy", base


def generate_verdict(date: str) -> AnalystVerdict:
    """Genera (o regenera) el veredicto de un día y lo persiste (upsert por fecha)."""
    metrics = aggregate_day(date)
    verdict, summary = verdict_from_metrics(metrics)
    with get_session() as s:
        row = s.exec(select(AnalystVerdict).where(AnalystVerdict.date == date)).first()
        if row is None:
            row = AnalystVerdict(date=date, verdict=verdict, summary=summary)
        row.verdict = verdict
        row.summary = summary[:2000]
        row.metrics_json = json.dumps(asdict(metrics))
        row.generated_at = datetime.now(UTC)
        s.add(row)
    logger.info(f"AnalystVerdict {date}: {verdict}")
    return AnalystVerdict(
        date=date, verdict=verdict, summary=summary, metrics_json=json.dumps(asdict(metrics))
    )


async def analyst_service(stop_event: asyncio.Event) -> None:
    """Regenera el veredicto del día corriente cada 10 min (idempotente)."""
    while not stop_event.is_set():
        try:
            generate_verdict(datetime.now(UTC).strftime("%Y-%m-%d"))
        except Exception:
            logger.exception("analyst_loop falló (sigo)")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=VERDICT_CHECK_INTERVAL_SEC)
            return
        except TimeoutError:
            continue


def create_service():
    return analyst_service
