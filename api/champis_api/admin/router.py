"""API JSON `/admin/api` du backoffice (spec › Backoffice, lot BO1 : lecture seule).

Les pages elles-mêmes (HTML rendu par le serveur, lot BO6), la connexion et les fichiers statiques
sont dans `pages.py` ; cette API reste celle des scripts (`Authorization: Bearer <clé>`) :

- `GET /admin/api/me`, `/health`, `/usage?days=90`, `/jobs?limit=50&kind=`, `/jobs/<id>` :
  ce que la page affiche, derrière le cookie ou `Authorization: Bearer <clé>` ;
- lot BO3 : `GET /admin/api/tools` (catalogue, état du service d'exécution, file), `POST
  /admin/api/jobs` (`{"kind", "args"}` → tâche `queued`, que `champis-jobs` prend), `POST
  /admin/api/jobs/<id>/cancel`, `GET /admin/api/renders` (cartes HD présentes dans le bucket, URL
  présignées d'une heure), `POST /admin/api/renders/delete` (`{"key"}` : retire une carte HD du
  bucket) ; lot BO4 : `GET /admin/api/artifacts` (sorties du pipeline à committer) ;
  lot BO5 : `GET /admin/api/species` (état de chaque espèce à chaque étape), `GET
  /admin/api/species/<id>` (fichier structuré, Φ, planche et son prompt), `GET
  /admin/api/species/<id>/illustration` (la planche, requise pour une espèce complète).

Tout répond 404 sans `CHAMPIS_ADMIN_KEY`. `X-Robots-Tag: noindex` partout.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import counts, jobs, render, tools
from . import auth, health

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)
ROBOTS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}
MAX_USAGE_DAYS = 400


def _state(request: Request):
    return request.app.state.champis


def _store(request: Request):
    store = _state(request).store
    if store is None:
        raise HTTPException(503, "pas de base (DATABASE_URL)")
    return store


@router.get("/api/me")
async def me(_: auth.Authenticated) -> JSONResponse:
    return JSONResponse({"ok": True}, headers=ROBOTS)


@router.get("/api/health")
async def api_health(request: Request, _: auth.Authenticated) -> JSONResponse:
    settings = request.app.state.settings
    started = getattr(request.app.state, "started_at", datetime.now(UTC))
    data = await asyncio.to_thread(health.gather, settings, _state(request), started)
    return JSONResponse(data, headers=ROBOTS)


def usage(store, settings, days: int) -> dict[str, Any]:
    """Ce que la page Usage affiche (synchrone : à lancer dans un fil)."""
    days = max(1, min(days, MAX_USAGE_DAYS))
    end = counts.today()
    span = [end - timedelta(days=k) for k in range(days)]
    data = store.counts_since(span[-1])
    by_name: dict[str, dict[str, int]] = {}
    for day, values in data.items():
        for name, value in values.items():
            by_name.setdefault(name, {})[day.isoformat()] = value
    return {
        "days": [d.isoformat() for d in sorted(span)],
        "labels": counts.COUNTERS,
        "counters": by_name,
        "table": {
            "header": list(counts.header()),
            "rows": counts.rows(data, span[: min(days, 14)]),
        },
        "devices": _devices(store, end, span[-1], settings),
        # part de chaque espèce et groupe (lot BO2) : app et site, 7 et 30 jours
        "layers": {
            "7d": counts.layer_shares(data, end, 7),
            "30d": counts.layer_shares(data, end, 30),
        },
    }


@router.get("/api/usage")
async def api_usage(request: Request, _: auth.Authenticated, days: int = 90) -> JSONResponse:
    data = await asyncio.to_thread(usage, _store(request), request.app.state.settings, days)
    return JSONResponse(data, headers=ROBOTS)


def _devices(store, end, since, settings) -> dict[str, Any]:
    """Appareils de l'app (lot BO2) : actifs du jour, de la semaine, du mois, uniques sur 13 mois,
    la série par jour ; `enabled` dit si le secret est posé (sinon rien n'est compté)."""
    with_coins, _ = store.coin_totals()
    daily = store.daily_active_devices(since)
    return {
        "enabled": bool(settings.measure_secret),
        "today": store.active_devices(end, 1),
        "yesterday": store.active_devices(end - timedelta(days=1), 1),
        "week": store.active_devices(end, 7),
        "month": store.active_devices(end, 30),
        "unique": store.active_devices(end, counts.KEEP_DAYS),
        "with_coins": with_coins,
        "daily": {d.isoformat(): n for d, n in sorted(daily.items())},
    }


@router.get("/api/jobs")
async def api_jobs(
    request: Request, _: auth.Authenticated, limit: int = 50, kind: str | None = None
) -> JSONResponse:
    store = _store(request)
    rows = await asyncio.to_thread(store.list_jobs, max(1, min(limit, 500)), kind or None)
    return JSONResponse({"jobs": rows, "kinds": _kinds()}, headers=ROBOTS)


@router.get("/api/jobs/{job_id}")
async def api_job(job_id: str, request: Request, _: auth.Authenticated) -> JSONResponse:
    store = _store(request)
    job = await asyncio.to_thread(store.job, job_id)
    if job is None:
        raise HTTPException(404, "tâche inconnue")
    events = await asyncio.to_thread(store.events_of, job_id)
    log_url = None
    bucket = _state(request).bucket
    if job.get("log_key") and bucket is not None:
        try:
            log_url = bucket.presign(job["log_key"], 3600)
        except Exception:  # le journal reste lisible dans les événements
            log_url = None
    return JSONResponse({"job": job, "events": events, "log_url": log_url}, headers=ROBOTS)


def _kinds() -> dict[str, Any]:
    return {
        k: {"label": v, "expected_every_h": jobs.EXPECTED_EVERY_H.get(k)}
        for k, v in jobs.KINDS.items()
    }


# ------------------------------------------------------------------ lot BO3 : lancer, annuler, cartes


class RenderKey(BaseModel):
    key: str


class Launch(BaseModel):
    kind: str
    args: dict[str, Any] = {}


def _queue(store) -> list[dict[str, Any]]:
    return store.jobs_with_status(("queued", "running"))


@router.get("/api/tools")
async def api_tools(request: Request, _: auth.Authenticated) -> JSONResponse:
    store = _store(request)
    workers_, queue = await asyncio.to_thread(lambda: (store.list_workers(), _queue(store)))
    return JSONResponse(
        {"tools": tools.describe(), "workers": workers_, "queue": queue}, headers=ROBOTS
    )


@router.post("/api/jobs", status_code=201)
async def api_launch(body: Launch, request: Request, _: auth.Action) -> JSONResponse:
    store = _store(request)
    try:
        args = tools.validate(body.kind, body.args)
    except tools.ToolError as e:
        raise HTTPException(422, str(e)) from e
    tool = tools.TOOLS[body.kind]
    if tool.exclusive:
        busy = [j for j in await asyncio.to_thread(_queue, store) if j["kind"] == body.kind]
        if busy:
            raise HTTPException(
                409, f"{tool.label} : une tâche est déjà {busy[0]['status']} ({busy[0]['id']})"
            )
    job_id = await asyncio.to_thread(store.create_job, body.kind, args, "admin", datetime.now(UTC))
    log.info("backoffice : %s mis en file (%s)", body.kind, job_id)
    return JSONResponse(
        {"id": job_id, "kind": body.kind, "args": args}, status_code=201, headers=ROBOTS
    )


@router.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str, request: Request, _: auth.Action) -> JSONResponse:
    store = _store(request)
    state = await asyncio.to_thread(store.request_cancel, job_id, datetime.now(UTC))
    if state is None:
        raise HTTPException(409, "tâche inconnue ou déjà terminée")
    return JSONResponse({"id": job_id, "status": state}, headers=ROBOTS)


