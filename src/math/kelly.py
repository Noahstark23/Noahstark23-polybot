"""
Sizing Kelly fraccional (portado del bot base — matemática pura, §2).

Para el arbitraje intra-mercado el sizing real lo domina la liquidez del libro
y el tope del 5% por trade; kelly_size queda para motores direccionales futuros.
"""
from __future__ import annotations


def kelly_fraction(win_prob: float, payout_ratio: float) -> float:
    """
    Fracción Kelly clásica: f* = (b*p - q) / b
    donde b = payout_ratio (ganancia/pérdida), p = win_prob, q = 1-p.
    Devuelve 0 si el edge es negativo.
    """
    if not 0.0 <= win_prob <= 1.0:
        raise ValueError(f"win_prob fuera de [0,1]: {win_prob}")
    if payout_ratio <= 0:
        raise ValueError(f"payout_ratio debe ser positivo: {payout_ratio}")
    q = 1.0 - win_prob
    f = (payout_ratio * win_prob - q) / payout_ratio
    return max(0.0, f)


def kelly_size_usd(
    capital_usd: float,
    win_prob: float,
    payout_ratio: float,
    kelly_multiplier: float = 0.25,
    max_trade_pct: float = 5.0,
) -> float:
    """Size en USD: Kelly fraccional (¼ por default) con tope duro del 5%."""
    f = kelly_fraction(win_prob, payout_ratio) * kelly_multiplier
    size = capital_usd * f
    cap = capital_usd * max_trade_pct / 100.0
    return min(size, cap)
