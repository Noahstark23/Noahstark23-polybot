"""Tests de la reconciliación on-chain <-> DB (F3, en seco)."""
from __future__ import annotations

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import Position, RiskEvent
from src.reconcile.service import (
    KEY_RECONCILE_STATUS,
    compare_positions,
    reconcile_once,
)
from src.risk.manager import RiskManager
from src.utils.config import get_settings


class FakeClob:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self):
        return self._positions


def _add_position(token_id: str, contracts: float):
    with get_session() as s:
        s.add(
            Position(
                condition_id="0xc1",
                token_id=token_id,
                outcome="YES",
                contracts=contracts,
                avg_price=0.5,
                exposure_usd=contracts * 0.5,
                strategy="motor_1_arbitrage",
            )
        )


class TestComparePositions:
    def test_match(self):
        assert compare_positions({"111": 4.0}, {"111": 4.0}) == []

    def test_tolerancia(self):
        assert compare_positions({"111": 4.005}, {"111": 4.0}) == []

    def test_mismatch(self):
        diffs = compare_positions({"111": 4.0}, {"111": 2.0})
        assert len(diffs) == 1

    def test_token_solo_en_un_lado(self):
        assert len(compare_positions({"111": 4.0}, {})) == 1
        assert len(compare_positions({}, {"111": 4.0})) == 1


class TestReconcileOnce:
    async def test_limpio_marca_synced(self, initialized_db):
        _add_position("111", 4.0)
        settings = get_settings()
        risk = RiskManager(settings)
        ok = await reconcile_once(FakeClob([{"asset": "111", "size": 4.0}]), risk, settings)
        assert ok is True
        assert RiskManager._get_state(KEY_RECONCILE_STATUS) == "ok"
        with get_session() as s:
            pos = s.exec(select(Position)).one()
            assert pos.on_chain_synced is True

    async def test_discrepancia_pausa_preventiva(self, initialized_db):
        _add_position("111", 4.0)
        settings = get_settings()
        risk = RiskManager(settings)
        ok = await reconcile_once(FakeClob([{"asset": "111", "size": 1.0}]), risk, settings)
        assert ok is False
        assert RiskManager._get_state(KEY_RECONCILE_STATUS) == "mismatch"
        assert risk.is_paused()
        with get_session() as s:
            events = s.exec(
                select(RiskEvent).where(RiskEvent.event_type == "reconcile_mismatch")
            ).all()
            assert len(events) >= 1

    async def test_mismatch_bloquea_no_go(self, initialized_db):
        """La discrepancia deja el checklist NO-GO en rojo."""
        from scripts.check_no_go import _check_reconciliation

        _add_position("111", 4.0)
        settings = get_settings()
        await reconcile_once(FakeClob([]), RiskManager(settings), settings)
        assert _check_reconciliation() is not None

    async def test_wallet_vacio_db_vacia_ok(self, initialized_db):
        settings = get_settings()
        ok = await reconcile_once(FakeClob([]), RiskManager(settings), settings)
        assert ok is True


class TestServiceGating:
    def test_no_arranca_en_shadow(self, initialized_db):
        from src.execution.service import create_service as user_channel_factory
        from src.reconcile.service import create_service as reconciler_factory

        assert reconciler_factory() is None
        assert user_channel_factory() is None
