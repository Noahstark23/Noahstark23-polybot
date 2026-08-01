"""
Motor 4 — detector de ORDER FLOW IMBALANCE (puro: sin red, sin DB, sin reloj).

Port del detector del M8 de Kalshi — el motor que su auditoría llamó "la única
promesa viva" (p50 +3.18pp a T+60 con n=130). TESIS a validar acá: un
desequilibrio anómalo del flujo de órdenes precede al movimiento del precio.

Adaptación de la fuente: Kalshi entrega deltas firmados por lado; el WS de
Polymarket entrega snapshots/price_change y el OrderbookManager los resume a
top-of-book. El flujo se reconstruye con el OFI clásico de top-of-book (Cont,
Kukanov & Stoikov): comparar el mejor bid/ask ANTES y DESPUÉS de cada evento:

    e = 1{Pb>=Pb'}·qb − 1{Pb<=Pb'}·qb' − 1{Pa<=Pa'}·qa + 1{Pa>=Pa'}·qa'

    (' = estado previo; q en contratos). e > 0 = presión COMPRADORA neta.

Sobre el z-score (lección 2026-07-28, edge_pct polimórfica): el z-score es
ADIMENSIONAL y vive en su propia columna `zscore` — jamás en un campo *_pct.
Y el detector solo emite con |z| >= z_min: la AUSENCIA de valores entre 0 y
z_min en la tabla es el umbral funcionando, no un artefacto de datos.

Reservas documentadas (por qué la tesis puede fallar — el shadow las mide):
books finos donde una sola orden "es" el book, y flujo informado cerca de la
resolución (adverse selection). Igual que en Kalshi, el shadow NO asume
dirección: registra presión y movimiento real posterior; momentum vs
contrarian lo decide el gate con datos.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TopOfBook:
    """Foto del mejor nivel de un token (None = lado vacío)."""

    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float


def ofi_contribution(prev: TopOfBook, curr: TopOfBook) -> float:
    """
    Contribución de UNA transición de top-of-book al OFI (Cont et al.).
    Positiva = presión compradora (el bid sube/engorda o el ask se vacía).
    Lados vacíos aportan 0 (sin nivel no hay flujo medible de ese lado).
    """
    e = 0.0
    if prev.bid is not None and curr.bid is not None:
        if curr.bid >= prev.bid:
            e += curr.bid_size
        if curr.bid <= prev.bid:
            e -= prev.bid_size
    if prev.ask is not None and curr.ask is not None:
        if curr.ask <= prev.ask:
            e -= curr.ask_size
        if curr.ask >= prev.ask:
            e += prev.ask_size
    return e


@dataclass(frozen=True, slots=True)
class OfiSignal:
    """Desequilibrio anómalo detectado (sin veredicto de dirección — eso es del gate)."""

    token_id: str
    pressure: str  # "UP" (compradora) | "DOWN" (vendedora)
    ofi: float  # flujo neto de la ventana (contratos)
    zscore: float
    n_baseline: int


@dataclass
class _TokenState:
    flows: deque = field(default_factory=deque)  # (ts, contribucion)
    ofi: float = 0.0  # suma corriente (evita re-sumar la ventana)
    baseline: deque = field(default_factory=lambda: deque(maxlen=1000))  # nada sin tope
    cooldown_until: float = 0.0
    last_top: TopOfBook | None = None


class OfiTracker:
    """Ventana rodante de OFI + z-score por token. Estado en memoria, nada persiste acá."""

    def __init__(
        self,
        *,
        window_sec: float,
        z_min: float,
        min_baseline: int,
        cooldown_sec: float,
    ) -> None:
        self._window = window_sec
        self._z_min = z_min
        self._min_baseline = min_baseline
        self._cooldown = cooldown_sec
        self._by_token: dict[str, _TokenState] = {}

    def observe(self, token_id: str, top: TopOfBook, now: float) -> OfiSignal | None:
        """
        Una transición de top-of-book → actualiza la ventana; señal si
        |z| >= z_min con baseline maduro y fuera del cooldown.

        El baseline se muestrea ANTES de sumar la contribución nueva (patrón
        del M8 de Kalshi: la señal se compara contra una historia que NO la
        incluye — un spike no puede normalizarse a sí mismo).
        """
        st = self._by_token.setdefault(token_id, _TokenState())
        prev_top = st.last_top
        st.last_top = top
        if prev_top is None:
            return None  # primera foto: no hay transición que medir

        contribution = ofi_contribution(prev_top, top)

        cutoff = now - self._window
        while st.flows and st.flows[0][0] < cutoff:
            _, old = st.flows.popleft()
            st.ofi -= old

        st.baseline.append(st.ofi)  # foto PRE-contribución
        st.flows.append((now, contribution))
        st.ofi += contribution

        if len(st.baseline) < self._min_baseline or now < st.cooldown_until:
            return None
        mean = sum(st.baseline) / len(st.baseline)
        var = sum((x - mean) ** 2 for x in st.baseline) / len(st.baseline)
        std = var**0.5
        if std <= 0:
            return None
        z = (st.ofi - mean) / std
        if abs(z) < self._z_min:
            return None
        st.cooldown_until = now + self._cooldown  # anti-ráfaga: una señal por episodio
        return OfiSignal(
            token_id=token_id,
            pressure="UP" if st.ofi > mean else "DOWN",
            ofi=st.ofi,
            zscore=z,
            n_baseline=len(st.baseline),
        )
