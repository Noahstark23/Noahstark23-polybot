"""
Executor del motor 1 (F3 — dinero real; PREPARADO pero APAGADO por default).

Flujo por oportunidad aprobada:
    1. Guard duro: TRADING_ENABLED + POLYMARKET_ENV=production (si no, no-op).
       Estos flags los enciende EL HUMANO (ARCHITECTURE.md §9); además el runner
       sólo inyecta el executor en el motor si el checklist NO-GO está verde.
    2. `RiskManager.check_and_reserve()` — si no aprueba, no hay orden.
    3. Construye y firma las DOS piernas (BUY YES + BUY NO) con build_order.
    4. Persiste Trade(status=pending) por pierna, postea ambas.
    5. Si una pierna falla: cancela la otra, marca error, libera la reserva.
    6. Los fills llegan por el WS user channel -> `handle_user_event` actualiza
       Trade y Position (la reconciliación on-chain los verifica después).
"""
from __future__ import annotations

import uuid
from typing import Any

from loguru import logger
from sqlmodel import select

from src.clients.polymarket_clob import PolymarketClobClient
from src.db.engine import get_session
from src.db.models import Position, Trade, utc_now
from src.risk.manager import RiskManager
from src.utils.config import Settings, get_settings

STRATEGY = "motor_1_arbitrage"


