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


# =====================================================
# Motor 2 — arbitraje multi-outcome (neg-risk)
# =====================================================


@dataclass(frozen=True)
class MultiEdge:
    """
    Edge de un set multi-outcome. UNIDADES (lección Kalshi 2026-07-28 — una
    columna llamada *_pct que no guarda un porcentaje envenena a todo el que
    la lea): TODOS los *_pct de acá son % DEL CAPITAL COMPROMETIDO POR SET
    (cost_per_set). No son "unidades de precio × 100" como en el binario,
    porque un set buy_no_all de N patas cuesta ~N−1, no ~1: sin normalizar
    por el costo, el mismo número significaría cosas distintas según N.
    """

    direction: str  # "buy_yes_all" | "buy_no_all"
    legs: int
    cost_per_set: float  # USDC por set (suma de asks)
    payout_per_set: float  # USDC que paga el set al resolver (1.0 o N-1)
    fees_per_set: float  # fees CLOB de todas las patas + slippage por pata
    gross_edge_pct: float  # (payout - cost) / cost * 100
    net_edge_pct: float  # (payout - cost - fees) / cost * 100

    @property
    def costs_pct(self) -> float:
        return round(self.gross_edge_pct - self.net_edge_pct, 6)

    @property
    def net_usd_per_set(self) -> float:
        return self.payout_per_set - self.cost_per_set - self.fees_per_set


def multi_outcome_edge(
    asks: list[float],
    direction: str,
    fee_rate_bps: int,
    slippage_buffer: float = DEFAULT_SLIPPAGE_BUFFER,
) -> MultiEdge:
    """
    Edge de un evento neg-risk de N outcomes mutuamente excluyentes donde
    EXACTAMENTE UNO resuelve YES (la tesis del Motor 1 de Kalshi extendida —
    su detección multi-outcome midió edge real de 3.13pp; lo que falló allá
    fue la ejecución, no la matemática):

        buy_yes_all: comprar YES en las N patas cuesta Σ ask_YES_i,
                     paga 1.00 seguro          → gross = 1 − Σ
        buy_no_all:  comprar NO en las N patas cuesta Σ ask_NO_i,
                     pagan las N−1 que pierden → gross = (N−1) − Σ

    Función PURA. Los fees usan la fórmula oficial del CLOB por pata
    (lección: fee exacto desde el día 1 — en Kalshi estuvo ~100× subestimado
    semanas y TODO el edge histórico era artefacto) y el slippage buffer se
    cobra POR PATA: N patas = N oportunidades de que el libro se mueva.
    """
    if direction not in ("buy_yes_all", "buy_no_all"):
        raise ValueError(f"direction inválida: {direction}")
    if len(asks) < 2:
        raise ValueError(f"un set multi-outcome necesita >=2 patas, llegaron {len(asks)}")
    n = len(asks)
    cost = sum(asks)
    payout = 1.0 if direction == "buy_yes_all" else float(n - 1)
    fees = sum(clob_fee(a, 1.0, fee_rate_bps) for a in asks) + slippage_buffer * n
    gross_pct = (payout - cost) / cost * 100.0 if cost > 0 else 0.0
    net_pct = (payout - cost - fees) / cost * 100.0 if cost > 0 else 0.0
    return MultiEdge(
        direction=direction,
        legs=n,
        cost_per_set=round(cost, 6),
        payout_per_set=payout,
        fees_per_set=round(fees, 6),
        gross_edge_pct=round(gross_pct, 4),
        net_edge_pct=round(net_pct, 4),
    )
