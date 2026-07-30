"""
Tests del Motor 2 (consenso de sportsbooks vía The Odds API) en SHADOW.

Cubren: no-vig, consenso por mediana con mínimo de books, el matcher
conservador (la falla catastrófica es emparejar el partido equivocado), el
pipeline con anti-fantasma, el breaker de cuota y la caché TTL del cliente
(la API es PAGA — incidente Kalshi 2026-07-19: 20k créditos quemados en días),
el parsing del CSV de sports con espacios (bug real del panel de Kalshi) y el
funnel con motor=motor_2.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import select

from src.clients.odds_api import Bookmaker, BookMarket, OddsApiClient, OddsEvent, Outcome
from src.db.engine import get_session
from src.db.models import ConsensusSignal, FunnelSnapshot
from src.marketdata import registry
from src.math.no_vig import implied_prob, overround, remove_vig_multiplicative
from src.motor_2_consensus.engine import ConsensusEngine, consensus_fair_prob
from src.motor_2_consensus.matcher import MarketMatch, match_market, normalize_name
from src.strategies.data_capture import DataCaptureService, WatchedMarket
from src.utils.config import get_settings

NOW = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)


def _event(
    home="New York Yankees",
    away="Boston Red Sox",
    books=3,
    home_odds=1.60,
    away_odds=2.50,
    event_id="ev1",
    commence=NOW,
) -> OddsEvent:
    return OddsEvent(
        id=event_id,
        sport_key="baseball_mlb",
        commence_time=commence,
        home_team=home,
        away_team=away,
        bookmakers=tuple(
            Bookmaker(
                key=f"book{i}",
                markets=(
                    BookMarket(
                        key="h2h",
                        outcomes=(
                            Outcome(name=home, price=home_odds),
                            Outcome(name=away, price=away_odds),
                        ),
                    ),
                ),
            )
            for i in range(books)
        ),
    )


# =====================================================
# Math: no-vig + consenso
# =====================================================


class TestNoVig:
    def test_implied_prob(self):
        assert implied_prob(2.0) == pytest.approx(0.5)
        with pytest.raises(ValueError):
            implied_prob(1.0)

    def test_remove_vig_multiplicativo_suma_1(self):
        # cuotas 1.60/2.50 -> implied 0.625+0.40 = 1.025 (vig 2.5%)
        fair = remove_vig_multiplicative([0.625, 0.40])
        assert sum(fair) == pytest.approx(1.0)
        assert fair[0] == pytest.approx(0.625 / 1.025)

    def test_overround(self):
        assert overround([0.625, 0.40]) == pytest.approx(0.025)

    def test_tres_outcomes_futbol(self):
        fair = remove_vig_multiplicative([0.50, 0.30, 0.28])
        assert sum(fair) == pytest.approx(1.0)


class TestConsensus:
    def test_mediana_entre_books(self):
        out = consensus_fair_prob(_event(books=3), "New York Yankees", min_books=3)
        assert out is not None
        fair, books = out
        assert books == 3
        assert fair == pytest.approx(0.625 / 1.025, abs=1e-4)

    def test_pocos_books_no_es_consenso(self):
        assert consensus_fair_prob(_event(books=2), "New York Yankees", min_books=3) is None

    def test_book_con_cuota_degenerada_se_descarta(self):
        ev = _event(books=3)
        broken = Bookmaker(
            key="roto",
            markets=(
                BookMarket(
                    key="h2h",
                    outcomes=(
                        Outcome(name="New York Yankees", price=1.0),  # cuota inválida
                        Outcome(name="Boston Red Sox", price=2.0),
                    ),
                ),
            ),
        )
        ev = OddsEvent(
            id=ev.id, sport_key=ev.sport_key, commence_time=ev.commence_time,
            home_team=ev.home_team, away_team=ev.away_team,
            bookmakers=(*ev.bookmakers, broken),
        )
        out = consensus_fair_prob(ev, "New York Yankees", min_books=3)
        assert out is not None
        assert out[1] == 3  # el book roto NO entró al consenso


# =====================================================
# Matcher — la falla catastrófica es el match equivocado
# =====================================================


class TestMatcher:
    def test_normaliza_acentos(self):
        assert normalize_name("Perú") == "peru"
        assert normalize_name("Côte d'Ivoire") == "cote d ivoire"

    def test_match_ambos_equipos_y_sujeto_primero(self):
        m = match_market(
            "Will the New York Yankees beat the Boston Red Sox?", None, [_event()]
        )
        assert isinstance(m, MarketMatch)
        assert m.subject_team == "New York Yankees"
        assert m.opponent_team == "Boston Red Sox"

    def test_sujeto_es_el_que_aparece_primero(self):
        m = match_market(
            "Will the Boston Red Sox beat the New York Yankees?", None, [_event()]
        )
        assert isinstance(m, MarketMatch)
        assert m.subject_team == "Boston Red Sox"

    def test_apodo_matchea(self):
        m = match_market("Will the Yankees beat the Red Sox tonight?", None, [_event()])
        assert isinstance(m, MarketMatch)
        assert m.subject_team == "New York Yankees"

    def test_un_solo_equipo_no_matchea(self):
        assert match_market("Will the Yankees win the World Series?", None, [_event()]) == "no_match"

    def test_dos_eventos_posibles_es_ambiguo(self):
        """Doubleheader: dos partidos del mismo par de equipos -> descarte, no adivinanza."""
        evs = [_event(event_id="g1"), _event(event_id="g2", commence=NOW + timedelta(hours=4))]
        assert match_market("Will the Yankees beat the Red Sox?", None, evs) == "ambiguous_match"

    def test_gate_de_fecha_filtra_partido_de_otra_semana(self):
        future = _event(commence=NOW + timedelta(days=20))
        end_iso = NOW.isoformat()
        assert match_market("Will the Yankees beat the Red Sox?", end_iso, [future]) == "no_match"

    def test_palabra_completa_no_substring(self):
        """"Utah" no puede matchear adentro de otra palabra ni un token corto colarse."""
        ev = _event(home="Utah Jazz", away="LA Clippers")
        assert match_market("Will Utahn voters approve prop 12?", None, [ev]) == "no_match"


# =====================================================
# Pipeline del engine
# =====================================================


@pytest.fixture()
def engine(initialized_db):
    OddsApiClient.reset_class_state_for_testing()
    return ConsensusEngine(get_settings())


def _match() -> MarketMatch:
    ev = _event()
    return MarketMatch(ev, ev.home_team, ev.away_team)


class TestEvaluateMatch:
    def test_edge_valido_shadow_recorded(self, engine):
        # fair ~0.6098; ask 0.55 -> ~5.8pp neto: señal YES
        status, sig = engine.evaluate_match(
            "0xc1", "q", _match(), ask_yes=0.55, ask_no=0.46, depth_yes=50, depth_no=50
        )
        assert status == "shadow_recorded"
        assert sig.side == "YES"
        assert sig.books_count == 3
        assert sig.net_edge_pp > 3
        assert sig.theoretical_ev_usd > 0

    def test_lado_no_tambien_se_evalua(self, engine):
        # fair NO = 1-0.6098 = 0.3902; ask_no 0.33 -> ~5.8pp: señal NO
        status, sig = engine.evaluate_match(
            "0xc1", "q", _match(), ask_yes=0.62, ask_no=0.33, depth_yes=50, depth_no=50
        )
        assert status == "shadow_recorded"
        assert sig.side == "NO"

    def test_mercado_alineado_below_min_edge(self, engine):
        status, sig = engine.evaluate_match(
            "0xc1", "q", _match(), ask_yes=0.61, ask_no=0.40, depth_yes=50, depth_no=50
        )
        assert status == "below_min_edge"
        assert sig is None

    def test_anti_fantasma_match_equivocado(self, engine):
        """Un ask de 0.10 contra fair 0.61 = 50pp: eso NO es señal, es un partido
        mal emparejado o un libro roto (backstop del incidente GER vs CUW)."""
        status, sig = engine.evaluate_match(
            "0xc1", "q", _match(), ask_yes=0.10, ask_no=0.95, depth_yes=50, depth_no=50
        )
        assert status == "edge_too_high"
        assert sig.status == "edge_too_high"

    def test_pocos_books(self, engine):
        ev = _event(books=2)
        status, sig = engine.evaluate_match(
            "0xc1", "q", MarketMatch(ev, ev.home_team, ev.away_team),
            ask_yes=0.55, ask_no=0.46, depth_yes=50, depth_no=50,
        )
        assert status == "few_books"

    def test_sin_libro_polymarket(self, engine):
        status, _ = engine.evaluate_match(
            "0xc1", "q", _match(), ask_yes=None, ask_no=None, depth_yes=None, depth_no=None
        )
        assert status == "no_book"

    def test_liquidez_insuficiente(self, engine):
        status, sig = engine.evaluate_match(
            "0xc1", "q", _match(), ask_yes=0.55, ask_no=0.46, depth_yes=3, depth_no=3
        )
        assert status == "low_liquidity"


# =====================================================
# Ciclo: funnel motor_2 + de-dupe + estados sin red
# =====================================================


class _Book:
    def __init__(self, ask):
        self.synced = True
        self.best_ask = type("L", (), {"price": ask, "size": 50.0})()


class _FakeBooks:
    def __init__(self, asks):
        self._asks = asks

    def get_book(self, token_id):
        ask = self._asks.get(token_id)
        return _Book(ask) if ask is not None else None


class _FakeOddsClient:
    """Reemplaza OddsApiClient en el engine: devuelve eventos fijos sin red."""

    events: list[OddsEvent] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_odds(self, sport_key):
        return list(self.events)


@pytest.fixture()
def capture_sports(initialized_db, isolated_env):
    isolated_env.setenv("ODDS_API_KEY", "test-key-nunca-loguear")
    import src.utils.config as config_module

    config_module.reset_settings_for_testing()
    svc = DataCaptureService(config_module.get_settings())
    m = WatchedMarket(
        condition_id="0xm1",
        question="Will the New York Yankees beat the Boston Red Sox?",
        token_id_yes="y1",
        token_id_no="n1",
        end_date_iso=None,
        neg_risk=False,
        tick_size=0.01,
    )
    svc.watched = {m.condition_id: m}
    svc.books = _FakeBooks({"y1": 0.55, "n1": 0.46})
    registry.set_capture(svc)
    yield svc
    registry.set_capture(None)


class TestCycle:
    @pytest.mark.asyncio
    async def test_ciclo_graba_senal_y_funnel_motor_2(self, capture_sports):
        OddsApiClient.reset_class_state_for_testing()
        _FakeOddsClient.events = [_event()]
        engine = ConsensusEngine(get_settings(), client_factory=_FakeOddsClient)
        summary = await engine.cycle()
        assert summary["recorded"] == 1
        with get_session() as s:
            sigs = list(s.exec(select(ConsensusSignal)))
            funnels = list(s.exec(select(FunnelSnapshot)))
            assert len(sigs) == 1
            assert sigs[0].side == "YES"
            assert sigs[0].subject_team == "New York Yankees"
            assert funnels[0].motor == "motor_2"

    @pytest.mark.asyncio
    async def test_dedupe_senal_identica_no_reescribe(self, capture_sports):
        OddsApiClient.reset_class_state_for_testing()
        _FakeOddsClient.events = [_event()]
        engine = ConsensusEngine(get_settings(), client_factory=_FakeOddsClient)
        await engine.cycle()
        summary2 = await engine.cycle()
        assert summary2["skips"].get("dedup_unchanged") == 1
        with get_session() as s:
            assert len(list(s.exec(select(ConsensusSignal)))) == 1

    @pytest.mark.asyncio
    async def test_sin_api_key_no_toca_red(self, initialized_db):
        """El motor encendido sin key es un ciclo no_api_key, no un crash-loop."""
        OddsApiClient.reset_class_state_for_testing()
        engine = ConsensusEngine(get_settings(), client_factory=_FakeOddsClient)
        summary = await engine.cycle()
        assert summary["skips"] == {"no_api_key": 1}


# =====================================================
# Cliente: breaker de cuota + caché TTL (la API es PAGA)
# =====================================================


def _client(handler, monkeypatch, ttl=240.0):
    import src.utils.config as config_module

    monkeypatch.setenv("ODDS_API_KEY", "k")
    monkeypatch.setenv("ODDS_API_CACHE_TTL_SEC", str(ttl))
    config_module.reset_settings_for_testing()
    return OddsApiClient(
        config_module.get_settings(), transport=httpx.MockTransport(handler)
    )


RAW_EVENT = {
    "id": "e1", "sport_key": "baseball_mlb", "commence_time": "2026-07-30T20:00:00Z",
    "home_team": "New York Yankees", "away_team": "Boston Red Sox",
    "bookmakers": [
        {"key": "b1", "markets": [{"key": "h2h", "outcomes": [
            {"name": "New York Yankees", "price": 1.6},
            {"name": "Boston Red Sox", "price": 2.5},
        ]}]},
    ],
}


class TestOddsClient:
    @pytest.mark.asyncio
    async def test_cache_ttl_no_repide_dentro_del_ttl(self, monkeypatch):
        """Incidente Kalshi 2026-07-19: 20k créditos quemados re-pidiendo lo mismo."""
        OddsApiClient.reset_class_state_for_testing()
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=[RAW_EVENT], headers={"x-requests-remaining": "19998"})

        async with _client(handler, monkeypatch) as c:
            e1 = await c.get_odds("baseball_mlb")
            e2 = await c.get_odds("baseball_mlb")
        assert len(calls) == 1  # la segunda salió de caché
        assert len(e1) == 1 and len(e2) == 1
        assert OddsApiClient.quota_remaining == 19998

    @pytest.mark.asyncio
    async def test_cuota_agotada_activa_breaker_sin_martillar(self, monkeypatch):
        OddsApiClient.reset_class_state_for_testing()
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, text="Usage quota has been reached: OUT_OF_USAGE_CREDITS")

        async with _client(handler, monkeypatch) as c:
            assert await c.get_odds("baseball_mlb") == []
            assert await c.get_odds("baseball_mlb") == []  # breaker: NI un request más
        assert len(calls) == 1
        assert OddsApiClient.quota_breaker_active() is True

    @pytest.mark.asyncio
    async def test_breaker_se_rearma_con_mes_nuevo(self, monkeypatch):
        OddsApiClient.reset_class_state_for_testing()
        OddsApiClient._quota_exhausted_at = datetime(2026, 7, 30, tzinfo=UTC)
        assert OddsApiClient.quota_breaker_active(datetime(2026, 8, 1, tzinfo=UTC)) is False

    @pytest.mark.asyncio
    async def test_evento_malformado_no_tira_el_batch(self, monkeypatch):
        OddsApiClient.reset_class_state_for_testing()

        def handler(request):
            return httpx.Response(200, json=[RAW_EVENT, {"id": "roto"}])

        async with _client(handler, monkeypatch) as c:
            events = await c.get_odds("baseball_mlb")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_sin_key_no_toca_red(self, monkeypatch):
        OddsApiClient.reset_class_state_for_testing()
        import src.utils.config as config_module

        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        config_module.reset_settings_for_testing()

        def handler(request):  # pragma: no cover - no debe llamarse
            raise AssertionError("no debería tocar la red sin key")

        async with OddsApiClient(
            config_module.get_settings(), transport=httpx.MockTransport(handler)
        ) as c:
            assert await c.get_odds("baseball_mlb") == []


# =====================================================
# Config
# =====================================================


class TestConfig:
    def test_sport_keys_csv_con_espacios(self, isolated_env):
        """Bug real del panel de Kalshi: 'baseball_mlb ,soccer_x' dejaba el
        espacio DENTRO del valor. La property lo limpia."""
        isolated_env.setenv("ODDS_API_SPORT_KEYS", "baseball_mlb ,  soccer_epl, ")
        import src.utils.config as config_module

        config_module.reset_settings_for_testing()
        assert config_module.get_settings().odds_api_sport_keys == [
            "baseball_mlb",
            "soccer_epl",
        ]

    def test_motor_2_default_apagado(self, settings):
        assert settings.MOTOR_2_CONSENSUS_ENABLED is False
        assert settings.ODDS_API_KEY == ""
