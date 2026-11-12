"""Onglet Espèces du backoffice (spec › Backoffice › Espèces, lot BO5, tranché le 8 nov. 2026) :
`GET /admin/api/species`, l'état de chaque espèce à chaque étape et d'où le relancer.

L'état se déduit de l'empreinte du fichier espèce (`species_hash`) comparée à ce qui existe :
`a_jour` (le jeu France publié porte l'empreinte courante), `perimee` (fichier modifié depuis),
`jamais_publiee`, `en_cours` (une tâche de l'espèce tourne ou attend). Trois sources : le fichier
(référentiel), le manifeste du bucket (tuiles publiées, G du groupe, cartes HD) et l'inventaire du
volume publié par le worker avec son battement (`workers.inventory`). La grille météo porte une
empreinte unique pour toutes les espèces : `grid_current` est global.

Une espèce est **complète** (demande d'Adrien du 8 nov. 2026) quand son fichier porte le bloc
`about`, une photo choisie et sa planche `shared/illustrations/<id>.jpg` ; la fiche montre la
planche et le prompt qui la produit (`admin/illustrations.py`). Depuis le 9 nov. 2026, elle n'est
**servie** aux clients que si, de plus, ses tuiles T France sont publiées (`champis_api.catalog`) :
le tableau nomme ce quatrième manque comme les trois autres.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from champis_pipeline.check.report import REPORTS_PREFIX
from champis_pipeline.species import (
    all_species_hash,
    group_hash,
    list_species,
    load_species,
    species_groups,
    species_hash,
)
from champis_pipeline.storage import MANIFEST_KEY, BucketLike, parse_tile_name

from .. import catalog, counts, render
from ..scoring import phenology_window
from ..settings import Settings
from ..store import Store
from . import illustrations

USAGE_DAYS = 30
PHI_YEAR = 2026  # année non bissextile : 365 jours, les mêmes pour toutes les espèces
PHI_ALTITUDE_STEPS_M = (0, 600)  # courbes Φ à l'altitude de référence et 600 m plus haut


def grid_hash(settings: Settings) -> str | None:
    try:
        return json.loads(settings.weather_grid.read_text(encoding="utf-8")).get("species_hash")
    except (OSError, ValueError):
        return None


def file_view(sid: str) -> dict[str, Any]:
    """Le fichier espèce tel quel pour la fiche (demande d'Adrien du 8 nov. 2026 : lecture
    structurée du fichier), avec la fenêtre Φ jour par jour calculée par le scoring de l'API,
    à l'altitude de référence du décalage et 600 m plus haut : la fiche ne recalcule rien."""
    spec = load_species(sid)
    shift = spec.get("phenology", {}).get("altitude_shift") or {}
    ref = float(shift.get("reference_m", 0))
    first = date(PHI_YEAR, 1, 1)
    curves = [
        {
            "altitude_m": ref + step,
            "days": [
                round(phenology_window(spec, first + timedelta(days=i), ref + step), 3)
                for i in range(365)
            ],
        }
        for step in PHI_ALTITUDE_STEPS_M
    ]
    return {
        "id": sid,
        "hash": species_hash(sid),
        "file": spec,
        "phi": curves,
        "illustration": illustrations.illustration_info(sid),
    }


