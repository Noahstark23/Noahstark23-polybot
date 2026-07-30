"""
Motor 2 — Consenso de sportsbooks vs Polymarket (F2: SHADOW puro).

La MISMA tesis que el Motor 2 del bot Kalshi, con la MISMA API paga (The Odds
API): si el consenso de sportsbooks (de-vig, mediana entre books) le asigna a
un resultado una probabilidad mayor que el precio del mercado de predicción,
hay edge direccional. ADVERTENCIA a priori, escrita antes de ver un solo dato:
en Kalshi esta tesis PERDIÓ -$432 reales (el edge techó en 0.15pp contra un
umbral de 3pp — la auditoría 2026-07-18 la marcó para apagar). Este shadow es
el re-test barato en OTRO venue: Polymarket puede ser menos eficiente que
Kalshi contra los sportsbooks, o no. El gate F2 decide con datos, no con fe.

Cadena del ciclo (cada skip se cuenta en el funnel):
    sin API key       no_api_key (el motor está encendido pero inutilizable)
    cuota agotada     quota_exhausted (breaker del cliente — API PAGA)
    sin eventos       no_sport_events
    pregunta↔evento   no_match | ambiguous_match  (matcher conservador)
    books < mínimo    few_books (un consenso de 1 book no es consenso)
    sin libro Poly    no_book
    edge < umbral     below_min_edge
    edge > plausible  edge_too_high (ANTI-FANTASMA: partido mal emparejado o
                      cuotas stale — el edge gigante es el síntoma, no premio)
    liquidez          low_liquidity
    riesgo dry-run    risk_blocked

SHADOW por construcción: no existe executor de M2 en el repo. La señal es
DIRECCIONAL (se puede perder con edge real): si algún día se ejecuta, entra
por el gate F2→F3 con sizing fraccional de Kelly y el post-mortem de Kalshi
como requisito de lectura.
"""
from __future__ import annotations

import asyncio
import json
import time
from statistics import median

from loguru import logger

from src.api.health import BotState
from src.clients.odds_api import OddsApiClient, OddsEvent
from src.db.engine import get_session
from src.db.models import ConsensusSignal, FunnelSnapshot, utc_now
from src.marketdata import registry
from src.math.fees import DEFAULT_SLIPPAGE_BUFFER, clob_fee
from src.math.no_vig import implied_prob, remove_vig_multiplicative
from src.motor_2_consensus.matcher import MarketMatch, match_market, normalize_name
from src.risk.manager import RiskManager
from src.utils.config import Settings, get_settings

MOTOR = "motor_2"


def consensus_fair_prob(
    event: OddsEvent, subject_team: str, min_books: int
) -> tuple[float, int] | None:
    """
    Probabilidad JUSTA del subject según el consenso: por cada book con h2h
    válido se quita el vig (multiplicativo) y se toma la prob del subject;
    el consenso es la MEDIANA entre books (robusta a un book roto — igual que
    el detector de Kalshi). None si no llegan `min_books` books válidos.
    """
    subject_norm = normalize_name(subject_team)
    fair_probs: list[float] = []
    for bk in event.bookmakers:
        h2h = next((m for m in bk.markets if m.key == "h2h"), None)
        if h2h is None or len(h2h.outcomes) < 2:
            continue
        try:
            implied = [implied_prob(o.price) for o in h2h.outcomes]
            fair = remove_vig_multiplicative(implied)
        except ValueError:
            continue  # cuota degenerada en este book → book descartado
        idx = next(
            (i for i, o in enumerate(h2h.outcomes) if normalize_name(o.name) == subject_norm),
            None,
        )
        if idx is None:
            continue
        fair_probs.append(fair[idx])
    if len(fair_probs) < min_books:
        return None
    return median(fair_probs), len(fair_probs)


