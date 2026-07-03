"""
Servicio del WS user channel (F3 — sólo corre con TRADING_ENABLED=true).

Escucha fills y cambios de estado de NUESTRAS órdenes y los aplica a la DB
via handle_user_event. Requiere credenciales L2 (derivadas en F1).
"""
from __future__ import annotations

import asyncio

from loguru import logger

from src.clients.polymarket_clob import PolymarketClobClient
from src.clients.polymarket_ws import PolymarketWsClient
from src.motor_1_arbitrage.executor import handle_user_event
from src.risk.manager import RiskManager
from src.utils.config import get_settings


async def user_channel_service(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    risk = RiskManager(settings)

    async with PolymarketClobClient(settings) as clob:
        if not clob.has_l2_credentials:
            try:
                await clob.derive_api_key()
            except Exception:
                logger.exception("No pude derivar credenciales L2 — user channel no arranca")
                return

    async def on_event(event: dict) -> None:
        handle_user_event(event, risk=risk)

    ws = PolymarketWsClient(
        channel="user",
        on_event=on_event,
        settings=settings,
        markets=settings.watched_condition_ids or [],
    )
    await ws.run(stop_event)


def create_service():
    settings = get_settings()
    if not settings.TRADING_ENABLED:
        return None  # nunca en shadow
    return user_channel_service
