"""
Remoción de vig (juice) de cuotas de sportsbook → probabilidades JUSTAS.

Portado del bot Kalshi (src/math/no_vig.py, probado en producción): el
sportsbook cobra su comisión inflando las probabilidades implícitas — la suma
de los inversos de las cuotas decimales es > 1 (el "overround"/vig). Para usar
el consenso como benchmark de precio justo (Motor 2) hay que quitarlo.

Método primario: MULTIPLICATIVO (proporcional): fair_i = p_i / Σp. Estable,
siempre devuelve probabilidades válidas que suman 1. El ADITIVO se incluye
solo para diagnóstico (sesga mal a los longshots; puede dar ≤ 0).

Funciones PURAS — sin I/O, sin estado (§7). Trabajan sobre PROBABILIDADES
implícitas; `implied_prob()` convierte cuota decimal → probabilidad.
"""
from __future__ import annotations

from collections.abc import Sequence


def implied_prob(decimal_odds: float) -> float:
    """Probabilidad implícita (con vig) de una cuota DECIMAL: p = 1/cuota."""
    if not decimal_odds > 1.0:
        raise ValueError(f"cuota decimal debe ser > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def _validate(implied: Sequence[float]) -> None:
    if len(implied) < 2:
        raise ValueError(f"se requieren >=2 outcomes, got {len(implied)}")
    for p in implied:
        if not (0.0 < p < 1.0):
            raise ValueError(f"cada probabilidad implícita debe estar en (0,1), got {p}")


def overround(implied: Sequence[float]) -> float:
    """Vig total = Σp − 1 (≈ 0.04–0.08 típico). Negativo ⇒ sin vig (o arbitraje)."""
    return sum(implied) - 1.0


def remove_vig_multiplicative(implied: Sequence[float]) -> list[float]:
    """Método MULTIPLICATIVO: fair_i = p_i / Σp. Siempre suma 1. Primario del M2."""
    _validate(implied)
    total = sum(implied)
    if total <= 0.0:
        raise ValueError("la suma de probabilidades implícitas debe ser > 0")
    return [p / total for p in implied]