class ConsensusEngine:
    """Motor 2 en shadow: emparejar, de-vig, comparar, registrar. Jamás postea."""

    def __init__(
        self,
        settings: Settings | None = None,
        risk: RiskManager | None = None,
        client_factory=OddsApiClient,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk = risk or RiskManager(self.settings)
        self._client_factory = client_factory
        # De-dupe (nada sin tope): última señal grabada por (condition_id, side).
        self._last_recorded: dict[tuple[str, str], tuple[float, float]] = {}

    # ==================================================
    # Evaluación pura de un mercado emparejado
    # ==================================================

    def evaluate_match(
        self,
        condition_id: str,
        question: str,
        match: MarketMatch,
        ask_yes: float | None,
        ask_no: float | None,
        depth_yes: float | None,
        depth_no: float | None,
    ) -> tuple[str, ConsensusSignal | None]:
        consensus = consensus_fair_prob(
            match.event, match.subject_team, self.settings.MOTOR_2_MIN_BOOKS
        )
        if consensus is None:
            return "few_books", None
        fair, books = consensus

        # Evaluar AMBOS lados y quedarse con el mejor: fair > ask_yes → YES;
        # (1 − fair) > ask_no → NO. Uno solo puede ser positivo a la vez.
        sides = []
        if ask_yes is not None:
            sides.append(("YES", fair, ask_yes, depth_yes))
        if ask_no is not None:
            sides.append(("NO", 1.0 - fair, ask_no, depth_no))
        if not sides:
            return "no_book", None
        side, prob, ask, depth = max(sides, key=lambda s: s[1] - s[2])

        if not 0.0 < ask < 1.0:
            return "no_book", None

        fee = clob_fee(ask, 1.0, self.settings.FEE_RATE_BPS)
        gross_pp = (prob - ask) * 100.0
        net_pp = (prob - ask - fee - DEFAULT_SLIPPAGE_BUFFER) * 100.0

        if net_pp < self.settings.MOTOR_2_MIN_EDGE_PP:
            return "below_min_edge", None

        signal = ConsensusSignal(
            condition_id=condition_id,
            question=question[:500],
            side=side,
            sport_key=match.event.sport_key,
            odds_event_id=match.event.id,
            home_team=match.event.home_team[:100],
            away_team=match.event.away_team[:100],
            subject_team=match.subject_team[:100],
            commence_time_iso=match.event.commence_time.isoformat(),
            books_count=books,
            # SIEMPRE la prob del lado señalado: si side=NO es la prob de que el
            # subject pierda — coherente con market_ask, que es el ask de ese lado.
            fair_prob=round(prob, 6),
            market_ask=ask,
            gross_edge_pp=round(gross_pp, 4),
            net_edge_pp=round(net_pp, 4),
            theoretical_size_contracts=0.0,
            theoretical_ev_usd=0.0,
            status="detected",
        )

        if net_pp > self.settings.MOTOR_2_MAX_PLAUSIBLE_EDGE_PP:
            signal.status = "edge_too_high"
            logger.warning(
                f"edge_too_high M2: {condition_id[:16]}… {side} net={net_pp:.1f}pp > "
                f"{self.settings.MOTOR_2_MAX_PLAUSIBLE_EDGE_PP}pp — partido mal emparejado "
                f"o cuotas stale ({match.event.home_team} vs {match.event.away_team}), "
                "NO es señal (anti-fantasma)"
            )
            return "edge_too_high", signal

        depth = float(depth or 0.0)
        if depth < self.settings.MIN_LIQUIDITY_CONTRACTS:
            signal.status = "low_liquidity"
            return "low_liquidity", signal

        max_usd = self.risk.max_trade_size_usd()
        size = min(depth, max_usd / ask)
        signal.theoretical_size_contracts = round(size, 2)
        # EV, no PnL: apuesta direccional (prob de ganar = fair_prob)
        signal.theoretical_ev_usd = round(size * (prob - ask - fee), 4)

        decision = self.risk.check_pre_trade(size_usd=size * ask, dry_run=True)
        signal.risk_approved = decision.approved
        signal.risk_reason = decision.reason
        if not decision.approved:
            signal.status = "risk_blocked"
            return "risk_blocked", signal

        signal.status = "shadow_recorded"
        return "shadow_recorded", signal

    # ==================================================
    # Ciclo
    # ==================================================

    async def cycle(self) -> dict:
        started = time.monotonic()
        skips: dict[str, int] = {}
        signals: list[ConsensusSignal] = []
        markets_evaluated = 0

        capture = registry.get_capture()
        if not self.settings.ODDS_API_KEY:
            skips["no_api_key"] = 1
        elif OddsApiClient.quota_breaker_active():
            skips["quota_exhausted"] = 1
        elif capture is None or not capture.watched:
            skips["no_capture"] = 1
        else:
            events: list[OddsEvent] = []
            async with self._client_factory(self.settings) as client:
                for sport in self.settings.odds_api_sport_keys:
                    events.extend(await client.get_odds(sport))
            if not events:
                skips["no_sport_events"] = 1
            else:
                for m in capture.watched.values():
                    markets_evaluated += 1
                    matched = match_market(m.question, m.end_date_iso, events)
                    if isinstance(matched, str):
                        skips[matched] = skips.get(matched, 0) + 1
                        continue
                    yes = capture.books.get_book(m.token_id_yes)
                    no = capture.books.get_book(m.token_id_no)
                    status, signal = self.evaluate_match(
                        condition_id=m.condition_id,
                        question=m.question,
                        match=matched,
                        ask_yes=yes.best_ask.price if yes and yes.synced and yes.best_ask else None,
                        ask_no=no.best_ask.price if no and no.synced and no.best_ask else None,
                        depth_yes=yes.best_ask.size if yes and yes.best_ask else None,
                        depth_no=no.best_ask.size if no and no.best_ask else None,
                    )
                    if status != "shadow_recorded":
                        skips[status] = skips.get(status, 0) + 1
                    if signal is None:
                        continue
                    key = (signal.condition_id, signal.side)
                    fingerprint = (signal.net_edge_pp, signal.fair_prob)
                    if self._last_recorded.get(key) == fingerprint:
                        skips["dedup_unchanged"] = skips.get("dedup_unchanged", 0) + 1
                        continue
                    self._last_recorded[key] = fingerprint
                    signals.append(signal)

        recorded = [s for s in signals if s.status == "shadow_recorded"]
        funnel = FunnelSnapshot(
            motor=MOTOR,
            cycle_ts=utc_now(),
            markets_evaluated=markets_evaluated,
            skips_json=json.dumps(skips) if skips else None,
            edges_detected=len(signals),
            edges_phantom=sum(1 for s in signals if s.status == "edge_too_high"),
            edges_risk_blocked=sum(1 for s in signals if s.status == "risk_blocked"),
            edges_recorded=len(recorded),
            theoretical_pnl_usd=round(sum(s.theoretical_ev_usd for s in recorded), 4),
            exposure_pct=(
                self.risk.current_exposure_usd() / self.settings.ACTIVE_CAPITAL_USD * 100.0
            ),
            ws_connected=BotState.ws_connected,
            cycle_latency_ms=round((time.monotonic() - started) * 1000.0, 2),
        )
        with get_session() as s:
            for sig in signals:
                s.add(sig)
            s.add(funnel)

        return {
            "markets_evaluated": markets_evaluated,
            "signals": len(signals),
            "recorded": len(recorded),
            "skips": skips,
        }

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"Motor 2 (consenso sportsbooks) en SHADOW — poll={self.settings.MOTOR_2_POLL_SECONDS}s, "
            f"sports={self.settings.odds_api_sport_keys}, min_books={self.settings.MOTOR_2_MIN_BOOKS}, "
            f"min_edge={self.settings.MOTOR_2_MIN_EDGE_PP}pp, "
            f"anti-fantasma>{self.settings.MOTOR_2_MAX_PLAUSIBLE_EDGE_PP}pp — SIN executor. "
            "Recordatorio a priori: esta tesis perdió -$432 en Kalshi; esto es el re-test."
        )
        while not stop_event.is_set():
            try:
                summary = await self.cycle()
                if summary["recorded"]:
                    logger.info(
                        f"M2 ciclo: evaluados={summary['markets_evaluated']} "
                        f"recorded={summary['recorded']} skips={summary['skips']}"
                    )
            except Exception:
                logger.exception("Error en ciclo del motor 2 (sigo)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.MOTOR_2_POLL_SECONDS
                )
                return
            except TimeoutError:
                continue


def create_service():
    """Factory para el runner. SIN rama de executor: shadow por construcción."""
    engine = ConsensusEngine(get_settings())
    return engine.run
