"""
Motor 4 — shadow del OFI: señal → medición del movimiento REAL a T+30/T+60.

Port del shadow del M8 de Kalshi. Cada señal del detector se guarda como
"pendiente" con el mid del momento (mid0) y madura con el propio flujo de
eventos: al pasar T+30 se toma mid30, al pasar T+60 se toma mid60 y recién ahí
se PERSISTE la fila completa (una señal sin resultado no sirve para el gate y
no se escribe — nada sin tope). Si el book está caído toda la ventana de
gracia, la medición se descarta y se cuenta.

Best-effort TOTAL (Lección 7): este código corre dentro del handler del feed
del data capture y JAMÁS puede romper la captura — cualquier excepción se
loguea y el stream sigue.

UNIDADES (una columna, una unidad): `zscore` es adimensional y tiene su propia
columna; los moves van en `_pp` (puntos de probabilidad, mid×100), FIRMADOS
desde la presión: move > 0 = el precio siguió a la presión (momentum), < 0 =
la contradijo (contrarian). El gate decide con la distribución, no el diseño.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

from src.db.engine import get_session
from src.db.models import OfiSignalRow
from src.motor_4_ofi.detector import OfiSignal, OfiTracker, TopOfBook

MEASURE_30 = 30.0
MEASURE_60 = 60.0
MEASURE_GRACE = 180.0  # book caído más allá de esto → medición descartada


@dataclass
class _Pending:
    signal: OfiSignal
    mid0: float
    t0: float
    mid30: float | None = None


@dataclass
class OfiShadowStats:
    signals: int = 0
    measured: int = 0
    dropped: int = 0
    pending: int = field(default=0)


class OfiShadow:
    """Observa transiciones de top-of-book, detecta señales y las mide."""

    MAX_PENDING = 500  # nada sin tope: pendientes más allá de esto se descartan

    def __init__(
        self,
        *,
        mid_fn,
        window_sec: float,
        z_min: float,
        min_baseline: int,
        cooldown_sec: float,
    ) -> None:
        self._mid = mid_fn  # token_id -> mid (0-1) | None
        self._tracker = OfiTracker(
            window_sec=window_sec,
            z_min=z_min,
            min_baseline=min_baseline,
            cooldown_sec=cooldown_sec,
        )
        self._pending: list[_Pending] = []
        self.stats = OfiShadowStats()

    def observe_top(self, token_id: str, top: TopOfBook, now: float | None = None) -> None:
        """Una transición de top-of-book. Best-effort: nunca rompe la captura."""
        try:
            now = time.monotonic() if now is None else now
            self._advance_measurements(now)
            sig = self._tracker.observe(token_id, top, now)
            if sig is None:
                return
            mid0 = self._mid(token_id)
            if mid0 is None:
                self.stats.dropped += 1  # sin book sano no hay experimento válido
                return
            if len(self._pending) >= self.MAX_PENDING:
                self.stats.dropped += 1
                return
            self.stats.signals += 1
            self._pending.append(_Pending(signal=sig, mid0=mid0, t0=now))
            logger.info(
                f"[MOTOR 4 SHADOW] ofi token={token_id[:16]}… presión={sig.pressure} "
                f"z={sig.zscore:+.2f} ofi={sig.ofi:+.0f} mid0={mid0:.3f} — midiendo "
                f"T+30/T+60 (NO ejecuta, F2)"
            )
        except Exception:
            logger.exception("motor4.shadow observe_top falló (la captura sigue)")

    def _advance_measurements(self, now: float) -> None:
        """Madura pendientes. El reloj lo empuja el flujo de eventos: sin eventos
        no hay mercado moviéndose y una medición tardía sigue siendo válida."""
        still: list[_Pending] = []
        for p in self._pending:
            age = now - p.t0
            if p.mid30 is None and age >= MEASURE_30:
                p.mid30 = self._mid(p.signal.token_id)  # None → se reintenta hasta grace
            if age >= MEASURE_60:
                mid60 = self._mid(p.signal.token_id)
                if mid60 is not None and p.mid30 is not None:
                    self._persist(p, mid60)
                    self.stats.measured += 1
                    continue
                if age >= MEASURE_GRACE:
                    self.stats.dropped += 1  # book caído toda la ventana
                    continue
            still.append(p)
        self._pending = still
        self.stats.pending = len(self._pending)

    def _persist(self, p: _Pending, mid60: float) -> None:
        """La señal CON su resultado — firmado desde la presión."""
        sign = 1.0 if p.signal.pressure == "UP" else -1.0
        move30_pp = ((p.mid30 or p.mid0) - p.mid0) * 100.0 * sign
        move60_pp = (mid60 - p.mid0) * 100.0 * sign
        try:
            with get_session() as s:
                s.add(
                    OfiSignalRow(
                        token_id=p.signal.token_id,
                        pressure=p.signal.pressure,
                        ofi_contracts=round(p.signal.ofi, 2),
                        zscore=round(p.signal.zscore, 4),
                        n_baseline=p.signal.n_baseline,
                        mid0=round(p.mid0, 6),
                        mid30=round(p.mid30, 6) if p.mid30 is not None else None,
                        mid60=round(mid60, 6),
                        move30_pp=round(move30_pp, 4),
                        move60_pp=round(move60_pp, 4),
                    )
                )
        except Exception:
            logger.exception("motor4.shadow persist_error (se sigue)")
