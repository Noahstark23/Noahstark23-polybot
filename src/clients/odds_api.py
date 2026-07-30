"""
Cliente async de The Odds API (v4) — la API PAGA del proyecto (Motor 2).

Portado del bot Kalshi con sus dos cicatrices de producción incorporadas:

  1. CACHÉ con TTL por (sport, regions, markets) — incidente 2026-07-19 en
     Kalshi: la cuota mensual de 20k créditos se quemó en días re-pidiendo las
     MISMAS cuotas cada ciclo. Las cuotas de sportsbooks no se mueven por
     segundo; dentro del TTL se sirve de caché sin tocar la red.
  2. BREAKER de cuota — un 401 OUT_OF_USAGE_CREDITS activaba un loop de
     reintentos sin posibilidad de éxito (544 warnings/día). Con el breaker
     activo, get_odds devuelve [] SIN red ni spam de logs; se rearma al vencer
     el cooldown o al cambiar el mes UTC (la cuota mensual resetea).

Ambos estados son DE CLASE: el engine crea un cliente nuevo por ciclo
(`async with`), estado por instancia moriría con cada fetch.

Sin tenacity (no está en las deps de Polybot): retry manual con backoff
exponencial acotado. La API key va por env var (secret de Coolify) y JAMÁS se
loguea. Devuelve modelos tipados; el parseo es fail-safe (evento malformado se
descarta con log, no tira el batch).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from loguru import logger

from src.utils.config import Settings, get_settings


class OddsApiError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"OddsAPI {status_code}: {body[:200]}")


# =====================================================
# Modelos tipados (shape de The Odds API v4)
# =====================================================


@dataclass(frozen=True, slots=True)
class Outcome:
    name: str  # equipo/resultado ("Yankees", "Draw", ...)
    price: float  # cuota DECIMAL (oddsFormat=decimal)


@dataclass(frozen=True, slots=True)
class BookMarket:
    key: str  # "h2h" (moneyline)
    outcomes: tuple[Outcome, ...]


@dataclass(frozen=True, slots=True)
class Bookmaker:
    key: str  # "pinnacle", "draftkings", ...
    markets: tuple[BookMarket, ...]


@dataclass(frozen=True, slots=True)
class OddsEvent:
    id: str
    sport_key: str
    commence_time: datetime  # AWARE, UTC
    home_team: str
    away_team: str
    bookmakers: tuple[Bookmaker, ...]


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)


def parse_event(d: dict[str, Any]) -> OddsEvent | None:
    """Evento crudo → OddsEvent. None si el shape es inválido (fail-safe)."""
    try:
        return OddsEvent(
            id=str(d["id"]),
            sport_key=str(d["sport_key"]),
            commence_time=_parse_dt(d["commence_time"]),
            home_team=str(d["home_team"]),
            away_team=str(d["away_team"]),
            bookmakers=tuple(
                Bookmaker(
                    key=str(bk["key"]),
                    markets=tuple(
                        BookMarket(
                            key=str(mk["key"]),
                            outcomes=tuple(
                                Outcome(name=str(o["name"]), price=float(o["price"]))
                                for o in mk.get("outcomes", [])
                            ),
                        )
                        for mk in bk.get("markets", [])
                    ),
                )
                for bk in d.get("bookmakers", [])
            ),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning(f"odds_api: evento descartado por shape inválido: {exc}")
        return None


class OddsApiClient:
    """Cliente async con caché TTL + breaker de cuota (ambos de clase)."""

    RETRY_ATTEMPTS = 3
    RETRY_BASE_SEC = 2.0
    CACHE_MAX_ENTRIES = 50  # nada sin tope (universo real: 2-3 sport_keys)

    _cache: ClassVar[dict[tuple[str, str], tuple[float, list[OddsEvent]]]] = {}
    _quota_exhausted_at: ClassVar[datetime | None] = None
    quota_remaining: ClassVar[int | None] = None  # header x-requests-remaining

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.ODDS_API_BASE_URL,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OddsApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # =====================================================
    # Breaker de cuota
    # =====================================================

    @classmethod
    def quota_breaker_active(cls, now: datetime | None = None) -> bool:
        if cls._quota_exhausted_at is None:
            return False
        now = now or datetime.now(UTC)
        if now.month != cls._quota_exhausted_at.month or now.year != cls._quota_exhausted_at.year:
            cls._quota_exhausted_at = None  # la cuota mensual resetea con el mes UTC
            logger.info("odds_api: mes UTC nuevo — breaker de cuota rearmado")
            return False
        cooldown = get_settings().ODDS_API_QUOTA_COOLDOWN_SEC
        if (now - cls._quota_exhausted_at).total_seconds() >= cooldown:
            cls._quota_exhausted_at = None
            logger.info("odds_api: cooldown vencido — breaker de cuota rearmado")
            return False
        return True

    @classmethod
    def _trip_quota_breaker(cls) -> None:
        if cls._quota_exhausted_at is None:
            cls._quota_exhausted_at = datetime.now(UTC)
            logger.error(
                "odds_api: CUOTA AGOTADA — breaker activo, sin requests hasta "
                "cooldown o mes nuevo (la API es PAGA: no se martilla)"
            )

    @classmethod
    def reset_class_state_for_testing(cls) -> None:
        cls._cache = {}
        cls._quota_exhausted_at = None
        cls.quota_remaining = None

    # =====================================================
    # Fetch
    # =====================================================

    async def get_odds(self, sport_key: str) -> list[OddsEvent]:
        """
        Cuotas h2h de un deporte. Caché TTL primero, red después. Con el
        breaker activo o sin API key: [] sin tocar la red.
        """
        if not self.settings.ODDS_API_KEY:
            return []  # sin key no hay motor 2 — el engine lo cuenta en el funnel
        if self.quota_breaker_active():
            return []

        cache_key = (sport_key, self.settings.ODDS_API_REGIONS)
        ttl = self.settings.ODDS_API_CACHE_TTL_SEC
        hit = self._cache.get(cache_key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]

        params = {
            "apiKey": self.settings.ODDS_API_KEY,
            "regions": self.settings.ODDS_API_REGIONS,
            "markets": "h2h",
            "oddsFormat": "decimal",
        }
        last_exc: Exception | None = None
        for attempt in range(self.RETRY_ATTEMPTS):
            try:
                resp = await self._client.get(f"/sports/{sport_key}/odds", params=params)
                remaining = resp.headers.get("x-requests-remaining")
                if remaining is not None:
                    with contextlib.suppress(ValueError):
                        type(self).quota_remaining = int(float(remaining))
                if resp.status_code == 401 and "credit" in resp.text.lower():
                    self._trip_quota_breaker()
                    return []
                if resp.status_code == 429:
                    self._trip_quota_breaker()  # rate/cuota: mismo tratamiento, sin martillar
                    return []
                if resp.status_code >= 300:
                    raise OddsApiError(resp.status_code, resp.text)
                events = [e for e in (parse_event(d) for d in resp.json()) if e is not None]
                if len(self._cache) >= self.CACHE_MAX_ENTRIES:
                    self._cache.clear()
                self._cache[cache_key] = (time.monotonic(), events)
                return events
            except (httpx.TimeoutException, httpx.NetworkError, OddsApiError) as exc:
                last_exc = exc
                if isinstance(exc, OddsApiError) and exc.status_code == 401:
                    break  # key inválida: reintentar no lo arregla
                await asyncio.sleep(self.RETRY_BASE_SEC * (2**attempt))

        try:
            from src.api.health import BotState

            BotState.record_error(f"OddsAPI {sport_key}: {type(last_exc).__name__}")
        except Exception:
            pass
        logger.warning(f"odds_api: fetch {sport_key} falló tras retries: {last_exc}")
        return []