def completeness(
    spec: dict[str, Any], sid: str, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Même règle que les clients (`champis_api.catalog.completeness`) : une espèce incomplète
    existe pour le pipeline et le backoffice, pas pour le site ni l'app. Avec `manifest`, le
    critère des tuiles publiées s'ajoute aux trois du référentiel."""
    return catalog.completeness(sid, spec, manifest_=manifest)


def _busy_jobs(store: Store | None) -> list[dict[str, Any]]:
    if store is None:
        return []
    try:
        return store.jobs_with_status(("queued", "running"))
    except Exception:
        return []


def _concerns(job: dict[str, Any], sid: str, group_ids: list[str]) -> bool:
    """La tâche touche l'espèce : option `species` qui la nomme (ou vide = toutes) sur les
    étapes du pipeline, `render` de l'espèce ou de toutes, `weather-grid` toujours."""
    args = job.get("args") or {}
    if job["kind"] in ("batch", "tiles", "publish", "check"):
        # `species` quand la tâche vient du backoffice, `args` (ligne de commande) depuis la CLI
        raw = args.get("species") or args.get("args", "")
        ids = raw.split() if isinstance(raw, str) else []
        named = [t for t in ids if not t.startswith("-")]
        return not named or sid in named or any(g in named for g in group_ids)
    if job["kind"] == "render":
        return args.get("species") in (sid, "all", None)
    return job["kind"] == "weather-grid"


def list_reports(bucket: BucketLike) -> list[dict[str, Any]]:
    """Les rapports de contrôle GBIF du bucket (`reports/<espèce>-<dept>[-<variante>].html`, lot
    du 12 nov. 2026), avec les chiffres de leur `.json` : AUC, et pour une variante la référence
    et le verdict du critère d'entrée. Les plus récents en dernier (ordre des noms)."""
    present = bucket.list(REPORTS_PREFIX)
    out = []
    for key, size in sorted(present.items()):
        name = key[len(REPORTS_PREFIX) :]
        if not name.endswith(".html"):
            continue
        data = bucket.get_json(REPORTS_PREFIX + name[: -len(".html")] + ".json") or {}
        species = data.get("species")
        dept = data.get("dept")
        if not species:  # rapport sans fiche JSON : on lit le nom `<espèce>-<dept>[-variante]`
            stem = name[: -len(".html")]
            for sid in list_species():
                if stem.startswith(sid + "-"):
                    species, dept = sid, stem[len(sid) + 1 :].split("-")[0]
                    break
        if not species:
            continue
        out.append(
            {
                "name": name,
                "key": key,
                "bytes": size,
                "species": species,
                "dept": dept,
                "variant": data.get("variant"),
                "auc": data.get("auc"),
                "n_in_forest": data.get("n_in_forest"),
                "t_hash": data.get("t_hash"),
                "baseline_auc": (data.get("baseline") or {}).get("auc"),
                "entry": data.get("entry"),
            }
        )
    return out


def gather(
    settings: Settings,
    bucket: BucketLike | None,
    store: Store | None,
    workers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    renders: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    if bucket is not None:
        try:
            manifest = bucket.get_json(MANIFEST_KEY) or {}
            renders = render.list_renders(bucket)
            reports = list_reports(bucket)
        except Exception:
            manifest = {}
    depts_by_hash = manifest.get("depts_by_hash", {})
    tiles = manifest.get("tiles", [])
    groups = species_groups()
    current_group_hash = {gid: group_hash(ids) for gid, ids in groups.items()}
    inv = None
    if workers:
        fresh = [w for w in workers if w.get("inventory")]
        if fresh:
            inv = max(fresh, key=lambda w: w["heartbeat_at"] or "")["inventory"]
    busy = _busy_jobs(store)
    shares: dict[str, dict[str, int]] = {}
    if store is not None:
        try:
            today = counts.today()
            data = store.counts_since(today - timedelta(days=USAGE_DAYS))
            shares = counts.layer_shares(data, today, USAGE_DAYS)
        except Exception:
            shares = {}
    total_app = sum((shares.get("app") or {}).values())
    total_web = sum((shares.get("web") or {}).values())
    current_grid = grid_hash(settings)
    out = []
    for sid in list_species():
        spec = load_species(sid)
        h = species_hash(sid)
        gid = spec["group"]["id"]
        group_ids = groups.get(gid, [])
        france = render.france_entry(manifest, sid)
        dept_sets = sum(
            1
            for t in tiles
            if t.get("kind", "T") == "T" and t.get("species") == sid and t.get("dept")
        )
        g_entries = [
            t for t in tiles if t.get("kind") == "G" and t.get("group") == gid and not t.get("dept")
        ]
        g_current = any(t.get("hash") == current_group_hash.get(gid) for t in g_entries)
        worker_t = (inv or {}).get("t", {}).get(sid)
        # les tuiles se construisent pour tout le groupe (le G empile un canal par espèce) : une
        # espèce du groupe sans T sur chaque département fait échouer « Construire les tuiles »
        # à sa première étape (constaté le 10 nov. 2026, Grisets)
        inv_depts = (inv or {}).get("depts", 0)
        group_missing_t = [
            g
            for g in group_ids
            if ((inv or {}).get("t", {}).get(g) or {}).get("current", 0) < inv_depts
        ]
        local_tiles = [
            n
            for n in (inv or {}).get("tiles", [])
            if (parse_tile_name(n) or {}).get("species") == sid
        ]
        local_current = [n for n in local_tiles if (parse_tile_name(n) or {}).get("hash") == h]
        # chaque carte présente s'ouvre depuis la fiche (URL signée d'une heure, comme l'onglet
        # Cartes le faisait avant d'être absorbé) ; la page redemande une URL fraîche au clic
        maps = [
            {
                **r,
                "current": r["hash"] == h,
                "url": bucket.presign(r["key"], 3600) if bucket else None,
            }
            for r in renders
            if r["species"] == sid
        ]
        checks = [
            {**r, "url": bucket.presign(r["key"], 3600) if bucket else None}
            for r in reports
            if r["species"] == sid
        ]
        jobs_here = [
            {
                "id": j["id"],
                "kind": j["kind"],
                "status": j["status"],
                "step_name": j.get("step_name"),
            }
            for j in busy
            if _concerns(j, sid, [gid])
        ]
        if jobs_here:
            state = "en_cours"
        elif france is None:
            state = "jamais_publiee"
        elif france.get("hash") == h:
            state = "a_jour"
        else:
            state = "perimee"
        app_share = (shares.get("app") or {}).get(sid, 0) + (shares.get("app") or {}).get(gid, 0)
        web_share = (shares.get("web") or {}).get(sid, 0) + (shares.get("web") or {}).get(gid, 0)
        out.append(
            {
                "id": sid,
                "name": spec.get("names", {}).get("fr", sid),
                "latin": spec.get("names", {}).get("la", ""),
                "group": {"id": gid, "name": spec["group"].get("fr", gid), "species": group_ids},
                "hash": h,
                "schema_version": spec.get("schema_version"),
                "about": bool(spec.get("about")),
                "completeness": completeness(spec, sid, manifest or None),
                "state": state,
                "published": {
                    "hash": france.get("hash") if france else None,
                    "depts": len(depts_by_hash.get(france.get("depts_hash", ""), []))
                    if france
                    else 0,
                    "bytes": int(france.get("bytes", 0)) if france else 0,
                    "generated": manifest.get("generated") if france else None,
                    "dept_sets": dept_sets,
                    "group_current": g_current,
                    "group_hash": current_group_hash.get(gid),
                }
                if manifest
                else None,
                "worker": {
                    "t_current": worker_t["current"] if worker_t else 0,
                    "t_stale": worker_t["stale"] if worker_t else 0,
                    "depts": inv_depts,
                    "group_missing_t": group_missing_t,
                    "tiles_current": len(local_current),
                    "tiles_stale": len(local_tiles) - len(local_current),
                    "batch_last": ((inv or {}).get("batch") or {}).get("last"),
                    "at": (inv or {}).get("at"),
                }
                if inv
                else None,
                "maps": maps,
                "checks": checks,
                "usage": {
                    "app": app_share,
                    "app_share": round(100 * app_share / total_app) if total_app else None,
                    "web": web_share,
                    "web_share": round(100 * web_share / total_web) if total_web else None,
                },
                "jobs": jobs_here,
            }
        )
    return {
        "species": out,
        "grid": {
            "hash": current_grid,
            "current_hash": all_species_hash(),
            "current": current_grid == all_species_hash(),
        },
        "manifest_generated": manifest.get("generated"),
        "inventory_at": (inv or {}).get("at"),
        "usage_days": USAGE_DAYS,
    }
