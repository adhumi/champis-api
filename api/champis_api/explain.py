"""Phrases en clair de la fiche d'un point (refonte UX, lot 2, décision du 1er nov. 2026).

Construites depuis les facteurs déjà calculés, avec des seuils génériques indépendants de l'espèce :
une seule source pour l'app et le site, qui ne font qu'afficher (spec › Backend, `GET /weather` ›
`summary`). Le terrain (T) n'est connu que des clients, par les tuiles : la phrase de terrain se
déduit chez eux du libellé de la palette. Aucune valeur d'espèce ici.
"""

from __future__ import annotations

# un facteur multiplicatif sous 1 est un frein actif ; les seuils ci-dessous lisent des facteurs
# normalisés dans [0, 1], jamais des millimètres ni des degrés d'une espèce
SOIL_WET = 0.75
SOIL_DRY = 0.25
MEMORY_DRY = 0.5
TEMP_BAD = 0.3
TRIGGER_FULL = 0.999


def ago(days: int) -> str:
    if days <= 0:
        return "aujourd'hui"
    if days == 1:
        return "hier"
    return f"il y a {days} jours"


def summary(factors: dict[str, float], phi: float, trigger_days: int | None) -> list[str]:
    """Deux à cinq phrases courtes, sans chiffre de score, dans l'ordre : pluie, sol, freins, saison."""
    if phi <= 0:
        return ["hors saison"]
    out: list[str] = []
    trigger = factors.get("trigger", 0.0)
    if trigger <= 0 or trigger_days is None:
        out.append("pas de pluie déclenchante récente")
    elif trigger >= TRIGGER_FULL:
        out.append(f"pluie déclenchante {ago(trigger_days)}")
    else:
        out.append(f"pluie déclenchante {ago(trigger_days)}, hors du meilleur délai")
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
    out.append("pleine saison" if phi >= 1.0 else "en marge de la saison")
    return out
