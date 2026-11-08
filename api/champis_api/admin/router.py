"""Routes `/admin` du backoffice (spec › Backoffice, lot BO1 : lecture seule).

- `GET /admin` et `/admin/static/<fichier>` : la page et ses fichiers, sans secret ;
- `POST /admin/login` (`{"key": …}`) pose le cookie, 5 essais par minute et par IP ;
  `POST /admin/logout` le retire ;
- `GET /admin/api/me`, `/health`, `/usage?days=90`, `/jobs?limit=50&kind=`, `/jobs/<id>` :
  ce que la page affiche, derrière le cookie ou `Authorization: Bearer <clé>`.

Tout répond 404 sans `CHAMPIS_ADMIN_KEY`. `X-Robots-Tag: noindex` partout.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import counts
from ..ratelimit import TokenBuckets, client_ip
from . import auth, health

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"admin.js": "application/javascript", "admin.css": "text/css"}
LOGIN_PER_MINUTE = 5
ROBOTS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}
MAX_USAGE_DAYS = 400


def _state(request: Request):
    return request.app.state.champis


def _store(request: Request):
    store = _state(request).store
    if store is None:
        raise HTTPException(503, "pas de base (DATABASE_URL)")
    return store


def _login_buckets(request: Request) -> TokenBuckets:
    buckets = getattr(request.app.state, "admin_login_buckets", None)
    if buckets is None:
        buckets = request.app.state.admin_login_buckets = TokenBuckets(LOGIN_PER_MINUTE)
    return buckets


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def page(_: auth.Enabled) -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers=ROBOTS)


@router.get("/static/{name}")
async def static(name: str, _: auth.Enabled) -> FileResponse:
    if name not in STATIC_FILES:
        raise HTTPException(404)
    return FileResponse(STATIC / name, media_type=STATIC_FILES[name], headers=ROBOTS)


class Login(BaseModel):
    key: str


@router.post("/login", status_code=204)
async def login(body: Login, request: Request, response: Response, key: auth.Enabled) -> Response:
    auth.same_origin(request)
    if request.headers.get(auth.ACTION_HEADER) != "1":
        raise HTTPException(403, f"en-tête {auth.ACTION_HEADER}: 1 requis")
    wait = _login_buckets(request).take(client_ip(dict(request.headers), request.client))
    if wait > 0:
        raise HTTPException(429, "trop d'essais", headers={"Retry-After": str(int(wait + 1))})
    if not auth.key_matches(key, body.key):
        log.warning("backoffice : clé refusée")
        raise HTTPException(401, "clé refusée")
    response = Response(status_code=204, headers=ROBOTS)
    response.set_cookie(
        auth.COOKIE,
        auth.make_token(key),
        max_age=auth.TTL_S,
        path="/admin",
        # derrière Railway, TLS est terminé en amont : le protocole d'origine est dans l'en-tête
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/logout", status_code=204)
async def logout(_: auth.Action) -> Response:
    response = Response(status_code=204, headers=ROBOTS)
    response.delete_cookie(auth.COOKIE, path="/admin")
    return response


@router.get("/api/me")
async def me(_: auth.Authenticated) -> JSONResponse:
    return JSONResponse({"ok": True}, headers=ROBOTS)


@router.get("/api/health")
async def api_health(request: Request, _: auth.Authenticated) -> JSONResponse:
    settings = request.app.state.settings
    started = getattr(request.app.state, "started_at", datetime.now(UTC))
    data = await asyncio.to_thread(health.gather, settings, _state(request), started)
    return JSONResponse(data, headers=ROBOTS)


@router.get("/api/usage")
async def api_usage(request: Request, _: auth.Authenticated, days: int = 90) -> JSONResponse:
    days = max(1, min(days, MAX_USAGE_DAYS))
    store = _store(request)
    end = counts.today()
    span = [end - timedelta(days=k) for k in range(days)]
    data = await asyncio.to_thread(store.counts_since, span[-1])
    devices = await asyncio.to_thread(_devices, store, end, span[-1], request.app.state.settings)
    by_name: dict[str, dict[str, int]] = {}
    for day, values in data.items():
        for name, value in values.items():
            by_name.setdefault(name, {})[day.isoformat()] = value
    return JSONResponse(
        {
            "days": [d.isoformat() for d in sorted(span)],
            "labels": counts.COUNTERS,
            "counters": by_name,
            "table": {
                "header": list(counts.header()),
                "rows": counts.rows(data, span[: min(days, 14)]),
            },
            "devices": devices,
            # part de chaque espèce et groupe (lot BO2) : app et site, 7 et 30 jours
            "layers": {
                "7d": counts.layer_shares(data, end, 7),
                "30d": counts.layer_shares(data, end, 30),
            },
        },
        headers=ROBOTS,
    )


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
    from .. import jobs

    return {
        k: {"label": v, "expected_every_h": jobs.EXPECTED_EVERY_H.get(k)}
        for k, v in jobs.KINDS.items()
    }
