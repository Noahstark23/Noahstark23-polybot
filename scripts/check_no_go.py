"""
Checklist NO-GO (ARCHITECTURE.md §8) — bloquea el arranque con dinero real.

Uso:
    python -m scripts.check_no_go

Comportamiento:
    - Corre todos los checks y los imprime.
    - Si TRADING_ENABLED=true y hay algún check en rojo -> exit 1 (NO-GO).
    - Con trading apagado los checks informan pero no bloquean (exit 0),
      EXCEPTO secretos commiteados en el repo, que siempre es fatal.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlmodel import select

from src.utils.config import (
    HARD_MAX_DAILY_LOSS_PCT,
    HARD_MAX_EXPOSURE_PCT,
    HARD_MAX_KELLY_FRACTION,
    HARD_MAX_MONTHLY_LOSS_PCT,
    HARD_MAX_TRADE_SIZE_PCT,
    HARD_MAX_WEEKLY_LOSS_PCT,
    Settings,
    get_settings,
)

SHADOW_MIN_DAYS = 7
PRIVATE_KEY_RE = re.compile(r"(0x)?[0-9a-fA-F]{64}")


def _check_wallet_key_secret(settings: Settings) -> str | None:
    """La wallet key debe estar en secret volume, nunca en .env en claro."""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if (
                "KEY" in key.upper()
                and "PATH" not in key.upper()
                and PRIVATE_KEY_RE.fullmatch(value.strip().strip('"').strip("'") or "")
            ):
                return f".env contiene lo que parece una private key en claro ({key})"
    if settings.TRADING_ENABLED:
        if settings.POLY_SIGNER_KEY_PATH is None:
            return "POLY_SIGNER_KEY_PATH sin configurar (la key va en secret volume)"
        if not settings.POLY_SIGNER_KEY_PATH.exists():
            return f"El secret volume no tiene la key: {settings.POLY_SIGNER_KEY_PATH}"
    return None


def _check_no_secrets_in_repo() -> str | None:
    """Ningún .pem/keystore/.env trackeado por git (hallazgo §10 del repo base)."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except Exception:
        return None  # sin git (contenedor) — el check corre en CI
    bad = [
        f
        for f in out
        if f.endswith((".pem", ".key"))
        or f == ".env"
        or "wallet_key" in f
        or (f.startswith("secrets/") and not f.endswith(".gitkeep"))
    ]
    if bad:
        return f"Secretos trackeados en git: {bad}"
    return None


def _check_shadow_days(settings: Settings) -> str | None:
    """production exige >= 7 días de shadow continuo (FunnelSnapshot en DB)."""
    if not settings.is_production:
        return None
    try:
        from src.db.engine import get_session
        from src.db.models import FunnelSnapshot

        with get_session() as s:
            first = s.exec(
                select(FunnelSnapshot).order_by(FunnelSnapshot.cycle_ts).limit(1)
            ).first()
            last = s.exec(
                select(FunnelSnapshot).order_by(FunnelSnapshot.cycle_ts.desc()).limit(1)  # type: ignore[attr-defined]
            ).first()
    except Exception as exc:
        return f"No pude leer FunnelSnapshot para validar shadow: {type(exc).__name__}"
    if first is None or last is None:
        return "Sin datos de shadow (FunnelSnapshot vacío) — se exigen >= 7 días"
    first_ts = first.cycle_ts.replace(tzinfo=first.cycle_ts.tzinfo or UTC)
    if datetime.now(UTC) - first_ts < timedelta(days=SHADOW_MIN_DAYS):
        return (
            f"Shadow insuficiente: empezó {first.cycle_ts.isoformat()} "
            f"(se exigen >= {SHADOW_MIN_DAYS} días)"
        )
    return None


def _check_kill_switch() -> str | None:
    """Kill-switch activo sólo lo limpia el humano (clear_kill_switch.py)."""
    try:
        from src.risk.manager import RiskManager

        if RiskManager().kill_switch_active():
            return "Kill-switch ACTIVO — limpiarlo requiere scripts/clear_kill_switch.py (humano)"
    except Exception as exc:
        return f"No pude leer el estado del kill-switch: {type(exc).__name__}"
    return None


def _check_reconciliation() -> str | None:
    """Última reconciliación on-chain <-> DB sin discrepancia."""
    try:
        from src.risk.manager import RiskManager

        status = RiskManager._get_state("reconcile_status")
    except Exception as exc:
        return f"No pude leer reconcile_status: {type(exc).__name__}"
    if status == "mismatch":
        return "Discrepancia posición on-chain <-> DB en el último ciclo de reconciliación"
    return None


