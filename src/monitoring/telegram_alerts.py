"""
Digest de Telegram (§7: REPORTAR — el humano lee, decide, recalibra).

Manda cada TELEGRAM_DASHBOARD_INTERVAL_SEC un resumen del funnel del día +
el último AnalystVerdict. Si Telegram no está configurado, el servicio no
arranca (factory devuelve None). Usa un CHAT_ID distinto al del bot Kalshi
para no mezclar (ver .env.example).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from loguru import logger
from sqlmodel import select

from src.analytics.analyst_loop import aggregate_day
from src.db.engine import get_session
from src.db.models import AnalystVerdict, RiskEvent
from src.utils.config import Settings, get_settings


async def send_telegram(text: str, settings: Settings | None = None) -> bool:
    """POST a la API de Telegram. Devuelve False si falla (no lanza)."""
    settings = settings or get_settings()
    if not settings.telegram_configured:
        return False
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            return resp.status_code == 200
    except Exception as exc:
        logger.warning(f"Telegram falló: {type(exc).__name__}")
        return False


def build_digest() -> str:
    """Arma el texto del digest con datos del día (función sin red, testeable)."""
    settings = get_settings()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    m = aggregate_day(today)

    with get_session() as s:
        verdict = s.exec(
            select(AnalystVerdict).order_by(AnalystVerdict.date.desc()).limit(1)  # type: ignore[attr-defined]
        ).first()
        verdict_line = f"Veredicto {verdict.date}: <b>{verdict.verdict}</b>" if verdict else None
        recent_risk = s.exec(
            select(RiskEvent).order_by(RiskEvent.triggered_at.desc()).limit(3)  # type: ignore[attr-defined]
        ).all()
        risk_lines = [f"  • {e.event_type} [{e.severity}] {e.message[:80]}" for e in recent_risk]

    mode = "SHADOW" if not settings.TRADING_ENABLED else "LIVE"
    lines = [
        f"<b>Polybot · {mode} · {settings.POLYMARKET_ENV}</b> — {today}",
        f"Ciclos: {m.cycles} | Mercados/ciclo: {m.avg_markets_evaluated} | WS: {m.ws_uptime_pct}%",
        f"Edges: {m.edges_recorded} shadow / {m.edges_phantom} fantasma / "
        f"{m.edges_risk_blocked} risk-blocked (de {m.edges_detected})",
        f"PnL teórico: ${m.theoretical_pnl_usd}",
        f"Eventos libro: {m.orderbook_events} | Gaps: {m.gaps}",
    ]
    if verdict_line:
        lines.append(verdict_line)
    if risk_lines:
        lines.append("Riesgo reciente:")
        lines.extend(risk_lines)
    return "\n".join(lines)


async def digest_service(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    logger.info(
        f"Digest Telegram cada {settings.TELEGRAM_DASHBOARD_INTERVAL_SEC}s "
        f"(chat {settings.TELEGRAM_CHAT_ID[:6]}…)"
    )
    while not stop_event.is_set():
        try:
            ok = await send_telegram(build_digest(), settings)
            if not ok:
                logger.warning("No pude mandar el digest")
        except Exception:
            logger.exception("digest falló (sigo)")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.TELEGRAM_DASHBOARD_INTERVAL_SEC
            )
            return
        except TimeoutError:
            continue


def create_service():
    """Factory: None si Telegram no está configurado (el runner lo salta)."""
    if not get_settings().telegram_configured:
        return None
    return digest_service