@router.get("/api/species")
async def api_species(request: Request, _: auth.Authenticated) -> JSONResponse:
    """Onglet Espèces (lot BO5) : l'état de chaque espèce à chaque étape, et d'où la relancer."""
    from . import species as species_view

    st = _state(request)
    settings = request.app.state.settings

    def gather():
        try:
            workers_ = st.store.list_workers() if st.store is not None else []
        except Exception:  # base injoignable : la fiche se passe de l'inventaire du volume
            log.exception("admin : service d'exécution illisible")
            workers_ = []
        return species_view.gather(settings, st.bucket, st.store, workers_)

    return JSONResponse(await asyncio.to_thread(gather), headers=ROBOTS)


@router.get("/api/species/{sid}")
async def api_species_file(sid: str, request: Request, _: auth.Authenticated) -> JSONResponse:
    """Le fichier d'une espèce, structuré dans la fiche, et sa fenêtre Φ calculée par l'API."""
    from champis_pipeline.species import list_species

    from . import species as species_view

    if sid not in list_species():
        raise HTTPException(404, "espèce inconnue")
    return JSONResponse(await asyncio.to_thread(species_view.file_view, sid), headers=ROBOTS)


@router.get("/api/species/{sid}/illustration")
async def api_species_illustration(sid: str, _: auth.Authenticated) -> FileResponse:
    """La planche `shared/illustrations/<id>.jpg` du référentiel (requise pour une espèce complète)."""
    from champis_pipeline.species import illustration_path, list_species

    path = illustration_path(sid) if sid in list_species() else None
    if path is None:
        raise HTTPException(404, "pas d'illustration pour cette espèce")
    return FileResponse(path, media_type="image/jpeg", headers=ROBOTS)


