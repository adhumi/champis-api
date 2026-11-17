"""Phrases en clair de la fiche d'un point (refonte UX, lot 2, décision du 1er nov. 2026).

Construites depuis les facteurs déjà calculés, avec des seuils génériques indépendants de l'espèce :
une seule source pour l'app et le site, qui ne font qu'afficher (spec › Backend, `GET /weather` ›
`summary`). Le terrain (T) n'est connu que des clients, par les tuiles : la phrase de terrain se
déduit chez eux du libellé de la palette. Aucune valeur d'espèce ici.

`summary` donne une liste de phrases courtes ; `sentence` (17 nov. 2026) les mêmes faits en une
phrase rédigée. Un fait ajouté à l'une s'ajoute à l'autre (un test compare leur nombre).
"""

from __future__ import annotations

# un facteur multiplicatif sous 1 est un frein actif ; les seuils ci-dessous lisent des facteurs
# normalisés dans [0, 1], jamais des millimètres ni des degrés d'une espèce
SOIL_WET = 0.75
SOIL_DRY = 0.25
MEMORY_DRY = 0.5
TEMP_BAD = 0.3


def ago(days: int) -> str:
    if days <= 0:
        return "aujourd'hui"
    if days == 1:
        return "hier"
    return f"il y a {days} jours"


SEASON_WORDS = {
    "hors": "hors saison",
    "debut": "début de saison",
    "pleine": "pleine saison",
    "fin": "fin de saison",
}


def rain_phrase(trigger_days: int | None, lag: dict[str, float] | None) -> str:
    """La grosse pluie retenue et ce qu'elle annonce, d'après le délai de pousse de l'espèce
    (`weather.trigger.lag_days` de son fichier : jamais une valeur écrite ici). Étape B2, 6 nov. 2026 :
    « trop tôt, pousse attendue dans 6 à 16 jours », « en plein dans le délai de pousse », « la pousse
    s'essouffle »."""
    if trigger_days is None:
        return "pas de grosse pluie récente"
    head = f"grosse pluie {ago(trigger_days)}"
    if not lag:
        return head
    low, high = round(lag["opt_low"]), round(lag["opt_high"])
    if trigger_days < low:
        return f"{head} : trop tôt, pousse attendue dans {low - trigger_days} à {high - trigger_days} jours"
    if trigger_days <= high:
        return f"{head} : en plein dans le délai de pousse"
    return f"{head} : la pousse s'essouffle"


def summary(
    factors: dict[str, float],
    phi: float,
    trigger_days: int | None,
    lag: dict[str, float] | None = None,
    phase: str | None = None,
) -> list[str]:
    """Deux à cinq phrases courtes, sans chiffre de score, dans l'ordre : pluie, sol, freins, saison.

    `lag` : `lag_days` de l'espèce (sens du délai de pousse) ; `phase` : position du jour dans la
    saison (`scoring.season_phase`). Sans eux, les phrases restent justes mais moins précises."""
    if phi <= 0:
        return ["hors saison"]
    out: list[str] = [rain_phrase(trigger_days, lag)]
    soil = factors.get("soil_moisture")
    if soil is not None:
        if soil >= SOIL_WET:
            out.append("sol bien humide")
        elif soil >= SOIL_DRY:
            out.append("sol moyennement humide")
        else:
            out.append("sol sec")
    if factors.get("frost", 1.0) < 1.0:
        out.append("gel ces dernières nuits")
    if factors.get("heat", 1.0) < 1.0:
        out.append("trop chaud ces derniers jours")
    if factors.get("water_balance", 1.0) < 1.0:
        out.append("le sol sèche, plus d'évaporation que de pluie")
    if factors.get("memory", 1.0) < MEMORY_DRY:
        out.append("peu de pluie ces derniers mois")
    temp = factors.get("temp")
    if temp is not None and temp < TEMP_BAD:
        out.append("températures défavorables")
    soil_temp = factors.get("soil_temp")  # facteur optionnel (9 nov. 2026)
    if soil_temp is not None and soil_temp < TEMP_BAD:
        out.append("sol trop froid ou trop chaud")
    if phase in SEASON_WORDS:
        out.append(SEASON_WORDS[phase])
    else:
        out.append("pleine saison" if phi >= 1.0 else "en marge de la saison")
    return out


