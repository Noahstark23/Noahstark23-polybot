"""
Logging con loguru.

Sinks:
    - stderr (lo que Coolify captura como container logs)
    - archivo diario en LOGS_DIR (rotate midnight, retention 30d, gzip)
    - archivo crítico (WARNING+, retention 90d) para auditoría
"""
from __future__ import annotations

import sys

from loguru import logger

from src.utils.config import get_settings


def setup_logging() -> None:
    """Configura el logger global. Llamar UNA vez al arranque."""
    settings = get_settings()
    logger.remove()

    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        backtrace=True,
        diagnose=False,  # no exponer valores (puede filtrar datos sensibles)
    )

    logs_dir = settings.LOGS_DIR
    try:
        logs_dir.mkdir(exist_ok=True, parents=True)
    except PermissionError:
        logger.warning(f"Sin permisos para crear {logs_dir} — sólo logs a stderr")
        return

    logger.add(
        logs_dir / "polybot_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        backtrace=True,
        diagnose=False,
    )

    logger.add(
        logs_dir / "critical_{time:YYYY-MM-DD}.log",
        level="WARNING",
        rotation="00:00",
        retention="90 days",
        compression="gz",
    )

    logger.info(f"Logging inicializado (level={settings.LOG_LEVEL}, env={settings.POLYMARKET_ENV})")