class ArbExecutor:
    """Postea órdenes firmadas. Nunca se instancia en shadow."""

    def __init__(
        self,
        settings: Settings | None = None,
        risk: RiskManager | None = None,
        clob: PolymarketClobClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk = risk or RiskManager(self.settings)
        self.clob = clob or PolymarketClobClient(self.settings)
        self.orders_posted = 0
        self.orders_failed = 0

    async def execute(self, opp: dict[str, Any]) -> bool:
        """
        Ejecuta una oportunidad {condition_id, token_id_yes, token_id_no,
        ask_yes, ask_no, size_contracts, net_edge_pct}. True si ambas piernas
        quedaron posteadas.
        """
        # Guard duro (redundante a propósito: post_order también lo chequea)
        if not self.settings.TRADING_ENABLED or not self.settings.is_production:
            logger.warning("Executor llamado con trading apagado — no-op (shadow)")
            return False

        size = float(opp["size_contracts"])
        cost_per_pair = float(opp["ask_yes"]) + float(opp["ask_no"])
        size_usd = size * cost_per_pair

        decision = self.risk.check_and_reserve(size_usd)
        if not decision.approved:
            logger.info(f"Risk rechazó la ejecución: {decision.reason} (${size_usd:.2f})")
            return False

        legs = [
            ("YES", str(opp["token_id_yes"]), float(opp["ask_yes"])),
            ("NO", str(opp["token_id_no"]), float(opp["ask_no"])),
        ]
        posted: list[tuple[str, str]] = []  # (client_order_id, exchange_order_id)
        created: list[str] = []  # client_order_ids con Trade persistido
        try:
            for outcome, token_id, price in legs:
                client_order_id = f"pb-{uuid.uuid4().hex[:20]}"
                signed = self.clob.build_order(
                    token_id=token_id, price=price, size=size, side="buy"
                )
                trade = Trade(
                    client_order_id=client_order_id,
                    condition_id=str(opp["condition_id"]),
                    token_id=token_id,
                    outcome=outcome,
                    side="buy",
                    size=size,
                    price=price,
                    strategy=STRATEGY,
                    estimated_edge_pct=float(opp.get("net_edge_pct") or 0.0),
                    status="pending",
                )
                with get_session() as s:
                    s.add(trade)
                created.append(client_order_id)

                resp = await self.clob.post_order(signed)
                exchange_id = str(resp.get("orderID") or resp.get("orderId") or "")
                posted.append((client_order_id, exchange_id))
                self.orders_posted += 1
                self._update_trade(client_order_id, status="placed", exchange_order_id=exchange_id)

            logger.info(
                f"✅ Arb ejecutado: {opp['condition_id'][:16]}… size={size} "
                f"edge={opp.get('net_edge_pct')}% (${size_usd:.2f})"
            )
            return True

        except Exception as exc:
            self.orders_failed += 1
            logger.exception(f"Ejecución falló ({type(exc).__name__}) — rollback de piernas")
            # Cancelar lo que llegó a postearse, marcar error TODAS las piernas
            # creadas (incluida la que falló antes de postear) y liberar la reserva
            for _, exchange_id in posted:
                if exchange_id:
                    try:
                        await self.clob.cancel_order(exchange_id)
                    except Exception:
                        logger.exception(f"No pude cancelar {exchange_id} — reconciliación lo verá")
            for client_order_id in created:
                self._update_trade(client_order_id, status="error")
            self.risk.release_reservation(size_usd)
            return False

    @staticmethod
    def _update_trade(client_order_id: str, **fields: Any) -> None:
        with get_session() as s:
            trade = s.exec(
                select(Trade).where(Trade.client_order_id == client_order_id)
            ).first()
            if trade is not None:
                for k, v in fields.items():
                    setattr(trade, k, v)
                s.add(trade)


def handle_user_event(event: dict[str, Any], risk: RiskManager | None = None) -> None:
    """
    Procesa eventos del WS user channel (F3): actualiza Trade y Position.

    event_type "trade": fill (parcial o total) de una de nuestras órdenes.
    event_type "order": cambio de estado (PLACEMENT / UPDATE / CANCELLATION).
    """
    event_type = event.get("event_type") or event.get("type")
    if event_type == "trade":
        _handle_fill(event, risk)
    elif event_type == "order":
        _handle_order_update(event)


def _handle_fill(event: dict[str, Any], risk: RiskManager | None) -> None:
    order_id = str(event.get("taker_order_id") or event.get("order_id") or "")
    price = float(event.get("price") or 0.0)
    size = float(event.get("size") or 0.0)
    if not order_id or size <= 0:
        return

    with get_session() as s:
        trade = s.exec(select(Trade).where(Trade.exchange_order_id == order_id)).first()
        if trade is None:
            logger.warning(f"Fill de orden desconocida {order_id} — reconciliación lo verá")
            return
        trade.fill_price = price
        trade.fill_size = (trade.fill_size or 0.0) + size
        trade.filled_at = utc_now()
        if trade.fill_size >= trade.size - 1e-9:
            trade.status = "filled"
        s.add(trade)

        # Upsert de la posición
        pos = s.exec(
            select(Position).where(
                Position.token_id == trade.token_id, Position.closed_at == None  # noqa: E711
            )
        ).first()
        if pos is None:
            pos = Position(
                condition_id=trade.condition_id,
                token_id=trade.token_id,
                outcome=trade.outcome,
                contracts=size,
                avg_price=price,
                exposure_usd=round(size * price, 6),
                strategy=trade.strategy,
                on_chain_synced=False,
            )
        else:
            total = pos.contracts + size
            pos.avg_price = round((pos.avg_price * pos.contracts + price * size) / total, 6)
            pos.contracts = total
            pos.exposure_usd = round(pos.avg_price * total, 6)
            pos.updated_at = utc_now()
            pos.on_chain_synced = False
        s.add(pos)

    # La exposición ya vive en Position -> liberar la reserva de esta pierna
    if risk is not None:
        risk.release_reservation(size * price)
    logger.info(f"Fill: {order_id[:16]}… {size} @ {price}")


def _handle_order_update(event: dict[str, Any]) -> None:
    order_id = str(event.get("id") or event.get("order_id") or "")
    status = (event.get("status") or event.get("type") or "").upper()
    if not order_id:
        return
    mapped = {"CANCELLATION": "cancelled", "CANCELED": "cancelled", "CANCELLED": "cancelled"}.get(
        status
    )
    if mapped is None:
        return
    with get_session() as s:
        trade = s.exec(select(Trade).where(Trade.exchange_order_id == order_id)).first()
        if trade is not None and trade.status not in ("filled", "settled"):
            trade.status = mapped
            s.add(trade)
