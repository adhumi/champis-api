"""Mesure minimale (spec › Produit › Mesure minimale ; étape C, lot C2, 6 nov. 2026).

Quelques compteurs agrégés par jour civil (heure de Paris), calculés côté serveur à partir de ce
que Champis reçoit ou stocke déjà : rien n'est lu ni écrit sur l'appareil pour mesurer, pas de
cookie, pas d'identifiant nouveau, pas d'outil tiers, aucune adresse IP conservée. Table
`daily_counts` (jour, nom, valeur) de la même base que les observations, gardée 13 mois, lue par
Adrien seul (`champis-api stats`, une ligne dans `doctor`).

- `web_map_opens` : ouvertures de la carte du site, comptées par l'API sur le `GET /tiles` que la
  page fait une fois à l'ouverture, reconnu à son en-tête Origin (le site est sur champis.app, l'API
  sur api.champis.app depuis le découpage des dépôts ; l'app iOS n'envoie pas d'Origin) ;
- `observations_found`, `observations_not_found` : observations reçues ce jour-là (nouvelles seulement) ;
- `devices_with_coins`, `coins` : relevés à chaque notification du matin ;
- `notify_sent`, `notify_failed` : notifications de ce jour-là ;
- `warm_cells`, `cells_without_weather` : bilan de la dernière pré-chauffe du jour ;
- `http_429`, `http_5xx` : réponses refusées par le limiteur et erreurs du serveur ce jour-là, en
  nombre seulement (backoffice › Santé, lot BO1, 8 nov. 2026) ;
- `active_devices` : appareils de l'app vus ce jour-là, relevé le lendemain matin par la
  notification depuis `device_days` (backoffice › Usage, lot BO2, tranché par Adrien le 8 nov. 2026) ;
- `layer_app:<cible>`, `layer_web:<cible>` : requêtes météo (`/weather`, `/weather/bbox`,
  `/weather/field`) par espèce ou groupe affiché, app (en-tête `X-Champis-User` présent) et site
  (`Origin` du site) séparés ; la part de chaque espèce sur la carte (lot BO2).

Appareils actifs (lot BO2) : l'app envoie son identifiant pseudonyme sur toutes ses requêtes ;
l'API n'en garde qu'une empreinte HMAC par jour (`device_hash`, secret `CHAMPIS_MEASURE_SECRET`),
jamais l'identifiant, dans `device_days` : actifs du jour, de la semaine, du mois et uniques sur
13 mois se comptent dessus. Un appareil hors ligne ne compte pas. Changer le secret remet
l'historique à zéro.

Les soutiens et les coûts restent un relevé manuel mensuel.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
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
    "http_429": "429",
    "http_5xx": "5xx",
    "active_devices": "actifs",
}

LAYER_PATHS = (
    "/weather",
    "/weather/bbox",
    "/weather/field",
)  # ce que les clients demandent par couche
ALL_SPECIES = "toutes"  # requête sans `species` : toutes les espèces
DEVICE_HASH_LENGTH = 32  # 16 octets en hexadécimal


def device_hash(secret: str, user_id: str) -> str:
    """Empreinte HMAC-SHA256 de l'identifiant de l'app, tronquée : ni l'identifiant ni rien qui
    s'y recoupe sans le secret."""
    digest = hmac.new(
        secret.encode("utf-8"), user_id.strip().lower().encode("utf-8"), hashlib.sha256
    )
    return digest.hexdigest()[:DEVICE_HASH_LENGTH]


def valid_user_id(value: str | None) -> str | None:
    """L'identifiant tel que l'app l'envoie (UUID), ou None."""
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class DeviceDayTracker:
    """Par worker : les (jour, empreinte) déjà écrits, pour n'écrire chaque appareil qu'une fois
    par jour ; vidé au changement de jour."""

    def __init__(self) -> None:
        self.day: date | None = None
        self.seen: set[str] = set()

    def first_today(self, day: date, hashed: str) -> bool:
        if day != self.day:
            self.day, self.seen = day, set()
        if hashed in self.seen:
            return False
        self.seen.add(hashed)
        return True


def layer_targets(species: list[str], groups: dict[str, list[str]]) -> list[str]:
    """La cible affichée : le groupe si la liste demandée est exactement un groupe, sinon chaque
    espèce ; `toutes` sans liste."""
    if not species:
        return [ALL_SPECIES]
    wanted = sorted(set(species))
    for gid, members in groups.items():
        if wanted == sorted(members):
            return [gid]
    return wanted


def layer_counter(client: str, target: str) -> str:
    return f"layer_{client}:{target}"


def layer_shares(
    counts: dict[date, dict[str, int]], end: date, days: int
) -> dict[str, dict[str, int]]:
    """`{client: {cible: total}}` sur les `days` jours qui se terminent à `end`."""
    since = end - timedelta(days=days - 1)
    out: dict[str, dict[str, int]] = {}
    for d, values in counts.items():
        if not since <= d <= end:
            continue
        for name, n in values.items():
            if not name.startswith("layer_") or ":" not in name:
                continue
            client, target = name[len("layer_") :].split(":", 1)
            out.setdefault(client, {})
            out[client][target] = out[client].get(target, 0) + n
    return out


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
        # la veille est complète : son nombre d'appareils actifs devient un compteur (lot BO2)
        yesterday = day - timedelta(days=1)
        active = store.active_devices(yesterday, 1)
        if active:
            store.set_count(yesterday, "active_devices", active)
        store.purge_counts(day - timedelta(days=KEEP_DAYS))
        store.purge_device_days(day - timedelta(days=KEEP_DAYS))
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
    if "active_devices" in v:
        parts.append(f"{v['active_devices']} appareils actifs")
    if "devices_with_coins" in v:
        parts.append(f"{v['devices_with_coins']} appareils avec coins ({v.get('coins', 0)} coins)")
    if "notify_sent" in v or "notify_failed" in v:
        parts.append(f"{v.get('notify_sent', 0)} notifications, {v.get('notify_failed', 0)} échecs")
    if "cells_without_weather" in v:
        parts.append(f"{v['cells_without_weather']} cellules sans météo")
    return f"{day.isoformat()} : " + ", ".join(parts)