def _check_risk_limits(settings: Settings) -> str | None:
    """Ningún límite fuera del rango hardcoded (no diluidos)."""
    violations = []
    if settings.MAX_DAILY_LOSS_PCT > HARD_MAX_DAILY_LOSS_PCT:
        violations.append("MAX_DAILY_LOSS_PCT")
    if settings.MAX_WEEKLY_LOSS_PCT > HARD_MAX_WEEKLY_LOSS_PCT:
        violations.append("MAX_WEEKLY_LOSS_PCT")
    if settings.MAX_MONTHLY_LOSS_PCT > HARD_MAX_MONTHLY_LOSS_PCT:
        violations.append("MAX_MONTHLY_LOSS_PCT")
    if settings.MAX_SIMULTANEOUS_EXPOSURE_PCT > HARD_MAX_EXPOSURE_PCT:
        violations.append("MAX_SIMULTANEOUS_EXPOSURE_PCT")
    if settings.MAX_TRADE_SIZE_PCT > HARD_MAX_TRADE_SIZE_PCT:
        violations.append("MAX_TRADE_SIZE_PCT")
    if settings.KELLY_FRACTION > HARD_MAX_KELLY_FRACTION:
        violations.append("KELLY_FRACTION")
    if violations:
        return f"Límites de riesgo diluidos: {violations}"
    return None


def _check_capital(settings: Settings) -> str | None:
    if settings.TRADING_ENABLED and settings.ACTIVE_CAPITAL_USD <= 0:
        return "TRADING_ENABLED=true sin ACTIVE_CAPITAL_USD configurado"
    return None


def _check_isolation(settings: Settings) -> str | None:
    """No colisionar con el contenedor Kalshi: paths y DB propios."""
    if "kalshi" in settings.DATABASE_URL.lower():
        return f"DATABASE_URL apunta a datos de Kalshi: {settings.DATABASE_URL}"
    if "kalshi" in str(settings.LOGS_DIR).lower():
        return f"LOGS_DIR apunta a logs de Kalshi: {settings.LOGS_DIR}"
    if settings.HEALTH_PORT == 18080:
        return "HEALTH_PORT=18080 es el del bot Kalshi (Polybot usa 8080 -> host 18081)"
    return None


def run_checks(settings: Settings | None = None) -> list[str]:
    """Devuelve la lista de fallos (vacía = GO)."""
    settings = settings or get_settings()
    checks: list[tuple[str, str | None]] = [
        ("wallet key en secret volume", _check_wallet_key_secret(settings)),
        ("sin secretos en el repo", _check_no_secrets_in_repo()),
        (f"shadow >= {SHADOW_MIN_DAYS} días antes de production", _check_shadow_days(settings)),
        ("kill-switch inactivo", _check_kill_switch()),
        ("reconciliación on-chain <-> DB limpia", _check_reconciliation()),
        ("límites de riesgo dentro del rango hardcoded", _check_risk_limits(settings)),
        ("capital configurado", _check_capital(settings)),
        ("aislamiento del contenedor Kalshi", _check_isolation(settings)),
    ]
    failures = []
    for name, error in checks:
        if error is None:
            logger.success(f"✓ {name}")
        else:
            logger.error(f"✗ {name}: {error}")
            failures.append(error)
    return failures


def main() -> None:
    from src.db.engine import init_db

    settings = get_settings()
    logger.info("=" * 60)
    logger.info("CHECKLIST NO-GO — Polybot (ARCHITECTURE.md §8)")
    logger.info(f"env={settings.POLYMARKET_ENV}  trading={settings.TRADING_ENABLED}")
    logger.info("=" * 60)

    try:
        init_db()
    except Exception:
        logger.exception("DB no disponible")

    failures = run_checks(settings)

    fatal_always = [f for f in failures if "trackeados en git" in f or "en claro" in f]

    if not failures:
        logger.success("✅ GO — checklist completo en verde")
        sys.exit(0)
    if settings.TRADING_ENABLED or fatal_always:
        logger.critical(f"🛑 NO-GO — {len(failures)} check(s) en rojo")
        sys.exit(1)
    logger.warning(
        f"⚠ {len(failures)} check(s) en rojo, pero TRADING_ENABLED=false — "
        "no bloquea (bloquearía el arranque con dinero)"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
