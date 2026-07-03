"""Tests de src/risk/manager.py — gate F0 exige risk/manager al 100%."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.db.engine import get_session
from src.db.models import DailyPnL, Position, RiskEvent
from src.risk.manager import RiskManager
from src.utils.config import get_settings


@pytest.fixture()
def rm(initialized_db):
    return RiskManager(get_settings())


def _add_daily_pnl(date: str, pnl: float, capital: float = 100.0) -> None:
    with get_session() as s:
        s.add(
            DailyPnL(
                date=date,
                starting_capital=capital,
                ending_capital=capital + pnl,
                pnl=pnl,
                pnl_pct=pnl / capital * 100,
            )
        )


def _today(offset_days: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _add_position(exposure_usd: float, closed: bool = False) -> None:
    with get_session() as s:
        s.add(
            Position(
                condition_id="0xcond",
                token_id="123",
                outcome="YES",
                contracts=exposure_usd,
                avg_price=0.5,
                exposure_usd=exposure_usd,
                strategy="test",
                closed_at=datetime.now(UTC) if closed else None,
            )
        )


class TestEstadoOperacional:
    def test_arranque_limpio(self, rm):
        assert not rm.kill_switch_active()
        assert not rm.is_paused()

    def test_kill_switch_persiste(self, rm, initialized_db):
        rm.activate_kill_switch("test manual")
        assert rm.kill_switch_active()
        # Otra instancia lo ve (persistencia en DB, sobrevive reinicio)
        assert RiskManager(get_settings()).kill_switch_active()

    def test_pause_y_resume(self, rm):
        rm.pause(timedelta(hours=1), "test")
        assert rm.is_paused()
        rm.resume()
        assert not rm.is_paused()

    def test_kill_switch_registra_risk_event(self, rm):
        rm.activate_kill_switch("boom")
        from sqlmodel import select

        with get_session() as s:
            events = s.exec(select(RiskEvent).where(RiskEvent.event_type == "kill_switch")).all()
            assert len(events) == 1
            assert events[0].severity == "critical"


class TestStopLosses:
    def test_sin_perdidas_no_dispara(self, rm):
        _add_daily_pnl(_today(), 1.0)
        assert rm.evaluate_stop_losses() is None

    def test_perdida_diaria_pausa_24h(self, rm):
        # capital default 100 -> -3 USD = -3%
        _add_daily_pnl(_today(), -3.0)
        assert rm.evaluate_stop_losses() == "daily_loss_limit"
        assert rm.is_paused()
        assert not rm.kill_switch_active()

    def test_perdida_semanal_pausa_7d(self, rm):
        for i in range(2, 6):
            _add_daily_pnl(_today(i), -2.0)  # -8 USD en la semana, nada hoy
        assert rm.evaluate_stop_losses() == "weekly_loss_limit"
        assert rm.is_paused()

    def test_perdida_mensual_kill_switch(self, rm):
        for i in range(10, 25):
            _add_daily_pnl(_today(i), -1.0)  # -15 USD en el mes
        assert rm.evaluate_stop_losses() == "monthly_loss_limit"
        assert rm.kill_switch_active()

    def test_perdida_menor_al_limite_no_dispara(self, rm):
        _add_daily_pnl(_today(), -2.9)
        assert rm.evaluate_stop_losses() is None


class TestPreTrade:
    def test_aprueba_trade_normal(self, rm):
        d = rm.check_pre_trade(size_usd=4.0)  # 4% del capital 100
        assert d.approved
        assert d.reason == "ok"

    def test_rechaza_size_sobre_5pct(self, rm):
        d = rm.check_pre_trade(size_usd=5.01)
        assert not d.approved
        assert d.reason == "size_within_limit"

    def test_size_5pct_exacto_pasa(self, rm):
        assert rm.check_pre_trade(size_usd=5.0).approved

    def test_rechaza_exposicion_sobre_25pct(self, rm):
        _add_position(22.0)
        d = rm.check_pre_trade(size_usd=4.0)  # 22 + 4 > 25
        assert not d.approved
        assert d.reason == "exposure_within_limit"

    def test_posiciones_cerradas_no_cuentan(self, rm):
        _add_position(22.0, closed=True)
        assert rm.check_pre_trade(size_usd=4.0).approved

    def test_rechaza_con_kill_switch(self, rm):
        rm.activate_kill_switch("test")
        d = rm.check_pre_trade(size_usd=1.0)
        assert not d.approved
        assert not d.checks["kill_switch_inactive"]

    def test_rechaza_pausado(self, rm):
        rm.pause(timedelta(hours=1), "test")
        d = rm.check_pre_trade(size_usd=1.0)
        assert not d.approved
        assert not d.checks["not_paused"]

    def test_rechaza_size_cero(self, rm):
        assert not rm.check_pre_trade(size_usd=0.0).approved

    def test_stop_loss_bloquea_pre_trade(self, rm):
        _add_daily_pnl(_today(), -3.5)
        d = rm.check_pre_trade(size_usd=1.0)
        assert not d.approved


class TestReservas:
    def test_dry_run_no_reserva(self, rm):
        before = rm.current_exposure_usd()
        d = rm.check_pre_trade(size_usd=4.0, dry_run=True)
        assert d.approved and d.dry_run
        assert rm.current_exposure_usd() == before

    def test_check_and_reserve_reserva(self, rm):
        d = rm.check_and_reserve(size_usd=4.0)
        assert d.approved and not d.dry_run
        assert rm.current_exposure_usd() == pytest.approx(4.0)

    def test_reserva_cuenta_contra_exposicion(self, rm):
        for _ in range(6):
            rm.check_and_reserve(size_usd=4.0)  # 24 reservados
        d = rm.check_and_reserve(size_usd=4.0)  # 28 > 25
        assert not d.approved

    def test_release_libera(self, rm):
        rm.check_and_reserve(size_usd=4.0)
        rm.release_reservation(4.0)
        assert rm.current_exposure_usd() == 0.0

    def test_release_no_baja_de_cero(self, rm):
        rm.release_reservation(99.0)
        assert rm.current_exposure_usd() == 0.0


class TestLimites:
    def test_max_trade_size_usd(self, rm):
        assert rm.max_trade_size_usd() == pytest.approx(5.0)  # 5% de 100

    def test_max_exposure_usd(self, rm):
        assert rm.max_exposure_usd() == pytest.approx(25.0)  # 25% de 100
