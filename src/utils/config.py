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

from pydantic import Field, field_validator, model_validator
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

    @field_validator("CLOB_API_URL", mode="before")
    @classmethod
    def _normalize_api_url(cls, v: str) -> str:
        """
        Tolera el typo clásico del panel de env vars: URL sin esquema
        ("clob.polymarket.com") — httpx lanza UnsupportedProtocol y el data
        capture muere en loop. Se normaliza a https:// y se valida acá,
        fail-fast en el boot en lugar de fallar en runtime.
        """
        v = str(v).strip().rstrip("/")
        if not v:
            raise ValueError("CLOB_API_URL vacío")
        if v.startswith(("ws://", "wss://")):
            raise ValueError(f"CLOB_API_URL es el endpoint REST, no el WS: {v}")
        if not v.startswith(("http://", "https://")):
            v = f"https://{v}"
        return v

    @field_validator("CLOB_WS_URL", mode="before")
    @classmethod
    def _normalize_ws_url(cls, v: str) -> str:
        """Idéntico para el WS: sin esquema -> wss://; http(s):// es error."""
        v = str(v).strip().rstrip("/")
        if not v:
            raise ValueError("CLOB_WS_URL vacío")
        if v.startswith(("http://", "https://")):
            raise ValueError(f"CLOB_WS_URL es el endpoint WS, no el REST: {v}")
        if not v.startswith(("ws://", "wss://")):
            v = f"wss://{v}"
        return v

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
    # Motor 2: consenso de sportsbooks vía The Odds API (la API PAGA del proyecto,
    # la misma que usa el M2 del bot Kalshi). SHADOW puro — sin executor en el repo.
    # Default OFF. ADVERTENCIA escrita a priori: esta MISMA tesis perdió -$432
    # reales en Kalshi (edge techo 0.15pp vs umbral 3pp); el shadow acá es el
    # re-test barato en otro venue, no una apuesta.
    MOTOR_2_CONSENSUS_ENABLED: bool = False
    MOTOR_2_POLL_SECONDS: float = Field(300.0, ge=60, le=3600)  # cuota paga: sin martillar
    MOTOR_2_MIN_BOOKS: int = Field(3, ge=1, le=20)  # books mínimos para un consenso válido
    MOTOR_2_MIN_EDGE_PP: float = Field(3.0, gt=0, le=20)  # umbral de señal, en pp de prob.
    # Anti-fantasma del M2 (heredado de Kalshi: MAX_PLAUSIBLE_EDGE=15pp): un "edge"
    # de consenso mayor a esto es partido emparejado MAL o cuotas stale, no señal.
    MOTOR_2_MAX_PLAUSIBLE_EDGE_PP: float = Field(15.0, gt=0, le=50)
    # Motor 3: arbitraje multi-outcome sobre eventos neg-risk (SHADOW puro — no
    # existe executor de M3 en el repo). Default OFF: mergear no cambia nada; se
    # enciende por env var en Coolify (mismo patrón que el pivote de universo).
    # Requiere MARKET_DISCOVERY_SOURCE=neg_risk para tener grupos que evaluar.
    MOTOR_3_NEG_RISK_ENABLED: bool = False
    MOTOR_3_MIN_LEGS: int = Field(3, ge=2, le=30)  # 2 patas neg-risk = un binario: territorio M1
    MOTOR_3_TICK_SECONDS: float = Field(5.0, gt=0.5, le=60)
    DATA_CAPTURE_ENABLED: bool = True
    ANALYST_ENABLED: bool = True  # analyst_loop (§7) — veredicto diario
    # Lista de condition_ids a observar (CSV). Vacío => descubrir vía get_markets.
    WATCHED_CONDITION_IDS: str = ""
    MAX_WATCHED_MARKETS: int = Field(20, gt=0, le=200)
    # Universo de discovery (decisión 2026-07-23 tras F2 rojo: 0 edges brutos
    # en 585k snapshots del universo sampling — mercados con rewards = los más
    # eficientes). "all_recent" observa el long-tail: binarios activos más
    # recientes de /markets, excluyendo los del set sampling.
    # "neg_risk" observa GRUPOS multi-outcome (eventos neg-risk con >= MOTOR_3_MIN_LEGS
    # patas) — es el universo del Motor 3; Motor 1 igual evalúa cada pata binaria.
    MARKET_DISCOVERY_SOURCE: Literal["sampling", "all_recent", "neg_risk"] = "sampling"
    DISCOVERY_MAX_PAGES: int = Field(200, gt=0, le=1000)
    ENGINE_TICK_SECONDS: float = Field(2.0, gt=0.1, le=60)
    RECONCILE_INTERVAL_SECONDS: int = Field(300, ge=30, le=3600)

    # === Mantenimiento / retención (lección "nada sin tope" — incidente 2026-07-11) ===
    MAINTENANCE_ENABLED: bool = True
    MAINTENANCE_INTERVAL_SECONDS: int = Field(3600, ge=60)
    RETENTION_ORDERBOOK_EVENTS_DAYS: int = Field(14, ge=1)
    RETENTION_MARKET_SNAPSHOTS_DAYS: int = Field(30, ge=1)
    RETENTION_FUNNEL_SNAPSHOTS_DAYS: int = Field(90, ge=7)
    RETENTION_MULTI_EDGE_WINDOWS_DAYS: int = Field(90, ge=7)
    RETENTION_CONSENSUS_SIGNALS_DAYS: int = Field(90, ge=7)

    # === The Odds API (Motor 2 — API PAGA, cuota mensual limitada) ===
    ODDS_API_KEY: str = ""  # secret en Coolify; JAMÁS en el repo ni en logs
    ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"
    ODDS_API_SPORT_KEYS: str = "baseball_mlb"  # CSV; parseado por la property (ver abajo)
    ODDS_API_REGIONS: str = "us,eu"
    ODDS_API_CACHE_TTL_SEC: float = Field(240.0, ge=0)  # anti-quema de créditos
    ODDS_API_QUOTA_COOLDOWN_SEC: float = Field(21600.0, ge=60)  # 6h sin red tras agotar cuota
    # Disco libre mínimo antes de podar agresivo (telemetría se sacrifica,
    # la captura/detección NUNCA se gatea)
    DISK_MIN_FREE_GB: float = Field(2.0, gt=0)
    DISK_WARN_FREE_GB: float = Field(5.0, gt=0)
    # last_error deja de mostrarse en /status pasado este TTL (sticky enmascara)
    LAST_ERROR_TTL_SECONDS: int = Field(21600, ge=60)  # 6h

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

    @property
    def odds_api_sport_keys(self) -> list[str]:
        """
        CSV parseado CON strip por elemento. Bug real del bot Kalshi (runbook
        PASO 0 #5): "baseball_mlb ,soccer_x" con espacio antes de la coma dejaba
        el espacio DENTRO del valor porque el str se usaba plano — acá el panel
        de Coolify puede escribir lo que quiera y el valor sale limpio igual.
        """
        return [k.strip() for k in self.ODDS_API_SPORT_KEYS.split(",") if k.strip()]


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
