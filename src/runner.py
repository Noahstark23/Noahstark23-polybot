"""
ProductionRunner de Polybot.

Orquesta:
    1. Logging
    2. DB (crea tablas)
    3. Health server FastAPI (uvicorn en background)
    4. Servicios de la fase activa (F1: data capture; F2: motor 1 en shadow;
       F3: reconciliación — sólo si el humano encendió trading)
    5. Shutdown limpio con SIGTERM (Coolify lo manda al redeploy)

Punto de entrada: `python -m src.runner`

Los servicios se resuelven por import dinámico: si el módulo de una fase todavía
no existe en el repo, el runner lo salta y sigue — así F0 arranca sólo con health.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import signal
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import uvicorn
from loguru import logger

from src.api.health import BotState, app
from src.db.engine import get_session, init_db
from src.db.models import BotRun
from src.utils.config import get_settings
from src.utils.logging import setup_logging

# (nombre, módulo, factory, flag de settings que lo habilita)
SERVICE_SPECS: list[tuple[str, str, str, str]] = [
    ("data_capture", "src.strategies.data_capture", "create_service", "DATA_CAPTURE_ENABLED"),
    ("motor_1_arbitrage", "src.motor_1_arbitrage.engine", "create_service", "MOTOR_1_ARBITRAGE_ENABLED"),
    ("motor_2_consensus", "src.motor_2_consensus.engine", "create_service", "MOTOR_2_CONSENSUS_ENABLED"),
    ("motor_3_neg_risk", "src.motor_3_neg_risk.engine", "create_service", "MOTOR_3_NEG_RISK_ENABLED"),
    # Motor 4 (OFI) NO es un servicio: va embebido en data_capture (mismo stream WS).
    ("motor_5_spillover", "src.motor_5_spillover.engine", "create_service", "MOTOR_5_SPILLOVER_ENABLED"),
    ("analyst", "src.analytics.analyst_loop", "create_service", "ANALYST_ENABLED"),
    ("maintenance", "src.storage.maintenance", "create_service", "MAINTENANCE_ENABLED"),
    ("user_channel", "src.execution.service", "create_service", "TRADING_ENABLED"),
    ("reconciler", "src.reconcile.service", "create_service", "TRADING_ENABLED"),
    ("telegram_digest", "src.monitoring.telegram_alerts", "create_service", "TELEGRAM_DASHBOARD_ENABLED"),
]

ServiceFactory = Callable[[], Callable[[asyncio.Event], Coroutine[Any, Any, None]]]


class Runner:
    """Orquestador principal."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop_event = asyncio.Event()
        self._uvicorn_server: uvicorn.Server | None = None
        self._bot_run_id: int | None = None
        self._service_tasks: list[asyncio.Task] = []
        self._motors: list[str] = []

    # =====================================================
    # DB tracking
    # =====================================================

    def _record_run_start(self) -> None:
        with get_session() as s:
            run = BotRun(
                environment=self.settings.POLYMARKET_ENV,
                trading_enabled=self.settings.TRADING_ENABLED,
                capital_at_start=self.settings.ACTIVE_CAPITAL_USD,
                motors_enabled=json.dumps(self._motors),
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            self._bot_run_id = run.id
            logger.info(f"BotRun#{run.id} registrado (motores: {self._motors or 'ninguno'})")

    def _record_run_end(self, crash_reason: str | None = None) -> None:
        if self._bot_run_id is None:
            return
        try:
            with get_session() as s:
                run = s.get(BotRun, self._bot_run_id)
                if run is not None:
                    run.ended_at = datetime.now(UTC)
                    run.crash_reason = crash_reason
                    s.add(run)
        except Exception:
            logger.exception("No pude cerrar BotRun en DB")

    # =====================================================
    # Servicios por fase
    # =====================================================

    def _resolve_services(self) -> list[tuple[str, Callable[[asyncio.Event], Coroutine]]]:
        """Import dinámico de los servicios habilitados y disponibles en el repo."""
        resolved = []
        for name, module_path, factory_name, flag in SERVICE_SPECS:
            if not getattr(self.settings, flag, False):
                logger.info(f"Servicio '{name}' deshabilitado ({flag}=false)")
                continue
            try:
                module = importlib.import_module(module_path)
            except ModuleNotFoundError:
                logger.info(f"Servicio '{name}' aún no implementado ({module_path}) — skip")
                continue
            factory = getattr(module, factory_name)
            service = factory()
            if service is None:
                logger.info(f"Servicio '{name}' no aplica en esta config — skip")
                continue
            resolved.append((name, service))
        return resolved

    async def _run_service(self, name: str, service: Callable[[asyncio.Event], Coroutine]) -> None:
        """Corre un servicio con reinicio ante crash (backoff) hasta stop_event."""
        backoff = 5.0
        while not self._stop_event.is_set():
            try:
                await service(self._stop_event)
                return  # terminó limpio
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                BotState.record_error(f"{name}: {type(exc).__name__}: {exc}")
                logger.exception(f"Servicio '{name}' crasheó — reinicio en {backoff:.0f}s")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    return
                except TimeoutError:
                    backoff = min(backoff * 2, 300.0)

    # =====================================================
    # Health server
    # =====================================================

    async def _start_health_server(self) -> None:
        config = uvicorn.Config(
            app=app,
            host=self.settings.HEALTH_HOST,
            port=self.settings.HEALTH_PORT,
            log_config=None,  # loguru maneja logs
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._service_tasks.append(asyncio.create_task(self._uvicorn_server.serve()))
        logger.info(
            f"Health server: http://{self.settings.HEALTH_HOST}:{self.settings.HEALTH_PORT}"
        )

    # =====================================================
    # Signals
    # =====================================================

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown, sig.name)
            except NotImplementedError:
                signal.signal(sig, lambda s, f: self._request_shutdown(signal.Signals(s).name))

    def _request_shutdown(self, sig_name: str) -> None:
        logger.warning(f"Señal {sig_name} recibida — shutdown limpio")
        self._stop_event.set()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    # =====================================================
    # Main
    # =====================================================

    async def run(self) -> int:
        setup_logging()
        logger.info(f"🚀 Polybot arrancando en {self.settings.POLYMARKET_ENV.upper()}")
        logger.info(f"Capital activo: ${self.settings.ACTIVE_CAPITAL_USD}")
        logger.info(f"Trading enabled: {self.settings.TRADING_ENABLED}")
        BotState.shadow_mode = not self.settings.TRADING_ENABLED

        try:
            init_db()
            BotState.db_initialized = True
        except Exception:
            logger.exception("Fallo al inicializar DB — aborto arranque")
            return 2

        # NO-GO check: si trading está encendido, el checklist debe estar verde
        if self.settings.TRADING_ENABLED:
            from scripts.check_no_go import run_checks

            failures = run_checks(self.settings)
            if failures:
                logger.critical(f"NO-GO: {failures} — arranco en modo SHADOW forzado")
                BotState.shadow_mode = True

        services = self._resolve_services()
        self._motors = [n for n, _ in services]
        BotState.motors = self._motors

        try:
            self._record_run_start()
        except Exception:
            logger.exception("No pude registrar BotRun (sigo arrancando)")

        self._install_signal_handlers()
        await self._start_health_server()

        for name, service in services:
            self._service_tasks.append(asyncio.create_task(self._run_service(name, service)))
            logger.info(f"Servicio '{name}' lanzado")

        if not services:
            logger.info("Sin servicios en esta fase — idle loop hasta SIGTERM")

        try:
            await self._stop_event.wait()
            # uvicorn ya tiene should_exit=True — darle una ventana de cierre limpio
            done, pending = await asyncio.wait(self._service_tasks, timeout=10.0)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._record_run_end(crash_reason=None)
            logger.info("Shutdown limpio completado")
            return 0
        except Exception as exc:
            logger.exception("Runner crasheó")
            self._record_run_end(crash_reason=f"{type(exc).__name__}: {exc}"[:500])
            return 1


def main() -> None:
    exit_code = asyncio.run(Runner().run())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
