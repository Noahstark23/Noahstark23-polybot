"""
Motor 5 — Spillover en grupos neg-risk (F2: SHADOW puro, port del M9 de Kalshi).

TESIS: en un evento multi-outcome las probabilidades se CONSERVAN (suman ~1).
Un salto de +X pp en una pata implica que las hermanas tienen que ajustar a la
baja (y viceversa). Si las hermanas ajustan con RETRASO medible, ese retraso
es una ineficiencia explotable. El shadow mide exactamente eso: cada salto
dispara mediciones del follow-through de cada hermana a T+60 y T+120.

El follow va FIRMADO desde la dirección ESPERADA (inversa del salto):
follow > 0 = la hermana ajustó como la conservación de probabilidad predice,
DESPUÉS del trigger. La distribución de follows decide el gate — el diseño no
asume que la tesis es cierta.

Reusa los grupos neg-risk del discovery del Motor 3 (sinergia deliberada del
plan: cero fuentes nuevas). Requiere MARKET_DISCOVERY_SOURCE=neg_risk.

Lecciones aplicadas: unidades únicas (todo en _pp), nada sin tope (historias
de mid con deque acotado, pendientes con tope y gracia, cooldown por pata
trigger, retención en el mismo commit), fila persistida SOLO con la medición
completa. SIN executor: no existe ejecución de M5 en el repo.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from loguru import logger

from src.db.engine import get_session
from src.db.models import SpilloverWindow
from src.marketdata import registry
from src.utils.config import Settings, get_settings

MEASURE_60 = 60.0
MEASURE_120 = 120.0
MEASURE_GRACE = 300.0
MAX_PENDING = 500  # nada sin tope


@dataclass
class _Pending:
    group_id: str
    trigger_cid: str
    sibling_cid: str
    sibling_token: str
    trigger_move_pp: float
    expected_sign: float  # inversa del salto
    mid0: float
    t0: float
    follow60_pp: float | None = None


@dataclass
class _LegState:
    mids: deque = field(default_factory=deque)  # (ts, mid) dentro de la ventana
    cooldown_until: float = 0.0


class SpilloverEngine:
    """Detecta saltos por pata y mide el follow-through de las hermanas."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._legs: dict[str, _LegState] = {}  # token_id_yes -> historia de mids
        self._pending: list[_Pending] = []
        self.stats = {"triggers": 0, "measured": 0, "dropped": 0}

    # ==================================================
    # Detección pura del salto (testeable sin capture)
    # ==================================================

    def detect_jump(self, token_id: str, mid: float, now: float) -> float | None:
        """
        Alimenta la historia del mid y devuelve el salto en pp si supera el
        umbral dentro de la ventana (con cooldown por pata: UN trigger por
        episodio, no uno por tick mientras el salto siga en la ventana).
        """
        st = self._legs.setdefault(token_id, _LegState())
        cutoff = now - self.settings.MOTOR_5_TRIGGER_WINDOW_SEC
        while st.mids and st.mids[0][0] < cutoff:
            st.mids.popleft()
        oldest_mid = st.mids[0][1] if st.mids else None
        st.mids.append((now, mid))
        if oldest_mid is None or now < st.cooldown_until:
            return None
        move_pp = (mid - oldest_mid) * 100.0
        if abs(move_pp) < self.settings.MOTOR_5_TRIGGER_MOVE_PP:
            return None
        st.cooldown_until = now + self.settings.MOTOR_5_TRIGGER_WINDOW_SEC
        return move_pp

    # ==================================================
    # Tick
    # ==================================================

    def tick(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        capture = registry.get_capture()
        summary = {"groups": 0, "triggers": 0, "measured": 0}
        if capture is None or not getattr(capture, "neg_risk_groups", None):
            self._advance(capture, now)
            return summary

        groups = capture.neg_risk_groups
        summary["groups"] = len(groups)
        for group_id, legs in groups.items():
            mids = {
                m.condition_id: (m.token_id_yes, capture._mid_of(m.token_id_yes))
                for m in legs
            }
            for m in legs:
                token, mid = mids[m.condition_id]
                if mid is None:
                    continue
                jump_pp = self.detect_jump(token, mid, now)
                if jump_pp is None:
                    continue
                summary["triggers"] += 1
                self.stats["triggers"] += 1
                expected_sign = -1.0 if jump_pp > 0 else 1.0
                logger.info(
                    f"[MOTOR 5 SHADOW] spillover trigger grupo={group_id[:16]}… "
                    f"pata={m.condition_id[:16]}… salto={jump_pp:+.1f}pp — midiendo "
                    f"follow de {len(legs) - 1} hermana(s) a T+60/T+120 (NO ejecuta)"
                )
                for sib in legs:
                    if sib.condition_id == m.condition_id:
                        continue
                    sib_token, sib_mid = mids[sib.condition_id]
                    if sib_mid is None:
                        self.stats["dropped"] += 1
                        continue
                    if len(self._pending) >= MAX_PENDING:
                        self.stats["dropped"] += 1
                        continue
                    self._pending.append(
                        _Pending(
                            group_id=group_id,
                            trigger_cid=m.condition_id,
                            sibling_cid=sib.condition_id,
                            sibling_token=sib_token,
                            trigger_move_pp=round(jump_pp, 4),
                            expected_sign=expected_sign,
                            mid0=sib_mid,
                            t0=now,
                        )
                    )
        summary["measured"] = self._advance(capture, now)
        return summary

    def _advance(self, capture, now: float) -> int:
        """Madura las mediciones pendientes. Devuelve cuántas se persistieron."""
        measured = 0
        still: list[_Pending] = []
        for p in self._pending:
            age = now - p.t0
            mid = capture._mid_of(p.sibling_token) if capture is not None else None
            if p.follow60_pp is None and age >= MEASURE_60 and mid is not None:
                p.follow60_pp = round((mid - p.mid0) * 100.0 * p.expected_sign, 4)
            if age >= MEASURE_120:
                if mid is not None:
                    self._persist(p, mid)
                    measured += 1
                    self.stats["measured"] += 1
                    continue
                if age >= MEASURE_GRACE:
                    self.stats["dropped"] += 1  # book caído toda la gracia
                    continue
            still.append(p)
        self._pending = still
        return measured

    def _persist(self, p: _Pending, mid120: float) -> None:
        try:
            with get_session() as s:
                s.add(
                    SpilloverWindow(
                        neg_risk_market_id=p.group_id,
                        trigger_condition_id=p.trigger_cid,
                        sibling_condition_id=p.sibling_cid,
                        trigger_move_pp=p.trigger_move_pp,
                        sibling_mid0=round(p.mid0, 6),
                        follow60_pp=p.follow60_pp,
                        follow120_pp=round((mid120 - p.mid0) * 100.0 * p.expected_sign, 4),
                    )
                )
        except Exception:
            logger.exception("motor5.shadow persist_error (se sigue)")

    # ==================================================
    # Servicio
    # ==================================================

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"Motor 5 (spillover neg-risk) en SHADOW — tick={self.settings.MOTOR_5_TICK_SECONDS}s, "
            f"trigger>={self.settings.MOTOR_5_TRIGGER_MOVE_PP}pp en "
            f"{self.settings.MOTOR_5_TRIGGER_WINDOW_SEC}s — SIN executor. Requiere "
            "discovery neg_risk (sin grupos no hay qué medir)."
        )
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Error en tick del motor 5 (sigo)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.MOTOR_5_TICK_SECONDS
                )
                return
            except TimeoutError:
                continue


def create_service():
    """Factory para el runner. SIN executor: shadow por construcción."""
    engine = SpilloverEngine(get_settings())
    return engine.run
