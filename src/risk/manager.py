"""
RiskManager — portado del bot base (ARCHITECTURE.md §2). Opera en USD, agnóstico al venue.

Reglas duras heredadas (NO diluir sin aprobación humana explícita):
    - Stop-loss diario  -3%  -> pausa 24h
    - Stop-loss semanal -8%  -> pausa 7 días
    - Stop-loss mensual -15% -> kill-switch total (sólo lo limpia el humano)
    - Exposición simultánea máxima: 25% del capital activo
    - Sizing máximo por trade: 5% del capital (¼ Kelly)

Modos:
    - dry_run=True (F2 shadow): evalúa y registra la decisión, NO reserva capital.
    - dry_run=False (F3+): `check_and_reserve()` aprueba y reserva exposición.

El estado de pausas y kill-switch vive en `operational_state` (DB), así sobrevive
reinicios del contenedor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import select

from src.db.engine import get_session
from src.db.models import DailyPnL, OperationalState, Position, RiskEvent, utc_now
from src.utils.config import Settings, get_settings

KEY_KILL_SWITCH = "kill_switch"
KEY_PAUSED_UNTIL = "paused_until"
KEY_PAUSE_REASON = "pause_reason"

PAUSE_DAILY = timedelta(hours=24)
PAUSE_WEEKLY = timedelta(days=7)


@dataclass
class RiskDecision:
    """Resultado de una evaluación pre-trade."""

    approved: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)
    dry_run: bool = True
    max_size_usd: float = 0.0


class RiskManager:
    """Gate central de riesgo. Toda orden (shadow o real) pasa por aquí."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._reserved_usd: float = 0.0  # exposición reservada aún no persistida

    # ==================================================
    # Estado operacional (DB-backed, sobrevive reinicios)
    # ==================================================

    @staticmethod
    def _get_state(key: str) -> str | None:
        with get_session() as s:
            row = s.exec(select(OperationalState).where(OperationalState.key == key)).first()
            return row.value if row else None

    @staticmethod
    def _set_state(key: str, value: str) -> None:
        with get_session() as s:
            row = s.exec(select(OperationalState).where(OperationalState.key == key)).first()
            if row is None:
                row = OperationalState(key=key, value=value)
            else:
                row.value = value
                row.updated_at = utc_now()
            s.add(row)

    def kill_switch_active(self) -> bool:
        return self._get_state(KEY_KILL_SWITCH) == "active"

    def is_paused(self) -> bool:
        raw = self._get_state(KEY_PAUSED_UNTIL)
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return False
        return datetime.now(UTC) < until

    def pause_reason(self) -> str | None:
        return self._get_state(KEY_PAUSE_REASON)

    def activate_kill_switch(self, reason: str) -> None:
        """Kill-switch total. SOLO lo limpia el humano (clear_kill_switch.py)."""
        self._set_state(KEY_KILL_SWITCH, "active")
        self._set_state(KEY_PAUSE_REASON, reason)
        self._record_event("kill_switch", "critical", reason)
        logger.critical(f"🛑 KILL-SWITCH ACTIVADO: {reason}")

    def pause(self, duration: timedelta, reason: str, event_type: str = "manual_pause") -> None:
        until = datetime.now(UTC) + duration
        self._set_state(KEY_PAUSED_UNTIL, until.isoformat())
        self._set_state(KEY_PAUSE_REASON, reason)
        self._record_event(event_type, "warning", f"{reason} (hasta {until.isoformat()})")
        logger.warning(f"⏸️  Trading pausado hasta {until.isoformat()}: {reason}")

    def resume(self) -> None:
        self._set_state(KEY_PAUSED_UNTIL, "")
        self._set_state(KEY_PAUSE_REASON, "")
        self._record_event("manual_resume", "info", "Pausa levantada manualmente")

    def _record_event(self, event_type: str, severity: str, message: str) -> None:
        with get_session() as s:
            s.add(
                RiskEvent(
                    event_type=event_type,
                    severity=severity,
                    message=message[:1000],
                    capital_at_event=self.settings.ACTIVE_CAPITAL_USD,
                )
            )

    # ==================================================
    # Exposición y PnL
    # ==================================================

    def current_exposure_usd(self) -> float:
        """Exposición viva: posiciones abiertas + reservas en vuelo."""
        with get_session() as s:
            positions = s.exec(select(Position).where(Position.closed_at == None)).all()  # noqa: E711
            open_exposure = sum(p.exposure_usd for p in positions)
        return open_exposure + self._reserved_usd

    @staticmethod
    def _pnl_since(cutoff_date: str) -> float:
        """PnL realizado acumulado desde una fecha (YYYY-MM-DD, inclusive)."""
        with get_session() as s:
            rows = s.exec(select(DailyPnL).where(DailyPnL.date >= cutoff_date)).all()
            return sum(r.pnl for r in rows)

    def _loss_pct_since(self, days: int) -> float:
        """Pérdida (%) sobre capital activo en los últimos `days` días. >0 = pérdida."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        pnl = self._pnl_since(cutoff)
        if pnl >= 0:
            return 0.0
        return abs(pnl) / self.settings.ACTIVE_CAPITAL_USD * 100.0

    # ==================================================
    # Stop-loss multi-timeframe
    # ==================================================

    def evaluate_stop_losses(self) -> str | None:
        """
        Evalúa los tres stop-loss. Si alguno dispara, aplica la acción
        correspondiente y devuelve el event_type. None si todo dentro de rango.
        Orden: mensual (kill) > semanal > diario.
        """
        monthly = self._loss_pct_since(30)
        if monthly >= self.settings.MAX_MONTHLY_LOSS_PCT:
            self.activate_kill_switch(
                f"Stop-loss mensual: -{monthly:.2f}% >= {self.settings.MAX_MONTHLY_LOSS_PCT}%"
            )
            return "monthly_loss_limit"

        weekly = self._loss_pct_since(7)
        if weekly >= self.settings.MAX_WEEKLY_LOSS_PCT:
            self.pause(
                PAUSE_WEEKLY,
                f"Stop-loss semanal: -{weekly:.2f}% >= {self.settings.MAX_WEEKLY_LOSS_PCT}%",
                event_type="weekly_loss_limit",
            )
            return "weekly_loss_limit"

        daily = self._loss_pct_since(1)
        if daily >= self.settings.MAX_DAILY_LOSS_PCT:
            self.pause(
                PAUSE_DAILY,
                f"Stop-loss diario: -{daily:.2f}% >= {self.settings.MAX_DAILY_LOSS_PCT}%",
                event_type="daily_loss_limit",
            )
            return "daily_loss_limit"

        return None

    # ==================================================
    # Pre-trade gate
    # ==================================================

    def max_trade_size_usd(self) -> float:
        return self.settings.ACTIVE_CAPITAL_USD * self.settings.MAX_TRADE_SIZE_PCT / 100.0

    def max_exposure_usd(self) -> float:
        return (
            self.settings.ACTIVE_CAPITAL_USD
            * self.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT
            / 100.0
        )

    def check_pre_trade(self, size_usd: float, dry_run: bool = True) -> RiskDecision:
        """
        Evalúa si un trade de `size_usd` pasa todos los límites.
        En dry_run registra la decisión sin reservar capital (F2 shadow).
        """
        checks: dict[str, bool] = {}

        checks["kill_switch_inactive"] = not self.kill_switch_active()
        checks["not_paused"] = not self.is_paused()

        stop = self.evaluate_stop_losses() if checks["kill_switch_inactive"] else None
        checks["stop_losses_ok"] = stop is None and checks["kill_switch_inactive"]

        checks["size_within_limit"] = size_usd <= self.max_trade_size_usd() + 1e-9

        exposure_after = self.current_exposure_usd() + size_usd
        checks["exposure_within_limit"] = exposure_after <= self.max_exposure_usd() + 1e-9

        checks["size_positive"] = size_usd > 0

        approved = all(checks.values())
        reason = "ok" if approved else next(k for k, v in checks.items() if not v)

        decision = RiskDecision(
            approved=approved,
            reason=reason,
            checks=checks,
            dry_run=dry_run,
            max_size_usd=self.max_trade_size_usd(),
        )
        if not approved:
            logger.debug(f"Risk check rechazado ({reason}) size=${size_usd:.2f} dry_run={dry_run}")
        return decision

    def check_and_reserve(self, size_usd: float) -> RiskDecision:
        """
        F3+: gate real. Si aprueba, reserva la exposición hasta que el executor
        confirme (release) o el fill se persista como Position.
        """
        decision = self.check_pre_trade(size_usd, dry_run=False)
        if decision.approved:
            self._reserved_usd += size_usd
        return decision

    def release_reservation(self, size_usd: float) -> None:
        """Libera exposición reservada (orden cancelada/error o fill persistido)."""
        self._reserved_usd = max(0.0, self._reserved_usd - size_usd)
