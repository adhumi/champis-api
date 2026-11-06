"""Mesure minimale (spec › Produit › Mesure minimale ; étape C, lot C2, 6 nov. 2026).

Quelques compteurs agrégés par jour civil (heure de Paris), calculés côté serveur à partir de ce
que Champis reçoit ou stocke déjà : rien n'est lu ni écrit sur l'appareil pour mesurer, pas de
cookie, pas d'identifiant nouveau, pas d'outil tiers, aucune adresse IP conservée. Table
`daily_counts` (jour, nom, valeur) de la même base que les observations, gardée 13 mois, lue par
Adrien seul (`champis-api stats`, une ligne dans `doctor`).

- `web_map_opens` : ouvertures de `/web/carte.html` (réponses 200 ou 304), par l'API qui sert le site ;
- `observations_found`, `observations_not_found` : observations reçues ce jour-là (nouvelles seulement) ;
- `devices_with_coins`, `coins` : relevés à chaque notification du matin ;
- `notify_sent`, `notify_failed` : notifications de ce jour-là ;
- `warm_cells`, `cells_without_weather` : bilan de la dernière pré-chauffe du jour.

Les soutiens et les coûts restent un relevé manuel mensuel.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

PARIS = ZoneInfo("Europe/Paris")
KEEP_DAYS = 400  # 13 mois, puis effacés

# nom → en-tête court du tableau de `champis-api stats`
COUNTERS: dict[str, str] = {
    "web_map_opens": "carte",
    "observations_found": "trouvé",
    "observations_not_found": "rien",
    "devices_with_coins": "appareils",
    "coins": "coins",
    "notify_sent": "notif.",
    "notify_failed": "échecs",
    "warm_cells": "cellules",
    "cells_without_weather": "sans météo",
}


def today(now: datetime | None = None) -> date:
    """Jour civil à Paris : la pré-chauffe et les notifications suivent l'heure française."""
    return (now or datetime.now(PARIS)).astimezone(PARIS).date()


def bump_quietly(store: Store, name: str, n: int = 1, day: date | None = None) -> None:
    """Un compteur ne doit jamais faire échouer ce qu'il compte : une erreur est journalisée."""
    try:
        store.bump(day or today(), name, n)
    except Exception:
        log.exception("mesure : compteur %s non enregistré", name)


def record_notify(store: Store, sent: int, failed: int, day: date | None = None) -> None:
    """Après la notification du matin : envoyées, échecs, et l'état des coins ce jour-là."""
    day = day or today()
    try:
        devices, n_coins = store.coin_totals()
        store.bump(day, "notify_sent", sent)
        store.bump(day, "notify_failed", failed)
        store.set_count(day, "devices_with_coins", devices)
        store.set_count(day, "coins", n_coins)
        store.purge_counts(day - timedelta(days=KEEP_DAYS))
    except Exception:
        log.exception("mesure : bilan des notifications non enregistré")


def record_warm(store: Store, cells: int, unavailable: int, day: date | None = None) -> None:
    """Après une pré-chauffe : la dernière du jour l'emporte."""
    day = day or today()
    try:
        store.set_count(day, "warm_cells", cells)
        store.set_count(day, "cells_without_weather", unavailable)
    except Exception:
        log.exception("mesure : bilan de la pré-chauffe non enregistré")


def rows(counts: dict[date, dict[str, int]], days: list[date]) -> list[list[str]]:
    """Une ligne par jour, du plus récent au plus ancien ; « · » = rien de relevé ce jour-là."""
    out = []
    for d in sorted(days, reverse=True):
        values = counts.get(d, {})
        out.append([d.isoformat(), *(str(values[k]) if k in values else "·" for k in COUNTERS)])
    return out


def header() -> tuple[str, ...]:
    return ("jour", *COUNTERS.values())


def summary(counts: dict[date, dict[str, int]], day: date) -> str:
    """Ligne de `doctor` : la veille, en mots."""
    v = counts.get(day, {})
    if not v:
        return f"rien de relevé le {day.isoformat()}"
    parts = [
        f"{v.get('web_map_opens', 0)} ouvertures de la carte",
        f"{v.get('observations_found', 0) + v.get('observations_not_found', 0)} observations",
    ]
    if "devices_with_coins" in v:
        parts.append(f"{v['devices_with_coins']} appareils avec coins ({v.get('coins', 0)} coins)")
    if "notify_sent" in v or "notify_failed" in v:
        parts.append(f"{v.get('notify_sent', 0)} notifications, {v.get('notify_failed', 0)} échecs")
    if "cells_without_weather" in v:
        parts.append(f"{v['cells_without_weather']} cellules sans météo")
    return f"{day.isoformat()} : " + ", ".join(parts)
