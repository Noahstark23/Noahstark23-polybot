"""Tests de src/utils/config.py — gate F0 exige config al 100%."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import src.utils.config as config_module
from src.utils.config import (
    HARD_MAX_CAPITAL_PRODUCTION_USD,
    HARD_MAX_DAILY_LOSS_PCT,
    Settings,
)


def _make(**overrides) -> Settings:
    """Settings sin leer .env del repo."""
    return Settings(_env_file=None, **overrides)


class TestDefaults:
    def test_defaults_seguros(self):
        s = _make()
        assert s.POLYMARKET_ENV == "paper"
        assert s.TRADING_ENABLED is False
        assert s.POLYGON_CHAIN_ID == 137
        assert s.is_production is False

    def test_riesgo_hereda_limites_kalshi(self):
        s = _make()
        assert s.MAX_DAILY_LOSS_PCT == 3.0
        assert s.MAX_WEEKLY_LOSS_PCT == 8.0
        assert s.MAX_MONTHLY_LOSS_PCT == 15.0
        assert s.MAX_SIMULTANEOUS_EXPOSURE_PCT == 25.0
        assert s.MAX_TRADE_SIZE_PCT == 5.0
        assert s.KELLY_FRACTION == 0.25

    def test_anti_fantasma_configurado(self):
        s = _make()
        assert s.MIN_EDGE_PCT_MAX == 10.0
        assert s.MIN_EDGE_PCT < s.MIN_EDGE_PCT_MAX


class TestLimitesNoDiluibles:
    """Los env vars pueden hacer el riesgo MÁS conservador, nunca más laxo."""

    def test_no_se_puede_subir_daily_loss(self):
        with pytest.raises(ValidationError):
            _make(MAX_DAILY_LOSS_PCT=HARD_MAX_DAILY_LOSS_PCT + 0.1)

    def test_no_se_puede_subir_exposure(self):
        with pytest.raises(ValidationError):
            _make(MAX_SIMULTANEOUS_EXPOSURE_PCT=26.0)

    def test_no_se_puede_subir_trade_size(self):
        with pytest.raises(ValidationError):
            _make(MAX_TRADE_SIZE_PCT=5.1)

    def test_no_se_puede_subir_kelly(self):
        with pytest.raises(ValidationError):
            _make(KELLY_FRACTION=0.3)

    def test_si_se_puede_bajar(self):
        s = _make(MAX_DAILY_LOSS_PCT=1.0, MAX_TRADE_SIZE_PCT=2.0)
        assert s.MAX_DAILY_LOSS_PCT == 1.0
        assert s.MAX_TRADE_SIZE_PCT == 2.0


class TestGuardarrailesProduction:
    def test_capital_tope_5k_en_production(self):
        with pytest.raises(ValidationError, match="límite de"):
            _make(POLYMARKET_ENV="production", ACTIVE_CAPITAL_USD=5001)

    def test_capital_5k_exacto_pasa(self):
        s = _make(POLYMARKET_ENV="production", ACTIVE_CAPITAL_USD=HARD_MAX_CAPITAL_PRODUCTION_USD)
        assert s.is_production

    def test_capital_alto_en_paper_pasa(self):
        s = _make(POLYMARKET_ENV="paper", ACTIVE_CAPITAL_USD=50_000)
        assert s.ACTIVE_CAPITAL_USD == 50_000

    def test_trading_enabled_exige_production(self):
        with pytest.raises(ValidationError, match="POLYMARKET_ENV=production"):
            _make(TRADING_ENABLED=True, POLYMARKET_ENV="paper")

    def test_trading_enabled_exige_wallet_real(self):
        with pytest.raises(ValidationError, match="POLY_WALLET_ADDRESS"):
            _make(TRADING_ENABLED=True, POLYMARKET_ENV="production")

    def test_trading_enabled_exige_signer_key_path(self, tmp_path):
        with pytest.raises(ValidationError, match="POLY_SIGNER_KEY_PATH"):
            _make(
                TRADING_ENABLED=True,
                POLYMARKET_ENV="production",
                POLY_WALLET_ADDRESS="0x" + "1" * 40,
            )

    def test_trading_enabled_valido(self, tmp_path):
        s = _make(
            TRADING_ENABLED=True,
            POLYMARKET_ENV="production",
            POLY_WALLET_ADDRESS="0x" + "1" * 40,
            POLY_SIGNER_KEY_PATH=tmp_path / "key.json",
        )
        assert s.TRADING_ENABLED


class TestEnvInvalido:
    def test_env_invalido_falla(self):
        with pytest.raises(ValidationError):
            _make(POLYMARKET_ENV="demo")

    def test_log_level_invalido_falla(self):
        with pytest.raises(ValidationError):
            _make(LOG_LEVEL="TRACE")


class TestHelpers:
    def test_singleton(self, isolated_env):
        a = config_module.get_settings()
        b = config_module.get_settings()
        assert a is b

    def test_reset_para_tests(self, isolated_env):
        a = config_module.get_settings()
        config_module.reset_settings_for_testing()
        assert config_module.get_settings() is not a

    def test_watched_condition_ids_parsea_csv(self):
        s = _make(WATCHED_CONDITION_IDS="0xabc, 0xdef ,")
        assert s.watched_condition_ids == ["0xabc", "0xdef"]

    def test_telegram_configured(self):
        assert not _make().telegram_configured
        assert _make(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c").telegram_configured