@router.get("/api/artifacts")
async def api_artifacts(request: Request, _: auth.Authenticated) -> JSONResponse:
    """Les sorties du pipeline à committer (lot BO4 : `weather_grid.json`), avec leur URL d'une heure."""
    from champis_pipeline import artifacts

    bucket = _state(request).bucket
    if bucket is None:
        raise HTTPException(503, "bucket non configuré")

    def gather():
        return [
            {**a, "url": bucket.presign(a["key"], 3600)} for a in artifacts.list_artifacts(bucket)
        ]

    return JSONResponse({"artifacts": await asyncio.to_thread(gather)}, headers=ROBOTS)


@router.get("/api/renders")
async def api_renders(request: Request, _: auth.Authenticated) -> JSONResponse:
    """Par espèce : nom, empreinte publiée, et les cartes présentes par niveau (URL d'une heure)."""
    from champis_pipeline.species import list_species, load_species
    from champis_pipeline.storage import MANIFEST_KEY

    bucket = _state(request).bucket
    if bucket is None:
        raise HTTPException(503, "bucket non configuré")

    def gather():
        manifest = bucket.get_json(MANIFEST_KEY) or {}
        present = render.list_renders(bucket)
        out = []
        for sid in list_species():
            names = load_species(sid).get("names", {})
            entry = render.france_entry(manifest, sid)
            files = [
                {
                    **info,
                    "current": bool(entry) and info["hash"] == entry["hash"],
                    "url": bucket.presign(info["key"], 3600),
                }
                for info in present
                if info["species"] == sid
            ]
            out.append(
                {
                    "species": sid,
                    "name": names.get("fr", sid),
                    "latin": names.get("la", ""),
                    "published": entry is not None,
                    "hash": entry["hash"] if entry else None,
                    "files": files,
                }
            )
        return {
            "generated": manifest.get("generated"),
            "zooms": list(tools.RENDER_ZOOMS),
            "species": out,
        }

    return JSONResponse(await asyncio.to_thread(gather), headers=ROBOTS)


@router.post("/api/renders/delete")
async def api_render_delete(body: RenderKey, request: Request, _: auth.Action) -> JSONResponse:
    """Retire une carte HD du bucket (sa fiche peut ensuite la régénérer). Seules les clés
    `renders/T-<espèce>-<hash>-z<niveau>.png` sont acceptées : rien d'autre ne s'efface ici."""
    bucket = _state(request).bucket
    if bucket is None:
        raise HTTPException(503, "bucket non configuré")
    if render.parse_render_key(body.key) is None:
        raise HTTPException(422, "clé de carte HD invalide")

    def remove() -> bool:
        if body.key not in bucket.list(body.key):
            return False
        bucket.delete([body.key])
        return True

    if not await asyncio.to_thread(remove):
        raise HTTPException(404, "carte absente du bucket")
    log.info("backoffice : carte HD supprimée (%s)", body.key)
    return JSONResponse({"key": body.key, "deleted": True}, headers=ROBOTS)
