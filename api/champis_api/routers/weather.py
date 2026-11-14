"""`GET /weather` (une cellule, détail des facteurs), `GET /weather/bbox` (peindre une emprise
d'écran) et `GET /weather/field` (champ compact `M_eff × Φ` d'une grande emprise, un seul jour)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from champis_pipeline import SCORING_VERSION
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .. import offload
from ..cache import StoredCellWeather, WeatherCache
from ..field_cache import FieldCache, FieldEntry
from ..grid import Cell, WeatherGrid
from ..snapshot import META_CACHE_PUBLISHED
from ..weather_client import CellRaw, OpenMeteoClient
from ..weather_service import (
    DAYS_AFTER,
    DAYS_BEFORE,
    HistoryTooShortError,
    SeriesGapError,
    cell_weather,
)
from .query import Species

router = APIRouter(prefix="/weather", tags=["weather"])

# Hors de la boucle d'événements (correctif du 13 nov. 2026) : le calcul des scores, la lecture
# du précalcul et l'encodage JSON des grandes réponses sont synchrones et se comptent en dixièmes
# de seconde, voire en secondes ; faits dans la boucle, ils gelaient toutes les autres requêtes
# du worker. Chaque route les confie à un fil (`offload.run`) et ne garde dans la boucle
# que les appels Open-Meteo. La compression est déjà faite dans un fil par le middleware gzip.
# Accès concurrents au cache SQLite : voir `WeatherCache.__init__`.

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

# Coordonnées des requêtes : des nombres finis et bornés, sinon 422 (correctif du 13 nov. 2026 :
# `lat=inf` remontait en 500, « math domain error » dans `WeatherGrid.nearest`). Un point est sur
# le globe ; une emprise ne sert qu'à des comparaisons et vient telle quelle de la carte, qui,
# dézoomée, dépasse ±180° de longitude (copies du monde) ou ±90° de latitude (région MapKit plus
# sa marge) : bornes larges, pour ne refuser que ce qui n'est pas une emprise.
Lat = Annotated[float, Query(ge=-90.0, le=90.0, allow_inf_nan=False)]
Lon = Annotated[float, Query(ge=-180.0, le=180.0, allow_inf_nan=False)]
BoxCoord = Annotated[float, Query(ge=-720.0, le=720.0, allow_inf_nan=False)]


def _grid(request: Request) -> WeatherGrid:
    return request.app.state.champis.grid


def _client(request: Request) -> OpenMeteoClient:
    return request.app.state.champis.client


def _cache(request: Request) -> WeatherCache:
    return request.app.state.champis.cache


def _field_cache(request: Request) -> FieldCache | None:
    return getattr(request.app.state.champis, "field_cache", None)


def _json(payload: Any) -> bytes:
    """Le JSON compact de `JSONResponse`, sans passer par la sérialisation générique de FastAPI
    (validation puis encodage récursif), qui coûte plus que la lecture sur une grande réponse."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _json_response(body: bytes) -> Response:
    return Response(body, media_type="application/json")


Grid = Annotated[WeatherGrid, Depends(_grid)]
Client = Annotated[OpenMeteoClient, Depends(_client)]
Cache = Annotated[WeatherCache, Depends(_cache)]
Fields = Annotated[FieldCache | None, Depends(_field_cache)]


