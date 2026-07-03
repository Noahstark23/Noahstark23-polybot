"""
Modelo de costos de Polymarket (REESCRITO para este venue — ARCHITECTURE.md §2).

Fee del CLOB (cuando aplica; hoy la mayoría de mercados opera a 0 bps):
    fee = (fee_rate_bps / 10_000) * min(price, 1 - price) * size

    Es simétrico: se cobra sobre el lado "barato" del par, en shares para
    compras y en USDC para ventas. Referencia: docs del CLOB de Polymarket.

Además del fee se modela un buffer de slippage: el edge se calcula con el best
ask, pero ejecutar puede mover el precio. El buffer es conservador y el gate F2
lo valida contra costos reales del orderbook (fill-rate y slippage medidos).

Funciones PURAS — sin I/O, sin estado (testeables y sin LLMs, §7).
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SLIPPAGE_BUFFER = 0.002  # 0.2 centavos por leg, conservador


def clob_fee(price: float, size: float, fee_rate_bps: int) -> float:
    """Fee del CLOB en USDC para una operación a `price` por `size` contratos."""
    if not 0.0 < price < 1.0:
        raise ValueError(f"price fuera de (0,1): {price}")
    if size < 0:
        raise ValueError(f"size negativo: {size}")
    return (fee_rate_bps / 10_000.0) * min(price, 1.0 - price) * size


@dataclass(frozen=True)
class ArbCosts:
    """Costos totales de comprar 1 par YES+NO (por contrato)."""

    fee_yes: float
    fee_no: float
    slippage_buffer: float

    @property
    def total_per_pair(self) -> float:
        return self.fee_yes + self.fee_no + self.slippage_buffer


def arb_pair_costs(
    ask_yes: float,
    ask_no: float,
    fee_rate_bps: int,
    slippage_buffer: float = DEFAULT_SLIPPAGE_BUFFER,
) -> ArbCosts:
    """Costos por par del arbitraje intra-mercado (comprar YES y NO)."""
    return ArbCosts(
        fee_yes=clob_fee(ask_yes, 1.0, fee_rate_bps),
        fee_no=clob_fee(ask_no, 1.0, fee_rate_bps),
        slippage_buffer=slippage_buffer,
    )


def arb_edge_pct(
    ask_yes: float,
    ask_no: float,
    fee_rate_bps: int,
    slippage_buffer: float = DEFAULT_SLIPPAGE_BUFFER,
) -> tuple[float, float, float]:
    """
    Edge del arbitraje intra-mercado (§6 F2):
        gross = 1.0 - (ask_yes + ask_no)          [en unidades de precio]
        net   = gross - fees - slippage

    Devuelve (gross_edge_pct, costs_pct, net_edge_pct), todo *100.
    """
    gross = 1.0 - (ask_yes + ask_no)
    costs = arb_pair_costs(ask_yes, ask_no, fee_rate_bps, slippage_buffer).total_per_pair
    return gross * 100.0, costs * 100.0, (gross - costs) * 100.0
