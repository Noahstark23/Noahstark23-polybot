"""
Motor 1 — Arbitraje intra-mercado (F2: SHADOW — detecta y registra, NO ejecuta).

Por cada tick (ENGINE_TICK_SECONDS) y cada mercado observado:
    edge bruto = 1.0 − (best_ask_YES + best_ask_NO)        [ARCHITECTURE §6 F2]
    edge neto  = bruto − fees − slippage buffer

Pipeline de filtros (cada skip se cuenta en el funnel):
    no_books        libros sin sincronizar todavía
    below_min_edge  net_edge < MIN_EDGE_PCT
    edge_too_high   net_edge > MIN_EDGE_PCT_MAX  ← FILTRO ANTI-FANTASMA (día 1;
                    lección directa del bot Kalshi: un edge "demasiado bueno"
                    es un libro roto/stale, no plata gratis)
    low_liquidity   depth en el mejor ask < MIN_LIQUIDITY_CONTRACTS
    risk_blocked    RiskManager.check_pre_trade() en dry-run rechazó

Los edges que pasan todo quedan como EdgeWindow(status=shadow_recorded) con el
PnL teórico. Cada ciclo emite un FunnelSnapshot (§7: OBSERVAR).

En F3, con TRADING_ENABLED=true + NO-GO verde, el Executor toma los edges
aprobados por check_and_reserve() — este motor no cambia.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from loguru import logger

from src.api.health import BotState
from src.db.engine import get_session
from src.db.models import EdgeWindow, FunnelSnapshot, utc_now
from src.marketdata import registry
from src.math.fees import arb_edge_pct
from src.risk.manager import RiskManager
from src.utils.config import Settings, get_settings


@dataclass
class EdgeEvaluation:
    """Resultado de evaluar un mercado en un tick."""

    condition_id: str
    status: str  # shadow_recorded | below_min_edge | edge_too_high | low_liquidity | risk_blocked | no_books
    edge_window: EdgeWindow | None = None


class ArbitrageEngine:
    """Motor 1 en shadow. Ejecuta el loop de detección; jamás postea órdenes."""

    def __init__(
        self,
        settings: Settings | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk = risk or RiskManager(self.settings)
        self.executor = None  # F3: el runner lo inyecta SOLO con NO-GO verde

    # ==================================================
    # Evaluación pura de un mercado (testeable sin WS)
    # ==================================================

    def evaluate_market(
        self,
        condition_id: str,
        token_id_yes: str,
        token_id_no: str,
        ask_yes: float | None,
        ask_no: float | None,
        depth_yes: float | None,
        depth_no: float | None,
    ) -> EdgeEvaluation:
        if ask_yes is None or ask_no is None:
            return EdgeEvaluation(condition_id, "no_books")

        gross_pct, costs_pct, net_pct = arb_edge_pct(
            ask_yes, ask_no, self.settings.FEE_RATE_BPS
        )

        if net_pct < self.settings.MIN_EDGE_PCT:
            return EdgeEvaluation(condition_id, "below_min_edge")

        liquidity = min(depth_yes or 0.0, depth_no or 0.0)

        window = EdgeWindow(
            condition_id=condition_id,
            token_id_yes=token_id_yes,
            token_id_no=token_id_no,
            best_ask_yes=ask_yes,
            best_ask_no=ask_no,
            gross_edge_pct=round(gross_pct, 4),
            costs_pct=round(costs_pct, 4),
            net_edge_pct=round(net_pct, 4),
            max_size_contracts=liquidity,
            theoretical_size=0.0,
            theoretical_pnl_usd=0.0,
            status="detected",
        )

        # FILTRO ANTI-EDGE-FANTASMA (activo desde el día 1, §9)
        if net_pct > self.settings.MIN_EDGE_PCT_MAX:
            window.status = "edge_too_high"
            logger.warning(
                f"edge_too_high: {condition_id[:16]}… net={net_pct:.2f}% > "
                f"{self.settings.MIN_EDGE_PCT_MAX}% — NO se ejecuta (anti-fantasma)"
            )
            return EdgeEvaluation(condition_id, "edge_too_high", window)

        if liquidity < self.settings.MIN_LIQUIDITY_CONTRACTS:
            window.status = "low_liquidity"
            return EdgeEvaluation(condition_id, "low_liquidity", window)

        # Sizing teórico: liquidez disponible, capado por el 5% por trade
        cost_per_pair = ask_yes + ask_no
        max_usd = self.risk.max_trade_size_usd()
        size_contracts = min(liquidity, max_usd / cost_per_pair)
        size_usd = size_contracts * cost_per_pair
        window.theoretical_size = round(size_contracts, 2)
        window.theoretical_pnl_usd = round(size_contracts * (net_pct / 100.0), 4)

        # RiskManager en dry-run (F2: registra la decisión, no reserva)
        decision = self.risk.check_pre_trade(size_usd=size_usd, dry_run=True)
        window.risk_approved = decision.approved
        window.risk_reason = decision.reason
        if not decision.approved:
            window.status = "risk_blocked"
            return EdgeEvaluation(condition_id, "risk_blocked", window)

        window.status = "shadow_recorded"
        return EdgeEvaluation(condition_id, "shadow_recorded", window)

    # ==================================================
    # Tick sobre los libros vivos del data capture
    # ==================================================

    def tick(self) -> dict:
        """Evalúa todos los mercados observados. Devuelve el resumen del funnel."""
        started = time.monotonic()
        capture = registry.get_capture()
        skips: dict[str, int] = {}
        evaluations: list[EdgeEvaluation] = []

        if capture is None or not capture.watched:
            skips["no_capture"] = 1
            markets_evaluated = 0
        else:
            for m in capture.watched.values():
                yes = capture.books.get_book(m.token_id_yes)
                no = capture.books.get_book(m.token_id_no)
                ev = self.evaluate_market(
                    condition_id=m.condition_id,
                    token_id_yes=m.token_id_yes,
                    token_id_no=m.token_id_no,
                    ask_yes=yes.best_ask.price if yes and yes.synced and yes.best_ask else None,
                    ask_no=no.best_ask.price if no and no.synced and no.best_ask else None,
                    depth_yes=yes.best_ask.size if yes and yes.best_ask else None,
                    depth_no=no.best_ask.size if no and no.best_ask else None,
                )
                evaluations.append(ev)
                if ev.status != "shadow_recorded":
                    skips[ev.status] = skips.get(ev.status, 0) + 1
            markets_evaluated = len(evaluations)

        windows = [e.edge_window for e in evaluations if e.edge_window is not None]
        recorded = [w for w in windows if w.status == "shadow_recorded"]

        # Oportunidades ejecutables (sólo las usa el Executor en F3; en shadow
        # el executor es None y esta lista muere acá)
        executable = [
            {
                "condition_id": w.condition_id,
                "token_id_yes": w.token_id_yes,
                "token_id_no": w.token_id_no,
                "ask_yes": w.best_ask_yes,
                "ask_no": w.best_ask_no,
                "size_contracts": w.theoretical_size,
                "net_edge_pct": w.net_edge_pct,
            }
            for w in recorded
        ]

        cycle_ts = utc_now()
        funnel = FunnelSnapshot(
            cycle_ts=cycle_ts,
            markets_evaluated=markets_evaluated,
            skips_json=json.dumps(skips) if skips else None,
            edges_detected=len(windows),
            edges_phantom=sum(1 for w in windows if w.status == "edge_too_high"),
            edges_risk_blocked=sum(1 for w in windows if w.status == "risk_blocked"),
            edges_recorded=len(recorded),
            theoretical_pnl_usd=round(sum(w.theoretical_pnl_usd for w in recorded), 4),
            exposure_pct=(
                self.risk.current_exposure_usd() / self.settings.ACTIVE_CAPITAL_USD * 100.0
            ),
            ws_connected=BotState.ws_connected,
            cycle_latency_ms=round((time.monotonic() - started) * 1000.0, 2),
        )

        with get_session() as s:
            for w in windows:
                s.add(w)
            s.add(funnel)

        BotState.last_cycle_at = cycle_ts
        return {
            "markets_evaluated": markets_evaluated,
            "edges_detected": len(windows),
            "edges_recorded": len(recorded),
            "skips": skips,
            "executable": executable,
        }

    # ==================================================
    # Servicio
    # ==================================================

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"Motor 1 (arbitraje) en SHADOW — tick={self.settings.ENGINE_TICK_SECONDS}s, "
            f"min_edge={self.settings.MIN_EDGE_PCT}%, anti-fantasma>{self.settings.MIN_EDGE_PCT_MAX}%"
        )
        while not stop_event.is_set():
            try:
                summary = self.tick()
                if summary["edges_recorded"]:
                    logger.info(
                        f"Tick: evaluados={summary['markets_evaluated']} "
                        f"recorded={summary['edges_recorded']} skips={summary['skips']}"
                    )
                # F3: sólo si el runner inyectó el executor (trading + NO-GO verde)
                if self.executor is not None:
                    for opp in summary["executable"]:
                        await self.executor.execute(opp)
            except Exception:
                logger.exception("Error en tick del motor 1 (sigo)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.ENGINE_TICK_SECONDS
                )
                return
            except TimeoutError:
                continue


def create_service():
    """
    Factory para el runner. El executor SOLO se inyecta si el humano encendió
    trading Y el runner no forzó shadow (checklist NO-GO verde). En cualquier
    otro caso el motor corre en shadow puro.
    """
    settings = get_settings()
    engine = ArbitrageEngine(settings)
    if settings.TRADING_ENABLED and not BotState.shadow_mode:
        from src.motor_1_arbitrage.executor import ArbExecutor

        engine.executor = ArbExecutor(settings, risk=engine.risk)
        logger.warning("⚡ Executor F3 ACTIVO — trading real habilitado por el humano")
    return engine.run
