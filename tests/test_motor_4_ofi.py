"""
Tests del Motor 4 (OFI) en SHADOW — port del M8 de Kalshi.

Cubren: la contribución OFI de top-of-book (Cont et al.), el tracker con
baseline pre-spike / cooldown / ventana, el shadow con medición T+30/T+60 y
firma desde la presión, el best-effort (Lección 7: nunca romper la captura),
los topes (baseline maxlen, MAX_PENDING) y /stats/ofi.
"""
from __future__ import annotations

import pytest
from sqlmodel import select
from starlette.testclient import TestClient

from src.api.health import BotState, app
from src.db.engine import get_session
from src.db.models import OfiSignalRow
from src.motor_4_ofi.detector import OfiTracker, TopOfBook, ofi_contribution
from src.motor_4_ofi.shadow import OfiShadow


def _top(bid=0.50, bid_size=100.0, ask=0.52, ask_size=100.0):
    return TopOfBook(bid=bid, bid_size=bid_size, ask=ask, ask_size=ask_size)


# =====================================================
# Contribución OFI (pura)
# =====================================================


class TestOfiContribution:
    def test_bid_sube_es_presion_compradora(self):
        e = ofi_contribution(_top(bid=0.50), _top(bid=0.51, bid_size=80))
        assert e > 0

    def test_ask_baja_es_presion_vendedora(self):
        e = ofi_contribution(_top(ask=0.52), _top(ask=0.51, ask_size=80))
        assert e < 0

    def test_sin_cambios_de_precio_mide_delta_de_size(self):
        # mismo precio en ambos lados: e = qb - qb' - qa + qa' = (qb-qb') - (qa-qa')
        e = ofi_contribution(
            _top(bid_size=100, ask_size=100), _top(bid_size=150, ask_size=100)
        )
        assert e == pytest.approx(50.0)  # engordó el bid → compradora

    def test_lado_vacio_aporta_cero(self):
        e = ofi_contribution(
            TopOfBook(bid=None, bid_size=0, ask=0.52, ask_size=100),
            TopOfBook(bid=0.50, bid_size=100, ask=0.52, ask_size=100),
        )
        # bid previo None → solo cuenta el lado ask (sin cambio → qa' - qa = 0)
        assert e == pytest.approx(0.0)


# =====================================================
# Tracker: baseline, umbral, cooldown
# =====================================================


def _feed_flat(tracker, token, n, start=0.0, step=1.0):
    """n transiciones sin cambios (contribución 0) para madurar el baseline."""
    t = start
    for _ in range(n):
        tracker.observe(token, _top(), t)
        t += step
    return t


class TestOfiTracker:
    def _tracker(self, z_min=3.0, min_baseline=50, cooldown=120.0):
        return OfiTracker(
            window_sec=60.0, z_min=z_min, min_baseline=min_baseline, cooldown_sec=cooldown
        )

    def test_baseline_inmaduro_no_emite(self):
        tr = self._tracker(min_baseline=50)
        t = _feed_flat(tr, "tk", 10)
        # spike gigante pero con 10 muestras de baseline: nada
        assert tr.observe("tk", _top(bid=0.51, bid_size=99999), t) is None

    def test_baseline_plano_no_divide_por_cero(self):
        tr = self._tracker(min_baseline=30)
        t = _feed_flat(tr, "tk", 60)
        # std=0 (todo el baseline es 0) → None, sin ZeroDivisionError
        assert tr.observe("tk", _top(), t) is None

    def test_spike_emite_y_cooldown_silencia(self):
        tr = self._tracker(min_baseline=30, cooldown=120.0)
        # baseline con ruido chico (contribuciones alternantes de ±1)
        t = 0.0
        size = 100.0
        for i in range(80):
            size += 1.0 if i % 2 == 0 else -1.0
            tr.observe("tk", _top(bid_size=size), t)
            t += 1.0
        sig = tr.observe("tk", _top(bid=0.51, bid_size=5000), t)
        assert sig is not None
        assert sig.pressure == "UP"
        assert abs(sig.zscore) >= 3.0
        # cooldown: otro spike inmediato NO emite (anti-ráfaga)
        assert tr.observe("tk", _top(bid=0.52, bid_size=9000), t + 1) is None

    def test_baseline_tiene_tope(self):
        tr = self._tracker(min_baseline=30)
        _feed_flat(tr, "tk", 2000)
        assert len(tr._by_token["tk"].baseline) <= 1000  # nada sin tope


