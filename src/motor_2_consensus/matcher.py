"""
Matcher Polymarket ↔ The Odds API (Motor 2).

Principio rector (heredado del matcher del M2 de Kalshi, probado en
producción): emparejar el partido EQUIVOCADO es una falla silenciosa
catastrófica — compararías el precio del mercado A contra la probabilidad
justa del partido B y el "edge" resultante parece real. Por eso el matcher es
deliberadamente CONSERVADOR: ante cualquier ambigüedad → descarta y el funnel
lo cuenta. Un match perdido cuesta una señal; un match falso cuesta el motor.

Diferencia con Kalshi: allá el evento trae outcomes estructurados y el match
es por CONJUNTOS de nombres. En Polymarket el mercado es una PREGUNTA binaria
("Will the Yankees beat the Red Sox?"), así que acá:

  1. Normalización con plegado de acentos (NFKD) — portada tal cual.
  2. Un evento matchea si AMBOS equipos aparecen en la pregunta normalizada.
  3. El lado YES es el equipo que aparece PRIMERO en la pregunta (convención
     "Will X beat Y" / "X to win"). Si los dos aparecen en la misma posición
     (imposible) o alguno no aparece → no hay match.
  4. Si DOS O MÁS eventos matchean la misma pregunta (doubleheaders, equipos
     con nombres contenidos) → AMBIGUO, se descarta entero.
  5. Gate temporal: si el mercado tiene end_date_iso, el commence_time del
     evento tiene que caer en [end − 3d, end + 1d]. Un partido de la semana
     que viene no es el de esta pregunta.

Lógica PURAMENTE SÍNCRONA: sin red, sin capital, sin ejecución.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.clients.odds_api import OddsEvent

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_SPACES_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """minúsculas + plegado de acentos (NFKD) + sin puntuación + espacios colapsados."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    lowered = _PUNCT_RE.sub(" ", folded.lower())
    return _SPACES_RE.sub(" ", lowered).strip()


def _find_team(question_norm: str, team: str) -> int:
    """Posición del equipo en la pregunta normalizada (como PALABRAS completas,
    no substring: "Utah" no puede matchear dentro de "Utahn"). -1 si no aparece.

    Fallback por SUFIJOS progresivos del nombre: "New York Yankees" también
    matchea como "yankees", y "Boston Red Sox" como "red sox" (el apodo real
    puede ser más de una palabra). Un sufijo de menos de 4 letras no cuenta
    (evita que "fc", "de" o siglas cortas emparejen cualquier cosa)."""
    team_norm = normalize_name(team)
    if not team_norm:
        return -1
    words = team_norm.split(" ")
    # Sufijos del más largo al más corto: nombre completo primero
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        if len(candidate) < 4:
            break  # sufijos aún más cortos serán más chicos todavía
        m = re.search(rf"\b{re.escape(candidate)}\b", question_norm)
        if m is not None:
            return m.start()
    return -1


def _within_date_gate(end_date_iso: str | None, commence: datetime) -> bool:
    """Mercado con fecha → el partido tiene que ser de ESA ventana."""
    if not end_date_iso:
        return True  # sin fecha no se puede gatear (el match por nombres decide)
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
    except ValueError:
        return True
    return end - timedelta(days=3) <= commence <= end + timedelta(days=1)


@dataclass(frozen=True)
class MarketMatch:
    """Resultado de emparejar un mercado de Polymarket con un evento de odds."""

    event: OddsEvent
    subject_team: str  # el equipo que el YES del mercado afirma
    opponent_team: str


def match_market(
    question: str,
    end_date_iso: str | None,
    events: list[OddsEvent],
) -> MarketMatch | str:
    """
    Empareja una pregunta binaria con UN evento. Devuelve MarketMatch o el
    motivo del descarte como string (lo consume el funnel): "no_match" |
    "ambiguous_match".
    """
    q = normalize_name(question)
    candidates: list[MarketMatch] = []
    for ev in events:
        if not _within_date_gate(end_date_iso, ev.commence_time):
            continue
        pos_home = _find_team(q, ev.home_team)
        pos_away = _find_team(q, ev.away_team)
        if pos_home < 0 or pos_away < 0:
            continue  # los DOS equipos tienen que estar en la pregunta
        if pos_home < pos_away:
            candidates.append(MarketMatch(ev, ev.home_team, ev.away_team))
        else:
            candidates.append(MarketMatch(ev, ev.away_team, ev.home_team))

    if not candidates:
        return "no_match"
    if len(candidates) > 1:
        # Doubleheader / nombres contenidos: elegir "el más probable" es
        # exactamente la falla catastrófica. Se descarta.
        return "ambiguous_match"
    return candidates[0]
