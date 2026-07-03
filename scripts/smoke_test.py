"""
Smoke test — verificación de que la infra F0 está sana (ARCHITECTURE.md §6 F0).

Uso:
    python -m scripts.smoke_test

Verifica:
    1. Settings cargan sin errores
    2. DB inicializa (crea tablas)
    3. DB round-trip (escribir + leer una fila)
    4. /health y /status responden 200 (TestClient, sin abrir puerto)
    5. CLOB read-only: conecta, lista >=1 mercado, lee 1 orderbook
       (si no hay salida a internet: check SKIPPED con warning — en CI y en el
        droplet corre completo)

Exit 0 si todo pasa, 1 si algún check falló.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
from loguru import logger

from src.api.health import BotState, app
from src.clients.polymarket_clob import PolymarketClobClient
from src.db.engine import get_session, init_db
from src.db.models import BotRun
from src.utils.config import get_settings
from src.utils.logging import setup_logging


async def check_clob_readonly() -> bool | None:
    """
    Conecta al CLOB público, lista mercados y lee un orderbook.
    Devuelve True (ok), False (falló con red), None (sin red -> SKIP).
    """
    settings = get_settings()
    try:
        async with PolymarketClobClient(settings) as clob:
            markets = await clob.get_sampling_markets()
            data = markets.get("data") or []
            if not data:
                markets = await clob.get_markets()
                data = markets.get("data") or []
            assert len(data) >= 1, "get_markets devolvió 0 mercados"
            logger.success(f"✓ CLOB conectado — {len(data)} mercados en la primera página")

            # Buscar un token para leer su orderbook
            token_id = None
            for m in data:
                for tok in m.get("tokens") or []:
                    if tok.get("token_id"):
                        token_id = tok["token_id"]
                        break
                if token_id:
                    break
            assert token_id, "ningún mercado trajo token_id"
            book = await clob.get_orderbook(token_id)
            assert "bids" in book or "asks" in book, f"orderbook sin bids/asks: {book}"
            n_bids = len(book.get("bids") or [])
            n_asks = len(book.get("asks") or [])
            logger.success(f"✓ Orderbook leído (token={token_id[:16]}…, {n_bids} bids / {n_asks} asks)")
            return True
    except (httpx.TransportError, httpx.ProxyError) as exc:
        logger.warning(
            f"⚠ CLOB inalcanzable desde este entorno ({type(exc).__name__}) — check SKIPPED. "
            "En CI y en el droplet este check corre completo."
        )
        return None
    except Exception:
        logger.exception("✗ CLOB read-only falló")
        return False


async def run_smoke_test() -> int:
    setup_logging()

    logger.info("=" * 60)
    logger.info("SMOKE TEST — Polybot")
    logger.info("=" * 60)

    # === 1. Settings ===
    try:
        settings = get_settings()
        logger.success(f"✓ Settings cargados (env={settings.POLYMARKET_ENV})")
        logger.info(f"  Capital: ${settings.ACTIVE_CAPITAL_USD}")
        logger.info(f"  Trading enabled: {settings.TRADING_ENABLED}")
        logger.info(f"  DATABASE_URL: {settings.DATABASE_URL}")
        logger.info(f"  CLOB: {settings.CLOB_API_URL}")
    except Exception:
        logger.exception("✗ Settings inválidos")
        return 1

    # === 2. DB init ===
    try:
        init_db()
        BotState.db_initialized = True
        logger.success("✓ DB inicializada")
    except Exception:
        logger.exception("✗ DB no arranca")
        return 1

    # === 3. DB round-trip ===
    try:
        with get_session() as s:
            run = BotRun(
                environment=settings.POLYMARKET_ENV,
                trading_enabled=settings.TRADING_ENABLED,
                capital_at_start=settings.ACTIVE_CAPITAL_USD,
                motors_enabled=json.dumps([]),
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            row_id = run.id
        with get_session() as s:
            got = s.get(BotRun, row_id)
            assert got is not None
            got_env = got.environment
        assert got_env == settings.POLYMARKET_ENV
        logger.success(f"✓ DB round-trip OK (BotRun#{row_id})")
    except Exception:
        logger.exception("✗ DB round-trip falló")
        return 1

    # === 4. Health endpoints ===
    try:
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200, f"status={r.status_code}, body={r.text}"
            body = r.json()
            assert body.get("status") == "ok", body
            logger.success(f"✓ /health responde 200 (uptime={body.get('uptime_seconds'):.1f}s)")

            r2 = client.get("/status")
            assert r2.status_code == 200
            assert "ws_connected" in r2.json()
            logger.success("✓ /status responde 200 (con ws_connected)")

            r3 = client.get("/ready")
            assert r3.status_code == 200, r3.text
            logger.success("✓ /ready responde 200")
    except Exception:
        logger.exception("✗ Health server no responde bien")
        return 1

    # === 5. CLOB read-only ===
    clob_ok = await check_clob_readonly()
    if clob_ok is False:
        return 1

    logger.info("=" * 60)
    logger.success("✅ TODOS LOS CHECKS PASARON — Polybot sano")
    logger.info("=" * 60)

    if settings.is_production:
        logger.warning("⚠️  POLYMARKET_ENV=production — verificá el checklist NO-GO.")
    else:
        logger.info("Estás en PAPER. Para levantar el runner:  python -m src.runner")

    return 0


def main() -> None:
    exit_code = asyncio.run(run_smoke_test())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
