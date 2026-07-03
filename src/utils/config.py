"""
Configuración centralizada de Polybot.

Toda la configuración viene de variables de entorno (panel de Coolify en deploy,
`.env` en local). Pydantic valida al arranque (fail fast).

Invariantes (ARCHITECTURE.md §9):
    - `TRADING_ENABLED=true` y `POLYMARKET_ENV=production` los pone el humano.
    - Los límites de riesgo tienen cota superior HARDCODED en los Field(le=...):
      el env var puede hacerlos más conservadores, nunca más laxos.
    - La private key del wallet jamás va aquí: sólo la RUTA al secret volume.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cotas superiores hardcoded de riesgo (heredadas del bot Kalshi, NO diluir).
# check_no_go.py y los validators las usan como autoridad.
HARD_MAX_DAILY_LOSS_PCT = 3.0
HARD_MAX_WEEKLY_LOSS_PCT = 8.0
HARD_MAX_MONTHLY_LOSS_PCT = 15.0
HARD_MAX_EXPOSURE_PCT = 25.0
HARD_MAX_TRADE_SIZE_PCT = 5.0
HARD_MAX_KELLY_FRACTION = 0.25
HARD_MAX_CAPITAL_PRODUCTION_USD = 5000.0


class Settings(BaseSettings):
    """Configuración del bot validada al arranque."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # === Entorno / venue ===
    POLYMARKET_ENV: Literal["paper", "production"] = "paper"
    POLYGON_CHAIN_ID: int = 137
    CLOB_API_URL: str = "https://clob.polymarket.com"
    CLOB_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com"

    # === Auth EVM (Polygon) ===
    # Sólo dirección pública y RUTA al keystore (secret volume). Nunca la key.
    POLY_WALLET_ADDRESS: str = "0x0000000000000000000000000000000000000000"
    POLY_SIGNER_KEY_PATH: Path | None = None
    # Password del keystore cifrado (si el secret volume trae JSON keystore)
    POLY_KEYSTORE_PASSWORD: str = ""
    # Credenciales L2 del CLOB (se derivan de la firma del wallet en F1)
    CLOB_API_KEY: str = ""
    CLOB_SECRET: str = ""
    CLOB_PASSPHRASE: str = ""
    USDC_CONTRACT: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    # === Storage ===
    DATABASE_URL: str = "sqlite:////app/data/polybot.db"

    # === Health server (dentro del contenedor; host mapea a :18081) ===
    HEALTH_HOST: str = "0.0.0.0"
    HEALTH_PORT: int = Field(8080, gt=0, le=65535)

    # === Logging ===
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOGS_DIR: Path = Path("/app/logs")

    # === Interruptor maestro (arranca APAGADO; lo enciende el humano) ===
    TRADING_ENABLED: bool = False

    # === Motores / servicios ===
    MOTOR_1_ARBITRAGE_ENABLED: bool = True  # en F2 corre en SHADOW aunque esté true
    DATA_CAPTURE_ENABLED: bool = True
    # Lista de condition_ids a observar (CSV). Vacío => descubrir vía get_markets.
    WATCHED_CONDITION_IDS: str = ""
    MAX_WATCHED_MARKETS: int = Field(20, gt=0, le=200)
    ENGINE_TICK_SECONDS: float = Field(2.0, gt=0.1, le=60)
    RECONCILE_INTERVAL_SECONDS: int = Field(300, ge=30, le=3600)

    # === Riesgo (cota superior hardcoded — se puede bajar por env, no subir) ===
    MAX_DAILY_LOSS_PCT: float = Field(HARD_MAX_DAILY_LOSS_PCT, gt=0, le=HARD_MAX_DAILY_LOSS_PCT)
    MAX_WEEKLY_LOSS_PCT: float = Field(HARD_MAX_WEEKLY_LOSS_PCT, gt=0, le=HARD_MAX_WEEKLY_LOSS_PCT)
    MAX_MONTHLY_LOSS_PCT: float = Field(
        HARD_MAX_MONTHLY_LOSS_PCT, gt=0, le=HARD_MAX_MONTHLY_LOSS_PCT
    )
    MAX_SIMULTANEOUS_EXPOSURE_PCT: float = Field(
        HARD_MAX_EXPOSURE_PCT, gt=0, le=HARD_MAX_EXPOSURE_PCT
    )
    MAX_TRADE_SIZE_PCT: float = Field(HARD_MAX_TRADE_SIZE_PCT, gt=0, le=HARD_MAX_TRADE_SIZE_PCT)
    KELLY_FRACTION: float = Field(HARD_MAX_KELLY_FRACTION, gt=0, le=HARD_MAX_KELLY_FRACTION)
    MIN_EDGE_PCT: float = Field(1.0, gt=0, le=20)
    # Filtro anti-edge-fantasma: edge mayor a esto se loguea y NO se ejecuta.
    MIN_EDGE_PCT_MAX: float = Field(10.0, gt=0, le=50)
    MIN_LIQUIDITY_CONTRACTS: int = Field(10, gt=0)
    FEE_RATE_BPS: int = Field(0, ge=0, le=1000)  # fee del CLOB (hoy 0 en la mayoría)

    # === Capital ===
    ACTIVE_CAPITAL_USD: float = Field(100.0, gt=0, le=100_000)
    DYNAMIC_CAPITAL_ENABLED: bool = True
    CAPITAL_SAFETY_FACTOR_PCT: float = Field(90.0, gt=0, le=100)
    CAPITAL_FLOOR_USD: float = Field(50.0, gt=0)
    CAPITAL_CAP_USD: float = Field(1000.0, gt=0)
    CAPITAL_SMOOTHING_PCT: float = Field(20.0, gt=0, le=100)
    BALANCE_REFRESH_SECONDS: int = Field(60, ge=10)

    # === Alertas / observabilidad ===
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_DASHBOARD_ENABLED: bool = True
    TELEGRAM_DASHBOARD_INTERVAL_SEC: int = Field(3600, ge=60)
    SENTRY_DSN: str = ""

    # ---- Validators ----

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        if (
            self.POLYMARKET_ENV == "production"
            and self.ACTIVE_CAPITAL_USD > HARD_MAX_CAPITAL_PRODUCTION_USD
        ):
            raise ValueError(
                    f"ACTIVE_CAPITAL_USD={self.ACTIVE_CAPITAL_USD} excede el límite de "
                    f"seguridad (${HARD_MAX_CAPITAL_PRODUCTION_USD:.0f}) en production."
                )
        if self.TRADING_ENABLED:
            if self.POLYMARKET_ENV != "production":
                raise ValueError(
                    "TRADING_ENABLED=true requiere POLYMARKET_ENV=production. "
                    "En paper el trading real no existe."
                )
            if int(self.POLY_WALLET_ADDRESS, 16) == 0:
                raise ValueError("TRADING_ENABLED=true requiere POLY_WALLET_ADDRESS real.")
            if self.POLY_SIGNER_KEY_PATH is None:
                raise ValueError(
                    "TRADING_ENABLED=true requiere POLY_SIGNER_KEY_PATH (secret volume)."
                )
        return self

    # ---- Properties ----

    @property
    def is_production(self) -> bool:
        return self.POLYMARKET_ENV == "production"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    @property
    def watched_condition_ids(self) -> list[str]:
        return [c.strip() for c in self.WATCHED_CONDITION_IDS.split(",") if c.strip()]


# Lazy singleton
_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton de Settings (instancia única en runtime)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_testing() -> None:
    """Sólo para tests — fuerza re-lectura del entorno."""
    global _settings
    _settings = None