# =====================================================
# Shadow: medición T+30/T+60 firmada + best-effort
# =====================================================


class _Mid:
    """mid_fn controlable por el test."""

    def __init__(self, value=0.50):
        self.value = value

    def __call__(self, token_id):
        return self.value


def _shadow(mid, **kw):
    return OfiShadow(
        mid_fn=mid,
        window_sec=60.0,
        z_min=kw.get("z_min", 3.0),
        min_baseline=kw.get("min_baseline", 30),
        cooldown_sec=120.0,
    )


def _force_signal(shadow, mid, token="tk", t0=100.0):
    """Madura baseline con ruido y dispara un spike en t0."""
    t = t0 - 80
    size = 100.0
    for i in range(80):
        size += 1.0 if i % 2 == 0 else -1.0
        shadow.observe_top(token, _top(bid_size=size), t)
        t += 1.0
    shadow.observe_top(token, _top(bid=0.51, bid_size=5000), t0)
    return t0


class TestOfiShadow:
    def test_senal_medida_y_firmada_momentum(self, initialized_db):
        mid = _Mid(0.50)
        sh = _shadow(mid)
        t0 = _force_signal(sh, mid)
        assert sh.stats.signals == 1
        mid.value = 0.53  # el precio SIGUIÓ a la presión compradora
        sh.observe_top("tk", _top(), t0 + 35)  # madura mid30
        sh.observe_top("tk", _top(), t0 + 61)  # madura mid60 y persiste
        with get_session() as s:
            rows = list(s.exec(select(OfiSignalRow)))
            assert len(rows) == 1
            assert rows[0].pressure == "UP"
            assert rows[0].move60_pp == pytest.approx(3.0, abs=0.01)  # momentum > 0
            assert rows[0].zscore != 0  # columna PROPIA, no un edge_pct

    def test_book_caido_toda_la_gracia_descarta(self, initialized_db):
        mid = _Mid(0.50)
        sh = _shadow(mid)
        t0 = _force_signal(sh, mid)
        mid.value = None  # book caído
        sh.observe_top("tk", _top(), t0 + 200)  # > MEASURE_GRACE
        assert sh.stats.dropped >= 1
        with get_session() as s:
            assert list(s.exec(select(OfiSignalRow))) == []

    def test_best_effort_jamas_rompe_la_captura(self):
        """Lección 7: un mid_fn que explota se traga y loguea — no propaga."""

        def broken_mid(token_id):
            raise RuntimeError("boom")

        sh = _shadow(broken_mid)
        sh.observe_top("tk", _top(), 1.0)  # no debe lanzar

    def test_max_pending_tope(self, initialized_db):
        mid = _Mid(0.50)
        sh = _shadow(mid)
        sh.MAX_PENDING = 1
        _force_signal(sh, mid, token="tk1", t0=100.0)
        # segunda señal en otro token con pendiente lleno → dropped
        sh2_t0 = 100.0
        t = sh2_t0 - 80
        size = 100.0
        for i in range(80):
            size += 1.0 if i % 2 == 0 else -1.0
            sh.observe_top("tk2", _top(bid_size=size), t)
            t += 1.0
        sh.observe_top("tk2", _top(bid=0.51, bid_size=5000), sh2_t0)
        assert sh.stats.dropped >= 1


# =====================================================
# /stats/ofi
# =====================================================


def test_stats_ofi_mediana_y_conteos(initialized_db):
    BotState.db_initialized = True
    with get_session() as s:
        for move in (-1.0, 0.5, 2.0):
            s.add(
                OfiSignalRow(
                    token_id="tk", pressure="UP", ofi_contracts=100, zscore=3.5,
                    n_baseline=200, mid0=0.5, mid30=0.5, mid60=0.5,
                    move30_pp=move, move60_pp=move,
                )
            )
        s.commit()
    body = TestClient(app).get("/stats/ofi").json()
    assert body["signals_measured"] == 3
    assert body["move60_pp_median"] == pytest.approx(0.5)
    assert body["followed_pressure"] == 2  # los > 0


def test_stats_ofi_vacio_no_rompe(initialized_db):
    BotState.db_initialized = True
    body = TestClient(app).get("/stats/ofi").json()
    assert body["signals_measured"] == 0
    assert body["move60_pp_median"] is None