# ---------------------------------------------------------------- la même chose en une phrase


def _join(parts: list[str]) -> str:
    """« a », « a et b », « a, b et c »."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " et " + parts[-1]


def _in_days(low: int, high: int) -> str:
    if low == high:
        return f"dans {low} jour{'s' if low > 1 else ''}"
    return f"dans {low} à {high} jours"


def rain_clause(trigger_days: int | None, lag: dict[str, float] | None) -> str:
    """La grosse pluie en proposition rédigée : mêmes cas que `rain_phrase`, mêmes bornes lues dans `lag_days` de l'espèce."""
    if trigger_days is None:
        return "il n'y a pas eu de grosse pluie récemment"
    if trigger_days <= 0:
        head = "la dernière grosse pluie est tombée aujourd'hui"
    elif trigger_days == 1:
        head = "la dernière grosse pluie est tombée hier"
    else:
        head = f"la dernière grosse pluie remonte à {trigger_days} jours"
    if not lag:
        return head
    low, high = round(lag["opt_low"]), round(lag["opt_high"])
    if trigger_days < low:
        wait = _in_days(low - trigger_days, high - trigger_days)
        return f"{head}, c'est trop tôt et la pousse est attendue {wait}"
    if trigger_days <= high:
        return f"{head}, en plein dans le délai de pousse"
    return f"{head} et la pousse s'essouffle"


def sentence(
    factors: dict[str, float],
    phi: float,
    trigger_days: int | None,
    lag: dict[str, float] | None = None,
    phase: str | None = None,
) -> str:
    """`summary` rédigé en une phrase (spec › App iOS › Fiche d'un point, 17 nov. 2026) : la grosse
    pluie, puis ce qui joue en faveur, puis « mais » et ce qui freine. Sans majuscule initiale ni
    point final : le client l'écrit derrière le terrain du pixel (« Terrain favorable : … »), qu'il
    est seul à connaître. Mêmes arguments et mêmes seuils génériques que `summary`."""
    if phi <= 0:
        return "hors saison pour cette espèce"
    rain = rain_clause(trigger_days, lag)
    good: list[str] = []
    bad: list[str] = []
    soil = factors.get("soil_moisture")
    if soil is not None:
        if soil >= SOIL_WET:
            good.append("le sol est bien humide")
        elif soil >= SOIL_DRY:
            good.append("le sol est moyennement humide")
        else:
            bad.append("le sol est sec")
    if factors.get("frost", 1.0) < 1.0:
        bad.append("il a gelé ces dernières nuits")
    if factors.get("heat", 1.0) < 1.0:
        bad.append("il a fait trop chaud ces derniers jours")
    if factors.get("water_balance", 1.0) < 1.0:
        bad.append("il s'évapore plus d'eau qu'il n'en tombe")
    if factors.get("memory", 1.0) < MEMORY_DRY:
        bad.append("il a peu plu ces derniers mois")
    temp = factors.get("temp")
    if temp is not None and temp < TEMP_BAD:
        bad.append("les températures sont défavorables")
    soil_temp = factors.get("soil_temp")
    if soil_temp is not None and soil_temp < TEMP_BAD:
        bad.append("le sol est trop froid ou trop chaud")
    if phase == "debut":
        good.append("la saison commence")
    elif phase == "fin":
        bad.append("la saison touche à sa fin")
    elif phase == "pleine" or (phase not in SEASON_WORDS and phi >= 1.0):
        good.append("c'est la pleine saison")
    else:
        bad.append("on est en marge de la saison")
    rest = f"{_join(good)}, mais {_join(bad)}" if good and bad else _join(good or bad)
    return f"{rain} ; {rest}"
