"""
Tests del Executor F3 EN SECO (gate F3 se valida en vivo; acá se prueba que:
el guard bloquea en shadow, el happy path postea 2 piernas, un fallo de pierna
cancela la otra y libera la reserva, y los fills del user channel actualizan
Trade/Position).
"""
from __future__ import annotations

import pytest
from sqlmodel import select

import src.db.engine as db_engine
import src.utils.config as config_module
from src.db.engine import get_session
from src.db.models import Position, Trade
from src.motor_1_arbitrage.executor import ArbExecutor, handle_user_event
from src.utils.config import Settings

OPP = {
    "condition_id": "0xc1",
    "token_id_yes": "111",
    "token_id_no": "222",
    "ask_yes": 0.48,
    "ask_no": 0.49,
    "size_contracts": 4.0,
    "net_edge_pct": 2.5,
}


class FakeClob:
    """Doble del cliente CLOB: firma y postea en memoria."""

    def __init__(self, fail_on_leg: int | None = None):
        self.posted: list[dict] = []
        self.cancelled: list[str] = []
        self.fail_on_leg = fail_on_leg

    def build_order(self, token_id, price, size, side, **kw):
        return {"tokenId": token_id, "price": price, "size": size, "side": side}

    async def post_order(self, signed, order_type="GTC"):
        if self.fail_on_leg is not None and len(self.posted) == self.fail_on_leg:
            raise RuntimeError("boom en el CLOB")
        self.posted.append(signed)
        return {"orderID": f"ex-{len(self.posted)}"}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": [order_id]}


@pytest.fixture()
def live_settings(tmp_path, isolated_env):
    """Settings con trading encendido (como lo pondría el humano en Coolify)."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1".zfill(64))
    s = Settings(
        _env_file=None,
        DATABASE_URL=config_module.get_settings().DATABASE_URL,
        TRADING_ENABLED=True,
        POLYMARKET_ENV="production",
        POLY_WALLET_ADDRESS="0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf",
        POLY_SIGNER_KEY_PATH=key_file,
        ACTIVE_CAPITAL_USD=100.0,
    )
    db_engine.init_db()
    return s


class TestGuards:
    async def test_no_op_en_shadow(self, initialized_db):
        """Con TRADING_ENABLED=false el executor es un no-op absoluto."""
        settings = config_module.get_settings()
        ex = ArbExecutor(settings, clob=FakeClob())
        assert await ex.execute(OPP) is False
        assert ex.clob.posted == []
        with get_session() as s:
            assert s.exec(select(Trade)).all() == []

    async def test_risk_rechaza_no_postea(self, live_settings):
        clob = FakeClob()
        ex = ArbExecutor(live_settings, clob=clob)
        ex.risk.activate_kill_switch("test")
        assert await ex.execute(OPP) is False
        assert clob.posted == []


class TestHappyPath:
    async def test_postea_dos_piernas_y_persiste(self, live_settings):
        clob = FakeClob()
        ex = ArbExecutor(live_settings, clob=clob)
        assert await ex.execute(OPP) is True
        assert len(clob.posted) == 2
        with get_session() as s:
            trades = s.exec(select(Trade)).all()
            assert len(trades) == 2
            assert {t.outcome for t in trades} == {"YES", "NO"}
            assert all(t.status == "placed" for t in trades)
            assert all(t.exchange_order_id for t in trades)
        # reserva viva hasta el fill
        assert ex.risk.current_exposure_usd() == pytest.approx(4.0 * 0.97)


class TestRollback:
    async def test_fallo_segunda_pierna_cancela_primera(self, live_settings):
        clob = FakeClob(fail_on_leg=1)  # la 2da pierna explota
        ex = ArbExecutor(live_settings, clob=clob)
        assert await ex.execute(OPP) is False
        assert clob.cancelled == ["ex-1"]
        assert ex.risk.current_exposure_usd() == 0.0  # reserva liberada
        with get_session() as s:
            trades = s.exec(select(Trade)).all()
            assert all(t.status == "error" for t in trades)


class TestUserChannel:
    async def test_fill_actualiza_trade_y_posicion(self, live_settings):
        clob = FakeClob()
        ex = ArbExecutor(live_settings, clob=clob)
        await ex.execute(OPP)

        handle_user_event(
            {"event_type": "trade", "taker_order_id": "ex-1", "price": "0.48", "size": "4"},
            risk=ex.risk,
        )
        with get_session() as s:
            trade = s.exec(select(Trade).where(Trade.exchange_order_id == "ex-1")).one()
            assert trade.status == "filled"
            assert trade.fill_price == 0.48
            pos = s.exec(select(Position)).one()
            assert pos.contracts == 4.0
            assert pos.exposure_usd == pytest.approx(1.92)
            assert pos.on_chain_synced is False

    async def test_fill_parcial_no_cierra(self, live_settings):
        ex = ArbExecutor(live_settings, clob=FakeClob())
        await ex.execute(OPP)
        handle_user_event(
            {"event_type": "trade", "taker_order_id": "ex-1", "price": "0.48", "size": "1"}
        )
        with get_session() as s:
            trade = s.exec(select(Trade).where(Trade.exchange_order_id == "ex-1")).one()
            assert trade.status == "placed"  # parcial
            assert trade.fill_size == 1.0

    async def test_cancelacion_marca_trade(self, live_settings):
        ex = ArbExecutor(live_settings, clob=FakeClob())
        await ex.execute(OPP)
        handle_user_event({"event_type": "order", "id": "ex-2", "status": "CANCELLATION"})
        with get_session() as s:
            trade = s.exec(select(Trade).where(Trade.exchange_order_id == "ex-2")).one()
            assert trade.status == "cancelled"

    def test_fill_desconocido_no_rompe(self, initialized_db):
        handle_user_event(
            {"event_type": "trade", "taker_order_id": "nope", "price": "0.5", "size": "1"}
        )  # no lanza
