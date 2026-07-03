"""
Limpia el kill-switch — SOLO para uso HUMANO, nunca lo corre el bot ni el agente.

Uso (dentro del contenedor o con el venv local):
    python -m scripts.clear_kill_switch --confirm "entiendo el riesgo"

Exige el texto de confirmación exacto para evitar limpiezas accidentales.
"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

from src.db.engine import init_db
from src.risk.manager import KEY_KILL_SWITCH, KEY_PAUSE_REASON, RiskManager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != "entiendo el riesgo":
        logger.error('Confirmación inválida. Usá: --confirm "entiendo el riesgo"')
        sys.exit(1)

    init_db()
    rm = RiskManager()
    if not rm.kill_switch_active():
        logger.info("El kill-switch no está activo. Nada que limpiar.")
        sys.exit(0)

    reason = rm.pause_reason()
    rm._set_state(KEY_KILL_SWITCH, "")
    rm._set_state(KEY_PAUSE_REASON, "")
    rm._record_event("kill_switch_cleared", "critical", f"Limpiado por humano. Causa previa: {reason}")
    logger.warning(f"Kill-switch limpiado. Causa previa: {reason}")
    logger.warning("Revisá la causa raíz ANTES de re-encender trading.")


if __name__ == "__main__":
    main()
