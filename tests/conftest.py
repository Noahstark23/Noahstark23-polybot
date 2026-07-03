"""Fixtures compartidos: entorno aislado + DB sqlite temporal por test."""
from __future__ import annotations

import pytest

import src.db.engine as db_engine
import src.utils.config as config_module


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """
    Entorno limpio: DB temporal, logs temporales, sin .env del repo.
    Resetea los singletons de Settings y engine.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("POLYMARKET_ENV", "paper")
    monkeypatch.setenv("TRADING_ENABLED", "false")
    config_module.reset_settings_for_testing()
    db_engine.reset_engine_for_testing()
    yield monkeypatch
    config_module.reset_settings_for_testing()
    db_engine.reset_engine_for_testing()


@pytest.fixture()
def initialized_db(isolated_env):
    """DB temporal con tablas creadas."""
    db_engine.init_db()
    return isolated_env


@pytest.fixture()
def settings(isolated_env):
    return config_module.get_settings()