def _score(
    cells: list[Cell], raws: dict[str, CellRaw], species: list[str], today: date, detail: bool
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Dans un fil : les scores des cellules qui ont leur météo (≈ 14 ms par cellule et par
    espèce) ; (résultats, cellules dont l'historique est encore trop court → raison)."""
    results, short = [], {}
    for c in cells:
        if c.id not in raws:
            continue
        try:
            results.append(cell_weather(c, raws[c.id], species, today, detail=detail))
        except HistoryTooShortError as e:  # transitoire : l'accumulation comble un jour par jour
            short[c.id] = str(e)
        except SeriesGapError as e:
            raise HTTPException(502, f"séries Open-Meteo incomplètes : {e}") from e
    return results, short


async def _compute(
    client: OpenMeteoClient, cells: list[Cell], species: list[str], detail: bool
) -> tuple[list[dict[str, Any]], dict[str, str], int | None]:
    """(résultats des cellules disponibles, indisponibles → raison, Retry-After éventuel)."""
    res = await client.fetch_cells_partial(cells)
    results, short = await offload.run(_score, cells, res.raws, species, client.today(), detail)
    return results, dict(res.unavailable) | short, res.retry_after_s


def _retry_headers(retry_after: int | None) -> dict[str, str] | None:
    return {"Retry-After": str(retry_after)} if retry_after else None


@router.get("")
async def get_weather(
    grid: Grid,
    client: Client,
    species: Species,
    cell: str | None = None,
    lat: Lat | None = None,
    lon: Lon | None = None,
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


BBox = tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


@dataclass
class _BBoxStored:
    """Ce que `/weather/bbox` tire du précalcul, avant tout calcul à la demande."""

    cells: list[Cell]
    stored: dict[tuple[str, str], StoredCellWeather]
    ready: set[str]  # cellules dont toutes les espèces demandées sont précalculées
    computed_at: str | None
    to_compute: list[Cell]
    deferred: list[Cell]


def _bbox_stored(
    grid: WeatherGrid, cache: WeatherCache, species: list[str], box: BBox
) -> _BBoxStored:
    """Dans un fil : cellules de l'emprise et leurs lignes précalculées (`cell_weather`)."""
    cells = grid.in_bbox(*box)
    if len(cells) > MAX_BBOX_CELLS:
        raise HTTPException(
            422, f"{len(cells)} cellules dans l'emprise, maximum {MAX_BBOX_CELLS} : zoomez"
        )
    stored = cache.cell_weather((c.id for c in cells), species)
    ready = {c.id for c in cells if all((c.id, sid) in stored for sid in species)}
    missing = [c for c in cells if c.id not in ready]
    computed_at = max(
        (stored[cid, sid].computed_at for cid in ready for sid in species), default=None
    )
    return _BBoxStored(
        cells, stored, ready, computed_at, missing[:MAX_BBOX_COMPUTE], missing[MAX_BBOX_COMPUTE:]
    )


def _bbox_body(
    cache: WeatherCache,
    pre: _BBoxStored,
    species: list[str],
    computed: list[dict[str, Any]],
    failed: dict[str, str],
    retry_after: int | None,
    now: datetime,
    today: date,
    day: date | None,
) -> bytes:
    """Dans un fil : range ce qui vient d'être calculé à la demande, puis encode la réponse.

    Cellule par cellule, puis collée. Un seul `json.dumps` de la réponse entière (30 Mo sur
    5 000 cellules) garde le verrou de l'interpréteur ≈ 0,7 s d'un bloc ; et décoder d'avance
    les lignes de toute l'emprise fait tourner le ramasse-miettes sur plus d'un million d'objets,
    ≈ 0,25 s de gel à chaque passe. Les deux gèlent la boucle d'événements depuis leur fil : les
    lignes d'une cellule ne sont donc décodées qu'au moment de l'encoder, puis lâchées.
    """
    computed_at = pre.computed_at
    unavailable: dict[str, str] = {c.id: UNAVAILABLE_NOT_PRECOMPUTED for c in pre.deferred}
    unavailable |= failed
    on_demand = {r["cell"]["id"]: r for r in computed}
    if computed:
        cache.put_cell_weather(
            (
                (r["cell"]["id"], sid, r["stale"], rows)
                for r in computed
                for sid, rows in r["species"].items()
            ),
            today,
            now,
        )
        if computed_at is None:
            computed_at = now.isoformat(timespec="seconds")

    only = day.isoformat() if day is not None else None
    fragments: list[bytes] = []
    for c in pre.cells:
        if c.id in pre.ready:
            entries = [pre.stored[c.id, sid] for sid in species]
            cell, stale = c.as_dict(), any(e.stale for e in entries)
            by_species = {sid: e.rows for sid, e in zip(species, entries, strict=True)}
        elif c.id in on_demand:
            r = on_demand[c.id]
            cell, stale, by_species = r["cell"], r["stale"], r["species"]
        else:
            continue
        if only is not None:
            by_species = {
                sid: [row for row in rows if row["date"] == only]
                for sid, rows in by_species.items()
            }
        fragments.append(_json({"cell": cell, "stale": stale, "species": by_species}))
    head = _json(
        {
            "today": today.isoformat(),
            "computed_at": computed_at,
            "scoring_version": SCORING_VERSION if fragments else None,
        }
    )
    tail = _json({"unavailable": sorted(unavailable), "retry_after": retry_after})
    return b"".join((head[:-1], b',"cells":[', b",".join(fragments), b"],", tail[1:]))


@router.get("/bbox")
async def get_weather_bbox(
    grid: Grid,
    client: Client,
    cache: Cache,
    species: Species,
    min_lon: BoxCoord,
    min_lat: BoxCoord,
    max_lon: BoxCoord,
    max_lat: BoxCoord,
    day: date | None = None,
) -> Response:
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
    box = (min_lon, min_lat, max_lon, max_lat)
    pre = await offload.run(_bbox_stored, grid, cache, species, box)
    computed: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    retry_after = None
    if pre.to_compute:
        computed, failed, retry_after = await _compute(
            client, pre.to_compute, species, detail=False
        )
    body = await offload.run(
        _bbox_body,
        cache,
        pre,
        species,
        computed,
        failed,
        retry_after,
        client.now(),
        client.today(),
        day,
    )
    return _json_response(body)


def _field_items(
    cells: list[Cell], cache: WeatherCache, species: list[str], day: date
) -> dict[str, tuple[bytes, str]]:
    """Par cellule servie : (son fragment JSON de la réponse, `computed_at` de son précalcul)."""
    stored = cache.cell_field((c.id for c in cells), species, day)
    items: dict[str, tuple[bytes, str]] = {}
    for c in cells:
        e = stored.get(c.id)
        if e is None:
            continue
        item: dict[str, Any] = c.as_dict()
        if e.stale:
            item["stale"] = True
        item["f"] = e.f
        items[c.id] = (_json(item), e.computed_at)
    return items


def _field_body(
    grid: WeatherGrid,
    cache: WeatherCache,
    fields: FieldCache | None,
    species: list[str],
    day: date,
    today: date,
    box: BBox,
) -> bytes:
    """Dans un fil : le corps JSON de `/weather/field`, depuis le cache en mémoire du champ
    (`field_cache`) quand il a l'entrée ou doit la construire, sinon par une lecture de l'emprise.

    La version du cache suit le précalcul : `cell_field` n'est écrite que par la pré-chauffe
    (instantané importé, `cache_published` ; ou passe intégrée, `dept_index_computed_at`) et par
    les calculs à la demande de ce worker (`field_generation`). Une cellule calculée à la demande
    par un **autre** worker n'apparaît donc dans une entrée déjà construite qu'au prochain
    précalcul, comme toute cellule que la pré-chauffe n'avait pas.
    """
    cells = grid.in_bbox(*box)
    entry = None
    if fields is not None:
        version = (
            cache.field_generation,
            cache.meta_get(META_CACHE_PUBLISHED),
            cache.meta_get(cache.META_DEPT_INDEX_AT),
        )
        entry = fields.get(
            version,
            (tuple(sorted(species)), day),
            lambda: FieldEntry.of(_field_items(list(grid.cells.values()), cache, species, day)),
            eager=2 * len(cells) >= len(grid),
        )
    items = entry.items if entry is not None else _field_items(cells, cache, species, day)

    fragments: list[bytes] = []
    computed_at: str | None = None
    unavailable = 0
    for c in cells:
        found = items.get(c.id)
        if found is None:
            unavailable += 1
            continue
        fragments.append(found[0])
        if computed_at is None or found[1] > computed_at:
            computed_at = found[1]
    head = _json(
        {
            "today": today.isoformat(),
            "computed_at": computed_at,
            "day": day.isoformat(),
            "scoring_version": SCORING_VERSION,
        }
    )
    return b"".join(
        (
            head[:-1],
            b',"cells":[',
            b",".join(fragments),
            b'],"unavailable":',
            str(unavailable).encode(),
            b"}",
        )
    )


@router.get("/field")
async def get_weather_field(
    grid: Grid,
    client: Client,
    cache: Cache,
    fields: Fields,
    species: Species,
    day: date,
    min_lon: BoxCoord,
    min_lat: BoxCoord,
    max_lon: BoxCoord,
    max_lat: BoxCoord,
) -> Response:
    """Champ météo compact d'une grande emprise, pour la carte sous le zoom 9 (spec › Points
    ouverts › Carte visible dès l'ouverture, 4 nov. 2026) : par cellule (`id`, `lat`, `lon`,
    `elevation_m`, `alt_classes`), `f[espèce][classe] = M_eff × phi` arrondi à trois décimales,
    pour le seul jour `day`, obligatoire, dans la fenêtre J−3 → J+7 de `today` (sinon 422).
    Les classes sont celles de `by_alt` de `/weather/bbox`.

    Servi **uniquement** depuis `cell_field`, la table compacte par jour que la pré-chauffe
    dérive de `cell_weather` dans la même transaction (spec › Points ouverts › Stockage compact
    par jour pour /weather/field, 5 nov. 2026), en une seule requête SQL : aucun calcul à la
    demande, aucun appel Open-Meteo, aucun plafond de cellules (la grille entière tient dans une
    réponse, compressée par le middleware). Une cellule de l'emprise que le précalcul n'a pas pour
    toutes les espèces demandées, ou sans ligne pour `day` (J+7 entre minuit et la pré-chauffe),
    est omise et comptée dans `unavailable`. `stale` (`true`) n'apparaît que sur une cellule dont
    le précalcul est périmé. `computed_at` est le plus récent des précalculs servis, `null` sans
    cellule.

    Lecture, assemblage et encodage se font dans un fil. Le champ d'un jeu d'espèces et d'un jour
    est gardé en mémoire tant que le précalcul n'a pas changé (`field_cache`, 13 nov. 2026) :
    une emprise qui couvre la moitié de la grille, ou une deuxième demande, ne relit plus rien.
    """
    today = client.today()
    first, last = today - timedelta(days=DAYS_BEFORE), today + timedelta(days=DAYS_AFTER)
    if not first <= day <= last:
        raise HTTPException(
            422,
            f"day={day.isoformat()} hors de la fenêtre J−{DAYS_BEFORE} → J+{DAYS_AFTER} "
            f"({first.isoformat()} → {last.isoformat()})",
        )
    box = (min_lon, min_lat, max_lon, max_lat)
    body = await offload.run(_field_body, grid, cache, fields, species, day, today, box)
    return _json_response(body)
