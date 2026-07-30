"""
Motor 2 — Arbitraje multi-outcome sobre eventos neg-risk (F2: SHADOW puro).

LA TESIS (heredada del motor que ganó en Kalshi): en un evento neg-risk con N
outcomes mutuamente excluyentes, exactamente UNO resuelve YES. Entonces:

    buy_yes_all: comprar YES en las N patas cuesta Σ ask_YES_i y paga 1.00
    buy_no_all:  comprar NO  en las N patas cuesta Σ ask_NO_i  y paga N−1

Si el costo baja del payout, el set es un arbitraje sin riesgo de resolución.
La detección multi-outcome de Kalshi midió edge REAL (3.13pp) — lo que falló
allá fue la EJECUCIÓN (73% de rollback: patas no atómicas en books finos), no
la matemática. Por eso este motor entra por F2 (ARCHITECTURE §6: "cada motor
nuevo re-entra por F2→F3→F4") y NO TIENE EXECUTOR EN EL REPO: el requisito de
ejecución (hard-leg-first, orden por profundidad, presupuesto de re-quote) se
escribe en el gate F2→F3 ANTES de que exista una línea de executor — la
excepción se define antes de encender, no después de ver el resultado.

Lecciones del bot Kalshi aplicadas acá (docs/lecciones_kalshi.md):
  - Anti-fantasma desde el día 1: en multi-outcome UNA pata stale rompe el set
    entero — un edge enorme es un grupo incompleto o un libro roto, no plata.
  - Nada sin tope: de-dupe por (grupo, dirección) — un arb persistente NO
    escribe una fila por tick; tick propio más lento que el binario.
  - Una columna, una unidad: tabla propia multi_edge_windows, todos los *_pct
    en % del capital comprometido por set.
  - El agregado enmascara: FunnelSnapshot lleva motor="motor_2".
  - Fee exacto desde el día 1: fórmula oficial del CLOB por pata + slippage
    por pata.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from loguru import logger

from src.api.health import BotState
from src.db.engine import get_session
from src.db.models import FunnelSnapshot, MultiEdgeWindow, utc_now
from src.marketdata import registry
from src.math.fees import multi_outcome_edge
from src.risk.manager import RiskManager
from src.utils.config import Settings, get_settings

MOTOR = "motor_2"


@dataclass
class LegQuote:
    """Top-of-book de una pata del grupo (None = libro sin sincronizar)."""

    condition_id: str
    ask_yes: float | None
    ask_no: float | None
    depth_yes: float | None
    depth_no: float | None


@dataclass
class GroupEvaluation:
    """Resultado de evaluar una dirección de un grupo en un tick."""

    group_id: str
    direction: str
    status: str  # shadow_recorded | below_min_edge | edge_too_high | low_liquidity | risk_blocked | leg_no_book
    window: MultiEdgeWindow | None = None


class NegRiskEngine:
    """Motor 2 en shadow. Detecta y registra; jamás postea órdenes."""

    def __init__(
        self,
        settings: Settings | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk = risk or RiskManager(self.settings)
        # De-dupe por (grupo, dirección): última oportunidad GRABADA. Un arb que
        # persiste idéntico entre ticks no re-escribe (anti-flood — el incidente
        # market_snapshots de Kalshi: 13M filas/día era el MISMO libro repetido).
        self._last_recorded: dict[tuple[str, str], tuple[float, float]] = {}

    # ==================================================
    # Evaluación pura de una dirección de un grupo
    # ==================================================

    def evaluate_group(
        self,
        group_id: str,
        legs: list[LegQuote],
        direction: str,
    ) -> GroupEvaluation:
        asks = [leg.ask_yes if direction == "buy_yes_all" else leg.ask_no for leg in legs]
        depths = [leg.depth_yes if direction == "buy_yes_all" else leg.depth_no for leg in legs]

        if any(a is None for a in asks):
            # Un set se compra ENTERO o no se compra: sin el libro de una pata
            # no hay evaluación válida (y "sumar las que hay" fabrica edge).
            return GroupEvaluation(group_id, direction, "leg_no_book")

        edge = multi_outcome_edge(
            [float(a) for a in asks],  # type: ignore[arg-type]
            direction,
            self.settings.FEE_RATE_BPS,
        )

        if edge.net_edge_pct < self.settings.MIN_EDGE_PCT:
            return GroupEvaluation(group_id, direction, "below_min_edge")

        min_depth = min(float(d or 0.0) for d in depths)
        window = MultiEdgeWindow(
            neg_risk_market_id=group_id,
            direction=direction,
            legs=edge.legs,
            legs_json=json.dumps(
                [
                    {"condition_id": leg.condition_id, "ask": a}
                    for leg, a in zip(legs, asks, strict=True)
                ]
            ),
            cost_per_set=edge.cost_per_set,
            payout_per_set=edge.payout_per_set,
            fees_per_set=edge.fees_per_set,
            gross_edge_pct=edge.gross_edge_pct,
            net_edge_pct=edge.net_edge_pct,
            min_leg_depth_contracts=min_depth,
            theoretical_size_sets=0.0,
            theoretical_pnl_usd=0.0,
            status="detected",
        )

        # ANTI-FANTASMA — acá es MÁS importante que en el binario: un grupo
        # incompleto (pata que el discovery no vio) o una pata stale produce
        # edges enormes, no sutiles. Ver _discover_neg_risk: esta es la 3ra red.
        if edge.net_edge_pct > self.settings.MIN_EDGE_PCT_MAX:
            window.status = "edge_too_high"
            logger.warning(
                f"edge_too_high M2: grupo {group_id[:16]}… {direction} legs={edge.legs} "
                f"net={edge.net_edge_pct:.2f}% > {self.settings.MIN_EDGE_PCT_MAX}% — "
                "grupo incompleto o libro stale, NO es plata (anti-fantasma)"
            )
            return GroupEvaluation(group_id, direction, "edge_too_high", window)

        if min_depth < self.settings.MIN_LIQUIDITY_CONTRACTS:
            # La pata más fina manda: el set se ejecuta al MÍNIMO común de
            # profundidad (lección REST de Kalshi: books finos = rollback).
            window.status = "low_liquidity"
            return GroupEvaluation(group_id, direction, "low_liquidity", window)

        max_usd = self.risk.max_trade_size_usd()
        size_sets = min(min_depth, max_usd / edge.cost_per_set)
        window.theoretical_size_sets = round(size_sets, 2)
        window.theoretical_pnl_usd = round(size_sets * edge.net_usd_per_set, 4)

        decision = self.risk.check_pre_trade(
            size_usd=size_sets * edge.cost_per_set, dry_run=True
        )
        window.risk_approved = decision.approved
        window.risk_reason = decision.reason
        if not decision.approved:
            window.status = "risk_blocked"
            return GroupEvaluation(group_id, direction, "risk_blocked", window)

        window.status = "shadow_recorded"
        return GroupEvaluation(group_id, direction, "shadow_recorded", window)

    # ==================================================
    # Tick sobre los grupos vivos del data capture
    # ==================================================

    def _legs_from_books(self, capture, markets) -> list[LegQuote]:
        legs = []
        for m in markets:
            yes = capture.books.get_book(m.token_id_yes)
            no = capture.books.get_book(m.token_id_no)
            legs.append(
                LegQuote(
                    condition_id=m.condition_id,
                    ask_yes=yes.best_ask.price if yes and yes.synced and yes.best_ask else None,
                    ask_no=no.best_ask.price if no and no.synced and no.best_ask else None,
                    depth_yes=yes.best_ask.size if yes and yes.best_ask else None,
                    depth_no=no.best_ask.size if no and no.best_ask else None,
                )
            )
        return legs

    def tick(self) -> dict:
        """Evalúa ambas direcciones de cada grupo observado."""
        started = time.monotonic()
        capture = registry.get_capture()
        skips: dict[str, int] = {}
        evaluations: list[GroupEvaluation] = []
        groups_evaluated = 0

        if capture is None or not getattr(capture, "neg_risk_groups", None):
            skips["no_groups"] = 1
        else:
            for group_id, markets in capture.neg_risk_groups.items():
                if len(markets) < self.settings.MOTOR_2_MIN_LEGS:
                    skips["group_too_small"] = skips.get("group_too_small", 0) + 1
                    continue
                groups_evaluated += 1
                legs = self._legs_from_books(capture, markets)
                for direction in ("buy_yes_all", "buy_no_all"):
                    ev = self.evaluate_group(group_id, legs, direction)
                    evaluations.append(ev)
                    if ev.status != "shadow_recorded":
                        skips[ev.status] = skips.get(ev.status, 0) + 1

        # De-dupe: una ventana idéntica a la última grabada del mismo
        # (grupo, dirección) no re-escribe. La clave incluye el edge y el size:
        # si CUALQUIERA cambió, es una observación nueva y se graba.
        windows: list[MultiEdgeWindow] = []
        for ev in evaluations:
            w = ev.window
            if w is None:
                continue
            key = (ev.group_id, ev.direction)
            fingerprint = (w.net_edge_pct, w.theoretical_size_sets)
            if self._last_recorded.get(key) == fingerprint:
                skips["dedup_unchanged"] = skips.get("dedup_unchanged", 0) + 1
                continue
            self._last_recorded[key] = fingerprint
            windows.append(w)

        recorded = [w for w in windows if w.status == "shadow_recorded"]
        cycle_ts = utc_now()
        funnel = FunnelSnapshot(
            motor=MOTOR,
            cycle_ts=cycle_ts,
            markets_evaluated=groups_evaluated,
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

        return {
            "groups_evaluated": groups_evaluated,
            "edges_detected": len(windows),
            "edges_recorded": len(recorded),
            "skips": skips,
        }

    # ==================================================
    # Servicio
    # ==================================================

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"Motor 2 (neg-risk multi-outcome) en SHADOW — tick={self.settings.MOTOR_2_TICK_SECONDS}s, "
            f"min_legs={self.settings.MOTOR_2_MIN_LEGS}, min_edge={self.settings.MIN_EDGE_PCT}%, "
            f"anti-fantasma>{self.settings.MIN_EDGE_PCT_MAX}% — SIN executor (no existe en el repo)"
        )
        while not stop_event.is_set():
            try:
                summary = self.tick()
                if summary["edges_recorded"]:
                    logger.info(
                        f"M2 tick: grupos={summary['groups_evaluated']} "
                        f"recorded={summary['edges_recorded']} skips={summary['skips']}"
                    )
            except Exception:
                logger.exception("Error en tick del motor 2 (sigo)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.MOTOR_2_TICK_SECONDS
                )
                return
            except TimeoutError:
                continue


def create_service():
    """
    Factory para el runner. A diferencia del Motor 1, acá NO hay rama de
    executor: el motor es shadow por construcción hasta que exista el diseño
    de ejecución multi-pata del gate F2→F3 (hard-leg-first) y el humano lo
    apruebe. Encender MOTOR_2_NEG_RISK_ENABLED sólo enciende la DETECCIÓN.
    """
    engine = NegRiskEngine(get_settings())
    return engine.run
