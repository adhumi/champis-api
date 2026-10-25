"""`GET /weather` (une cellule, détail des facteurs) et `GET /weather/bbox` (peindre une emprise)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from champis_pipeline.species import list_species
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..grid import Cell, WeatherGrid
from ..weather_client import OpenMeteoClient
from ..weather_service import SeriesGapError, cell_weather

router = APIRouter(prefix="/weather", tags=["weather"])

MAX_BBOX_CELLS = 40  # au-delà, zoomer : protège le quota Open-Meteo sur les cellules froides


def _grid(request: Request) -> WeatherGrid:
    return request.app.state.champis.grid


def _client(request: Request) -> OpenMeteoClient:
    return request.app.state.champis.client


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
Species = Annotated[list[str], Depends(_species)]


async def _compute(
    client: OpenMeteoClient, cells: list[Cell], species: list[str], detail: bool
) -> tuple[list[dict[str, Any]], dict[str, str], int | None]:
    """(résultats des cellules disponibles, indisponibles → raison, Retry-After éventuel)."""
    res = await client.fetch_cells_partial(cells)
    today = client.today()
    try:
        results = [
            cell_weather(c, res.raws[c.id], species, today, detail=detail)
            for c in cells
            if c.id in res.raws
        ]
    except SeriesGapError as e:
        raise HTTPException(502, f"séries Open-Meteo incomplètes : {e}") from e
    return results, res.unavailable, res.retry_after_s


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
    species: Species,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    day: date | None = None,
) -> dict[str, Any]:
    """Par cellule de l'emprise (id, lat, lon, alt_classes, elevation_m) : `M_eff` et `phi` par classe
    d'altitude, sans détail des facteurs.

    `day` filtre sur un seul jour (défaut : les 11 jours J−3 → J+7). Les cellules dont la météo
    manque (Open-Meteo injoignable, rien en cache) sont listées dans `unavailable` au lieu de
    faire échouer la réponse ; `retry_after` (s) quand Open-Meteo l'a indiqué.
    """
    cells = grid.in_bbox(min_lon, min_lat, max_lon, max_lat)
    if len(cells) > MAX_BBOX_CELLS:
        raise HTTPException(
            422, f"{len(cells)} cellules dans l'emprise, maximum {MAX_BBOX_CELLS} : zoomez"
        )
    results, unavailable, retry_after = await _compute(client, cells, species, detail=False)
    out = []
    for r in results:
        sp = {}
        for sid, rows in r["species"].items():
            if day is not None:
                rows = [row for row in rows if row["date"] == day.isoformat()]
            sp[sid] = rows
        out.append({"cell": r["cell"], "stale": r["stale"], "species": sp})
    return {
        "today": client.today().isoformat(),
        "scoring_version": results[0]["scoring_version"] if results else None,
        "cells": out,
        "unavailable": sorted(unavailable),
        "retry_after": retry_after,
    }
