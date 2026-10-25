"""Occurrences GBIF d'une espèce sur une emprise : API occurrence/search, cache disque.

Filtres (spec › Validation) : France, coordonnées présentes sans problème géospatial, datées, et
précision acceptable : incertitude déclarée ≤ `MAX_UNCERTAINTY_M`, ou incertitude non renseignée
mais coordonnées à ≥ `MIN_DECIMALS` décimales (≈ 10 m). Le filtre est appliqué **côté client** :
le paramètre `coordinateUncertaintyInMeters` de l'API exclut aussi les enregistrements sans
incertitude, c'est-à-dire la plupart des jeux naturalistes français (constaté sur le Doubs : 0
occurrence). Les points écartés (incertitude > 100 m ou coordonnées arrondies) sont comptés.
Jamais retéléchargé si le fichier de cache existe (`force`). Licence conservée par enregistrement.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import RAW_DIR

log = logging.getLogger(__name__)

API = "https://api.gbif.org/v1"
PAGE = 300
MAX_OFFSET = 100_000  # limite de l'API search ; au-delà il faudrait l'API download
MAX_UNCERTAINTY_M = 100
MIN_DECIMALS = 4  # 0,0001° ≈ 11 m en latitude, 8 m en longitude en France
GBIF_DIR = RAW_DIR / "gbif"
CACHE_VERSION = 3


@dataclass(frozen=True)
class Occurrence:
    lat: float
    lon: float
    date: str  # AAAA-MM-JJ (eventDate tronqué)
    uncertainty_m: float | None
    dataset: str
    license: str
    precision: str  # "declared" (≤ 100 m déclarés) ou "fine_coords" (inconnue, ≥ 4 décimales)


UNCERTAINTY_BUCKETS_M = (250, 500, 1000, 5000)  # bornes des classes d'incertitude écartée


@dataclass
class Rejected:
    """Compteurs des enregistrements écartés, pour le rapport (et pour choisir un seuil)."""

    undated: int = 0
    imprecise: int = 0  # incertitude déclarée > MAX_UNCERTAINTY_M
    rounded: int = 0  # incertitude inconnue et coordonnées à moins de MIN_DECIMALS décimales
    imprecise_by_m: dict[str, int] = field(default_factory=dict)  # "100-250", …, ">5000"


def _uncertainty_bucket(unc_m: float) -> str:
    lo = MAX_UNCERTAINTY_M
    for hi in UNCERTAINTY_BUCKETS_M:
        if unc_m <= hi:
            return f"{lo}-{hi}"
        lo = hi
    return f">{lo}"


def taxon_key(name_la: str, http: httpx.Client) -> int:
    """usageKey GBIF du nom latin (règne Fungi) ; erreur si aucune correspondance."""
    r = http.get(f"{API}/species/match", params={"name": name_la, "kingdom": "Fungi"})
    r.raise_for_status()
    body = r.json()
    if body.get("matchType") == "NONE" or "usageKey" not in body:
        raise ValueError(f"GBIF ne connaît pas « {name_la} » : {body.get('note')}")
    return int(body["usageKey"])


def occurrences(
    species_id: str,
    name_la: str,
    bbox_4326: tuple[float, float, float, float],
    http: httpx.Client,
    cache_dir: Path = GBIF_DIR,
    force: bool = False,
) -> tuple[list[Occurrence], Rejected]:
    """Occurrences retenues dans l'emprise (w, s, e, n) et compteurs d'écartés ; cache si présent."""
    w, s, e, n = bbox_4326
    cache = cache_dir / f"{species_id}_{w:.2f}_{s:.2f}_{e:.2f}_{n:.2f}.json"
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("version") == CACHE_VERSION:
            return [Occurrence(**r) for r in data["occurrences"]], Rejected(**data["rejected"])
        log.info("cache %s d'une version antérieure : retéléchargement", cache.name)

    key = taxon_key(name_la, http)
    params: dict[str, Any] = {
        "taxonKey": key,
        "country": "FR",
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
        "decimalLatitude": f"{s},{n}",
        "decimalLongitude": f"{w},{e}",
        "limit": PAGE,
    }
    out: list[Occurrence] = []
    rejected = Rejected()
    offset = 0
    while offset < MAX_OFFSET:
        r = http.get(f"{API}/occurrence/search", params={**params, "offset": offset})
        r.raise_for_status()
        body = r.json()
        for rec in body.get("results", []):
            o = _parse(rec, rejected)
            if o is not None:
                out.append(o)
        if body.get("endOfRecords", True):
            break
        offset += PAGE
    log.info(
        "GBIF %s : %d occurrences retenues (taxonKey %d) ; écartées : %d sans date, "
        "%d imprécises, %d arrondies",
        species_id,
        len(out),
        key,
        rejected.undated,
        rejected.imprecise,
        rejected.rounded,
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "species": species_id,
                "taxon_key": key,
                "bbox": list(bbox_4326),
                "occurrences": [asdict(o) for o in out],
                "rejected": asdict(rejected),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return out, rejected


def _decimals(v: Any) -> int:
    """Décimales significatives d'une coordonnée telle que GBIF la renvoie (47.25 → 2)."""
    s = repr(float(v))
    if "e" in s:
        return 0
    return len(s.split(".")[1].rstrip("0")) if "." in s else 0


def _parse(rec: dict[str, Any], rejected: Rejected) -> Occurrence | None:
    lat, lon = rec.get("decimalLatitude"), rec.get("decimalLongitude")
    if lat is None or lon is None:
        return None
    date = rec.get("eventDate")
    if not date:
        rejected.undated += 1
        return None
    unc = rec.get("coordinateUncertaintyInMeters")
    if unc is not None:
        if float(unc) > MAX_UNCERTAINTY_M:
            rejected.imprecise += 1
            b = _uncertainty_bucket(float(unc))
            rejected.imprecise_by_m[b] = rejected.imprecise_by_m.get(b, 0) + 1
            return None
        precision = "declared"
    elif min(_decimals(lat), _decimals(lon)) >= MIN_DECIMALS:
        precision = "fine_coords"
    else:
        rejected.rounded += 1
        return None
    return Occurrence(
        lat=float(lat),
        lon=float(lon),
        date=str(date)[:10],
        uncertainty_m=None if unc is None else float(unc),
        dataset=str(rec.get("datasetKey", "")),
        license=str(rec.get("license", "")),
        precision=precision,
    )
