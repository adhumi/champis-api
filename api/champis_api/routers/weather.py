"""`GET /weather` (une cellule, détail des facteurs) et `GET /weather/bbox` (peindre une emprise)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import list_species
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..cache import WeatherCache
from ..grid import Cell, WeatherGrid
from ..weather_client import OpenMeteoClient
from ..weather_service import HistoryTooShortError, SeriesGapError, cell_weather

router = APIRouter(prefix="/weather", tags=["weather"])

# Cellules d'une emprise de `/weather/bbox` (décision du 2 nov. 2026). Un iPhone en portrait au
# zoom 9, là où la carte passe des départements aux tuiles, en voit ≈ 900 avec sa marge ; un iPad
# en paysage ≈ 3 000 ; une fenêtre Mac de 1 920 pt ≈ 4 500. Au-delà, l'emprise n'est pas celle
# d'un écran : 422 « zoomez ».
MAX_BBOX_CELLS = 5000
# Cellules absentes de la météo précalculée (`cell_weather`, rangée à chaque pré-chauffe) qu'une
# requête accepte de calculer à la demande : ≈ 14 ms par cellule et par espèce, et un lot
# Open-Meteo au plus si elles sont froides. L'ancien plafond de l'emprise entière (17 oct. 2026),
# qui protégeait le quota avant la pré-chauffe ; les autres sont listées `unavailable`.
MAX_BBOX_COMPUTE = 40
UNAVAILABLE_NOT_PRECOMPUTED = "météo pas encore précalculée : prochaine pré-chauffe"


def _grid(request: Request) -> WeatherGrid:
    return request.app.state.champis.grid


def _client(request: Request) -> OpenMeteoClient:
    return request.app.state.champis.client


def _cache(request: Request) -> WeatherCache:
    return request.app.state.champis.cache


def _species(species: Annotated[list[str] | None, Query()] = None) -> list[str]:
    known = list_species()
    if not species:
        return known
    unknown = [s for s in species if s not in known]
    if unknown:
        raise HTTPException(404, f"espèce(s) inconnue(s) : {unknown}")
    return species


Grid = Annotated[WeatherGrid, Depends(_grid)]
Client = Annotated[OpenMeteoClient, Depends(_client)]
Cache = Annotated[WeatherCache, Depends(_cache)]
Species = Annotated[list[str], Depends(_species)]


async def _compute(
    client: OpenMeteoClient, cells: list[Cell], species: list[str], detail: bool
) -> tuple[list[dict[str, Any]], dict[str, str], int | None]:
    """(résultats des cellules disponibles, indisponibles → raison, Retry-After éventuel)."""
    res = await client.fetch_cells_partial(cells)
    today = client.today()
    results, unavailable = [], dict(res.unavailable)
    for c in cells:
        if c.id not in res.raws:
            continue
        try:
            results.append(cell_weather(c, res.raws[c.id], species, today, detail=detail))
        except HistoryTooShortError as e:  # transitoire : l'accumulation comble un jour par jour
            unavailable[c.id] = str(e)
        except SeriesGapError as e:
            raise HTTPException(502, f"séries Open-Meteo incomplètes : {e}") from e
    return results, unavailable, res.retry_after_s


def _retry_headers(retry_after: int | None) -> dict[str, str] | None:
    return {"Retry-After": str(retry_after)} if retry_after else None


@router.get("")
async def get_weather(
    grid: Grid,
    client: Client,
    species: Species,
    cell: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    if cell is not None:
        c = grid.get(cell)
        if c is None:
            raise HTTPException(404, f"cellule inconnue : {cell}")
    elif lat is not None and lon is not None:
        c = grid.nearest(lat, lon)
        if c is None:
            raise HTTPException(404, "grille météo vide")
    else:
        raise HTTPException(422, "préciser cell=<id> ou lat=&lon=")
    results, unavailable, retry_after = await _compute(client, [c], species, detail=True)
    if unavailable:
        reason = next(iter(unavailable.values()))
        raise HTTPException(
            503, f"météo indisponible : {reason}", headers=_retry_headers(retry_after)
        )
    return results[0]


@router.get("/bbox")
async def get_weather_bbox(
    grid: Grid,
    client: Client,
    cache: Cache,
    species: Species,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    day: date | None = None,
) -> dict[str, Any]:
    """Par cellule de l'emprise (id, lat, lon, alt_classes, elevation_m) : `M_eff` et `phi` par classe
    d'altitude, sans détail des facteurs.

    Les cellules sont servies depuis la météo précalculée à la dernière pré-chauffe
    (`cell_weather`, la même passe que l'indice par département), sans recalcul ni appel
    Open-Meteo : une emprise d'écran entière, jusqu'à `MAX_BBOX_CELLS` cellules, tient dans une
    réponse. Une cellule que la pré-chauffe n'a pas (nouvelle, indisponible à ce moment-là) est
    calculée à la demande, au plus `MAX_BBOX_COMPUTE` par requête ; les suivantes sont listées
    dans `unavailable`, comme celles dont la météo manque (Open-Meteo injoignable, rien en cache).
    `computed_at` horodate le précalcul servi (ou la requête, si tout a été calculé à la demande).

    `day` filtre sur un seul jour (défaut : les 11 jours J−3 → J+7). Entre minuit et la pré-chauffe
    du matin, les lignes précalculées la veille couvrent J−4 → J+6 : J+7 manque jusqu'à 3 h.
    `retry_after` (s) quand Open-Meteo l'a indiqué.
    """
    cells = grid.in_bbox(min_lon, min_lat, max_lon, max_lat)
    if len(cells) > MAX_BBOX_CELLS:
        raise HTTPException(
            422, f"{len(cells)} cellules dans l'emprise, maximum {MAX_BBOX_CELLS} : zoomez"
        )
    stored = cache.cell_weather((c.id for c in cells), species)
    ready = [c for c in cells if all((c.id, sid) in stored for sid in species)]
    ready_ids = {c.id for c in ready}
    missing = [c for c in cells if c.id not in ready_ids]
    to_compute, deferred = missing[:MAX_BBOX_COMPUTE], missing[MAX_BBOX_COMPUTE:]

    results: dict[str, dict[str, Any]] = {}
    for c in ready:
        results[c.id] = {
            "cell": c.as_dict(),
            "stale": any(stored[c.id, sid].stale for sid in species),
            "species": {sid: stored[c.id, sid].rows for sid in species},
        }
    computed_at = max(
        (stored[c.id, sid].computed_at for c in ready for sid in species), default=None
    )

    unavailable: dict[str, str] = {c.id: UNAVAILABLE_NOT_PRECOMPUTED for c in deferred}
    retry_after = None
    if to_compute:
        computed, failed, retry_after = await _compute(client, to_compute, species, detail=False)
        unavailable |= failed
        now = client.now()
        today = client.today()
        for r in computed:
            results[r["cell"]["id"]] = {
                "cell": r["cell"],
                "stale": r["stale"],
                "species": r["species"],
            }
        cache.put_cell_weather(
            (
                (r["cell"]["id"], sid, r["stale"], rows)
                for r in computed
                for sid, rows in r["species"].items()
            ),
            today,
            now,
        )
        if computed_at is None and computed:
            computed_at = now.isoformat(timespec="seconds")

    out = []
    for c in cells:
        r = results.get(c.id)
        if r is None:
            continue
        sp = {}
        for sid, rows in r["species"].items():
            if day is not None:
                rows = [row for row in rows if row["date"] == day.isoformat()]
            sp[sid] = rows
        out.append({"cell": r["cell"], "stale": r["stale"], "species": sp})
    return {
        "today": client.today().isoformat(),
        "computed_at": computed_at,
        "scoring_version": SCORING_VERSION if out else None,
        "cells": out,
        "unavailable": sorted(unavailable),
        "retry_after": retry_after,
    }
