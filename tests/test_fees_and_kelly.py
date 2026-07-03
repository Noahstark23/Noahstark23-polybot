"""Tests de la matemática pura (fees reescrito para Polymarket + kelly)."""
from __future__ import annotations

import pytest

from src.math.fees import arb_edge_pct, arb_pair_costs, clob_fee
from src.math.kelly import kelly_fraction, kelly_size_usd


class TestClobFee:
    def test_cero_bps_cero_fee(self):
        assert clob_fee(0.55, 100, 0) == 0.0

    def test_formula_simetrica(self):
        # fee = bps/10000 * min(p, 1-p) * size
        assert clob_fee(0.30, 100, 200) == pytest.approx(0.02 * 0.30 * 100)
        assert clob_fee(0.70, 100, 200) == pytest.approx(0.02 * 0.30 * 100)  # simétrico

    def test_precio_invalido(self):
        with pytest.raises(ValueError):
            clob_fee(0.0, 10, 0)
        with pytest.raises(ValueError):
            clob_fee(1.0, 10, 0)

    def test_size_negativo(self):
        with pytest.raises(ValueError):
            clob_fee(0.5, -1, 0)


class TestArbEdge:
    def test_edge_bruto_sin_costos(self):
        gross, costs, net = arb_edge_pct(0.48, 0.49, fee_rate_bps=0, slippage_buffer=0.0)
        assert gross == pytest.approx(3.0)
        assert costs == 0.0
        assert net == pytest.approx(3.0)

    def test_edge_neto_descuenta_fees_y_slippage(self):
        gross, costs, net = arb_edge_pct(0.48, 0.49, fee_rate_bps=200, slippage_buffer=0.002)
        # fees: 0.02*0.48 + 0.02*0.49 = 0.0194 ; slippage 0.002 => 2.14%
        assert gross == pytest.approx(3.0)
        assert costs == pytest.approx(2.14)
        assert net == pytest.approx(0.86)

    def test_mercado_eficiente_edge_negativo(self):
        _, _, net = arb_edge_pct(0.51, 0.50, fee_rate_bps=0)
        assert net < 0

    def test_costos_por_par(self):
        c = arb_pair_costs(0.40, 0.55, fee_rate_bps=100, slippage_buffer=0.001)
        assert c.fee_yes == pytest.approx(0.01 * 0.40)
        assert c.fee_no == pytest.approx(0.01 * 0.45)
        assert c.total_per_pair == pytest.approx(0.004 + 0.0045 + 0.001)


class TestKelly:
    def test_sin_edge_devuelve_cero(self):
        assert kelly_fraction(0.5, 1.0) == 0.0

    def test_kelly_clasico(self):
        # p=0.6, b=1 -> f* = (0.6 - 0.4)/1 = 0.2
        assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)

    def test_size_respeta_tope_5pct(self):
        # Kelly diría 0.2*0.25=5% -> justo el tope; con p mayor, el tope manda
        assert kelly_size_usd(100, 0.9, 1.0, kelly_multiplier=1.0) == pytest.approx(5.0)

    def test_inputs_invalidos(self):
        with pytest.raises(ValueError):
            kelly_fraction(1.5, 1.0)
        with pytest.raises(ValueError):
            kelly_fraction(0.5, 0.0)
