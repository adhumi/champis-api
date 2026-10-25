"""Amorçage : historique J−200 → J−1 et climatologie du sol depuis l'archive ERA5-Land gratuite.

À lancer **hors Railway** (`champis-api seed`, depuis un poste dont l'IP n'est pas bridée), une fois
par grille. Le résultat, `data/weather_seed.json`, est versionné et embarqué dans l'image ; l'API
l'importe au démarrage dans SQLite (`history` source `seed`, jamais par-dessus la prévision ;
`soil_clim`). Reprise : les cellules déjà présentes dans le fichier ne sont pas refaites.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .cache import WeatherCache
from .grid import Cell, WeatherGrid
from .weather_client import OpenMeteoClient, _redact, _retry_after

log = logging.getLogger(__name__)

SEED_VERSION = 1
BACKOFF_MIN_S = 60  # premier délai sur un 429 sans Retry-After exploitable
BACKOFF_MAX_S = 900  # les quotas se comptent à l'heure : attendre, pas abandonner


def load_seed(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": SEED_VERSION, "generated": None, "cells": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != SEED_VERSION:
        raise ValueError(f"weather_seed.json version {data.get('version')!r} inconnue")
    return data


def save_seed(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


async def seed(
    grid: WeatherGrid,
    client: OpenMeteoClient,
    out: Path,
    today: date | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Complète `out` avec les cellules de la grille qui y manquent ; écrit après chaque cellule.

    Un 429 (quota de l'archive) ou une coupure réseau ne fait pas échouer l'amorçage :
    on attend (Retry-After, sinon un délai qui double jusqu'à 15 min) et on reprend la même cellule.
    """
    today = today or client.today()
    data = load_seed(out)
    todo = [c for c in grid.cells.values() if c.id not in data["cells"]]
    log.info("amorçage : %d cellules à faire, %d déjà présentes", len(todo), len(data["cells"]))
    for i, cell in enumerate(todo, 1):
        rows, clim = await _fetch_patiently(client, cell, today, sleep)
        data["cells"][cell.id] = {"history": rows, "soil_clim": clim["months"]}
        data["generated"] = today.isoformat()
        save_seed(out, data)
        log.info("amorçage %d/%d : %s (%d jours)", i, len(todo), cell.id, len(rows))
    return data


async def _fetch_patiently(
    client: OpenMeteoClient, cell: Cell, today: date, sleep: Callable[[float], Awaitable[None]]
) -> tuple[dict[str, dict], dict[str, Any]]:
    backoff = BACKOFF_MIN_S
    while True:
        try:
            return await client.fetch_seed(cell, today)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            ra = _retry_after(e)
            if ra is None and not isinstance(e, httpx.TransportError):
                raise  # 4xx/5xx autre qu'un quota : ce n'est pas en attendant que ça passera
            wait = max(ra or 0, backoff)
            log.warning("%s : %s — reprise dans %d s", cell.id, _redact(e), wait)
            await sleep(wait)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def import_seed(cache: WeatherCache, path: Path) -> int:
    """Charge le fichier d'amorçage dans SQLite. Renvoie le nombre de cellules importées."""
    data = load_seed(path)
    for cid, c in data["cells"].items():
        cache.put_history(cid, c["history"], "seed")
        cache.put_soil_clim(cid, c["soil_clim"])
    return len(data["cells"])
