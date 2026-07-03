"""
Reconciliación posiciones on-chain <-> DB (F3 — sólo con TRADING_ENABLED=true).

Cada RECONCILE_INTERVAL_SECONDS compara las posiciones del wallet (data-api)
contra las posiciones abiertas en la DB:
    - Coinciden (tolerancia 0.01 contratos): marca on_chain_synced=True y
      reconcile_status=ok.
    - Discrepan: RiskEvent crítico + PAUSA PREVENTIVA + reconcile_status=mismatch
      (el checklist NO-GO bloquea el próximo arranque con dinero hasta resolver).
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from loguru import logger
from sqlmodel import select

from src.clients.polymarket_clob import PolymarketClobClient
from src.db.engine import get_session
from src.db.models import Position, utc_now
from src.risk.manager import RiskManager
from src.utils.config import Settings, get_settings

TOLERANCE_CONTRACTS = 0.01
KEY_RECONCILE_STATUS = "reconcile_status"


def _db_positions_by_token() -> dict[str, float]:
    with get_session() as s:
        rows = s.exec(select(Position).where(Position.closed_at == None)).all()  # noqa: E711
        agg: dict[str, float] = {}
        for p in rows:
            agg[p.token_id] = agg.get(p.token_id, 0.0) + p.contracts
        return agg


def _onchain_positions_by_token(raw: list[dict]) -> dict[str, float]:
    agg: dict[str, float] = {}
    for p in raw:
        token = str(p.get("asset") or p.get("token_id") or p.get("tokenId") or "")
        size = float(p.get("size") or 0.0)
        if token and abs(size) > 0:
            agg[token] = agg.get(token, 0.0) + size
    return agg


def compare_positions(
    onchain: dict[str, float], db: dict[str, float], tolerance: float = TOLERANCE_CONTRACTS
) -> list[str]:
    """Devuelve las discrepancias (lista vacía = reconciliado). Función pura."""
    diffs = []
    for token in sorted(set(onchain) | set(db)):
        a, b = onchain.get(token, 0.0), db.get(token, 0.0)
        if abs(a - b) > tolerance:
            diffs.append(f"token {token[:16]}…: on-chain={a} db={b}")
    return diffs


async def reconcile_once(
    clob: PolymarketClobClient, risk: RiskManager, settings: Settings
) -> bool:
    """Un ciclo de reconciliación. True si está limpio."""
    raw = await clob.get_positions()
    onchain = _onchain_positions_by_token(raw if isinstance(raw, list) else [])
    db = _db_positions_by_token()
    diffs = compare_positions(onchain, db)

    if diffs:
        msg = f"Reconciliación FALLÓ: {json.dumps(diffs[:5])}"
        risk._set_state(KEY_RECONCILE_STATUS, "mismatch")
        risk._record_event("reconcile_mismatch", "critical", msg)
        risk.pause(timedelta(hours=24), msg, event_type="reconcile_mismatch")
        logger.critical(msg)
        return False

    risk._set_state(KEY_RECONCILE_STATUS, "ok")
    with get_session() as s:
        rows = s.exec(select(Position).where(Position.closed_at == None)).all()  # noqa: E711
        for p in rows:
            p.on_chain_synced = True
            p.updated_at = utc_now()
            s.add(p)
    logger.debug(f"Reconciliación OK ({len(db)} tokens)")
    return True


async def reconciler_service(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    risk = RiskManager(settings)
    async with PolymarketClobClient(settings) as clob:
        while not stop_event.is_set():
            try:
                await reconcile_once(clob, risk, settings)
            except Exception:
                logger.exception("Reconciliación falló por error técnico (reintento)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.RECONCILE_INTERVAL_SECONDS
                )
                return
            except TimeoutError:
                continue


def create_service():
    settings = get_settings()
    if not settings.TRADING_ENABLED:
        return None  # nunca en shadow
    return reconciler_service
