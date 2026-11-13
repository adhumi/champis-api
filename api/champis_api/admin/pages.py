"""Pages du backoffice, rendues par le serveur (lot BO6, 13 nov. 2026 : `docs/backoffice-bo6.md`).

Chaque écran est une page HTML (Jinja2) avec sa propre adresse sous `/admin/` ; la navigation se
fait par de vrais liens et de vrais formulaires (`POST` puis redirection 303), l'état d'une liste
(recherche, filtres, tri, page) vit dans la query string. htmx n'apporte que des fragments : une
requête qui porte `HX-Request` et vise un fragment connu (`HX-Target`) reçoit ce seul fragment,
toute autre requête la page entière. Rien ne dépend de JavaScript.

- `GET /admin` → Santé ; `GET|POST /admin/login`, `POST /admin/logout` ;
- `/admin/health`, `/admin/usage?days=` ;
- `/admin/species?q=&state=&group=&complete=&sort=&dir=`, `/admin/species/<id>`,
  `/admin/species/<id>/file` ;
- `/admin/jobs?q=&kind=&status=&origin=&sort=&dir=&page=`, `/admin/jobs/<id>`,
  `POST /admin/jobs/<id>/cancel` ;
- `/admin/tools`, `GET|POST /admin/tools/<sorte>` (formulaire pré-rempli par la query string,
  erreurs par champ, confirmation servie par le serveur, puis redirection vers la tâche) ;
- `/admin/renders`, `/admin/reports`, `/admin/artifacts` et leurs `…/open?key=` (redirection vers
  une URL signée fraîche), `GET|POST /admin/renders/delete` ;
- `/admin/search?q=` ; `/admin/static/<fichier>` (Tabler et htmx vendorés, sans tiers).

L'API JSON `/admin/api/*` (`router.py`) ne change pas. Accès : `auth.py` (cookie signé ou Bearer,
jeton `csrf` dans chaque formulaire) ; une page sans session redirige vers la connexion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, unquote, urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import jobs, render, tools
from ..ratelimit import TokenBuckets, client_ip, scope_headers
from . import auth, health, views
from . import router as api

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

HERE = Path(__file__).parent
STATIC = HERE / "static"
VENDOR = {
    "tabler_css": "vendor/tabler-1.7.5.min.css",
    "tabler_js": "vendor/tabler-1.7.5.min.js",
    "htmx_js": "vendor/htmx-2.0.11.min.js",
}
STATIC_FILES = {
    "admin.css": "text/css",
    "admin.js": "application/javascript",
    VENDOR["tabler_css"]: "text/css",
    VENDOR["tabler_js"]: "application/javascript",
    VENDOR["htmx_js"]: "application/javascript",
}
ROBOTS = api.ROBOTS
LOGIN_PER_MINUTE = 5
FLASH_COOKIE = "champis_flash"
HOME = "/admin/health"
FORM_TYPE = "application/x-www-form-urlencoded"

NAV = (
    ("Santé", "/admin/health"),
    ("Usage", "/admin/usage"),
    ("Espèces", "/admin/species"),
    ("Tâches", "/admin/jobs"),
    ("Outils", "/admin/tools"),
    ("Cartes HD", "/admin/renders"),
    ("Rapports GBIF", "/admin/reports"),
    ("Sorties", "/admin/artifacts"),
)

env = Environment(
    loader=FileSystemLoader(HERE / "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters.update(
    n=views.fmt_n,
    num=views.fmt_num,
    size=views.fmt_size,
    when=views.fmt_when,
    clock=views.fmt_time,
    ago=views.fmt_ago,
    kind_label=views.kind_label,
    last_word=views.last_word,
    tojson_pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=2),
)
env.globals.update(
    v=views,
    nav=NAV,
    vendor=VENDOR,
    states=views.STATES,
    state_colors=views.STATE_COLORS,
    status_colors=views.STATUS_COLORS,
)


def _species_view():
    """`admin.species` importe le contrôle GBIF du pipeline (rasterio) : jamais au démarrage de
    l'API, seulement quand une page en a besoin (`test_render.py` le vérifie)."""
    from . import species

    return species


# ------------------------------------------------------------------ erreurs et session


class PageError(Exception):
    """Une page qui ne peut pas s'afficher : rendue en HTML avec son explication, pas en JSON."""

    def __init__(self, status: int, title: str, detail: str = "") -> None:
        super().__init__(title)
        self.status, self.title, self.detail = status, title, detail


class LoginRequiredError(Exception):
    """Pas de session : la page redirige vers la connexion, qui ramènera ici."""


def safe_next(value: str | None) -> str:
    """Une adresse de retour reste dans le backoffice : chemin `/admin…`, jamais un autre site."""
    value = value or ""
    inside = value == "/admin" or value.startswith(("/admin/", "/admin?"))
    if not inside or "//" in value or "\\" in value or any(c in value for c in "\r\n"):
        return HOME
    return value


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _login_buckets(request: Request) -> TokenBuckets:
    """Essais de connexion par adresse ; `TokenBuckets` oublie seul les seaux redevenus pleins et
    borne leur nombre (ils n'étaient jamais purgés avant le 13 nov. 2026)."""
    buckets = getattr(request.app.state, "admin_login_buckets", None)
    if buckets is None:
        buckets = request.app.state.admin_login_buckets = TokenBuckets(LOGIN_PER_MINUTE)
    return buckets


class Ctx:
    """La requête d'une page ouverte : état de l'application, jeton des formulaires, rendu."""

    def __init__(self, request: Request, key: str, logged: bool = True) -> None:
        self.request = request
        self.key = key
        self.logged = logged
        self.csrf = auth.csrf_token(key, request.cookies.get(auth.COOKIE))
        self.htmx = _is_htmx(request)
        self.target = request.headers.get("hx-target", "")
        self.settings = request.app.state.settings
        self.state = getattr(request.app.state, "champis", None)
        self.store = getattr(self.state, "store", None)
        self.bucket = getattr(self.state, "bucket", None)

    def need_store(self):
        if self.store is None:
            raise PageError(
                503,
                "Pas de base de données",
                "DATABASE_URL n'est pas configurée : le journal des tâches, les outils et la "
                "mesure d'usage ne sont pas disponibles.",
            )
        return self.store

    @property
    def here(self) -> str:
        """L'adresse courante, query string comprise (retour après une action)."""
        url = self.request.url
        return url.path + (f"?{url.query}" if url.query else "")

    def _queue(self) -> int:
        try:
            return len(self.store.jobs_with_status(views.ACTIVE)) if self.store else 0
        except Exception:  # base injoignable : la page s'affiche sans la pastille
            return 0

    async def render(
        self,
        template: str,
        status: int = 200,
        partials: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        **context: Any,
    ) -> Response:
        """La page entière, ou le seul fragment visé quand htmx le demande."""
        base = {
            "csrf": self.csrf,
            "path": self.request.url.path,
            "here": self.here,
            "logged": self.logged,
            "now": datetime.now(UTC),
            "search_q": "",
        }
        partial = (partials or {}).get(self.target) if self.htmx else None
        lst = context.get("lst")
        # un formulaire de filtres envoie aussi ses champs vides (`?q=&kind=`) : l'adresse gardée
        # est celle de la liste, sans eux
        untidy = isinstance(lst, views.Listing) and "" in self.request.query_params.values()
        if partial:
            html = env.get_template(partial).render(**{**base, **context})
            push = {"HX-Push-Url": lst.url()} if untidy else {}
            return HTMLResponse(html, status, headers={**ROBOTS, **push, **(headers or {})})
        if untidy and status == 200:
            return RedirectResponse(lst.url(), 303, headers=ROBOTS)
        flash = None
        raw = auth.unsign(self.key, self.request.cookies.get(FLASH_COOKIE))
        if raw:
            level, _, text = unquote(raw).partition("|")
            flash = {"level": level, "text": text}
        queue = await asyncio.to_thread(self._queue) if self.logged else 0
        html = env.get_template(template).render(
            **{**base, "flash": flash, "queue": queue, **context}
        )
        response = HTMLResponse(html, status, headers={**ROBOTS, **(headers or {})})
        if self.request.cookies.get(FLASH_COOKIE):
            response.delete_cookie(FLASH_COOKIE, path="/admin")
        return response

    def redirect(self, url: str, flash: tuple[str, str] | None = None) -> Response:
        """303 après un `POST` (le rechargement ne renvoie pas le formulaire) ; le message
        s'affiche une fois sur la page d'arrivée."""
        response = RedirectResponse(url, 303, headers=ROBOTS)
        if flash:
            response.set_cookie(
                FLASH_COOKIE,
                auth.sign(self.key, quote(f"{flash[0]}|{flash[1]}")),
                max_age=60,
                path="/admin",
                httponly=True,
                samesite="strict",
            )
        return response


def session(request: Request, key: auth.Enabled) -> Ctx:
    auth.same_origin(request)
    if not auth.logged_in(request, key):
        raise LoginRequiredError
    return Ctx(request, key)


Session = Annotated[Ctx, Depends(session)]


class Form:
    """Champs d'un formulaire `application/x-www-form-urlencoded` (bibliothèque standard)."""

    def __init__(self, data: dict[str, list[str]]) -> None:
        self.data = data

    def get(self, name: str, default: str = "") -> str:
        values = self.data.get(name)
        return values[-1] if values else default

    def getlist(self, name: str) -> list[str]:
        return self.data.get(name, [])


async def read_form(request: Request) -> Form:
    if not request.headers.get("content-type", "").startswith(FORM_TYPE):
        return Form({})
    body = (await request.body()).decode("utf-8", errors="replace")
    return Form(parse_qs(body, keep_blank_values=True))


async def posted(request: Request, ctx: Session) -> Form:
    """Le formulaire d'une action : session, et jeton `csrf` qui prouve qu'il vient d'une page."""
    form = await read_form(request)
    if not auth.csrf_valid(ctx.key, request.cookies.get(auth.COOKIE), form.get("csrf")):
        raise PageError(
            403,
            "Formulaire expiré",
            "Le jeton du formulaire ne correspond plus à la session. Reviens à la page "
            "précédente, recharge-la et recommence.",
        )
    return form


Posted = Annotated[Form, Depends(posted)]


async def _page_error(request: Request, exc: PageError) -> Response:
    key = request.app.state.settings.admin_key
    ctx = Ctx(request, key, logged=auth.logged_in(request, key))
    ctx.htmx = False  # une erreur remplace la page, pas un fragment
    return await ctx.render(
        "error.html", exc.status, title=exc.title, detail=exc.detail, status_code=exc.status
    )


async def _login_required(request: Request, exc: LoginRequiredError) -> Response:
    url = request.url
    here = url.path + (f"?{url.query}" if url.query else "")
    target = "/admin/login"
    if request.method == "GET" and safe_next(here) == here and here not in ("/admin", HOME):
        target += "?" + urlencode({"next": here})
    if _is_htmx(request):  # un fragment ne doit pas recevoir la page de connexion
        return Response(status_code=401, headers={**ROBOTS, "HX-Redirect": target})
    return RedirectResponse(target, 303, headers=ROBOTS)


def install(app: FastAPI) -> None:
    """Branche les pages et leurs deux erreurs propres sur l'application."""
    app.include_router(router)
    app.add_exception_handler(PageError, _page_error)
    app.add_exception_handler(LoginRequiredError, _login_required)


# ------------------------------------------------------------------ connexion, fichiers


@router.get("")
@router.get("/")
async def root(ctx: Session) -> Response:
    return RedirectResponse(HOME, 303, headers=ROBOTS)


@router.get("/static/{name:path}")
async def static(name: str, _: auth.Enabled) -> FileResponse:
    if name not in STATIC_FILES:
        raise HTTPException(404)
    # les fichiers vendorés portent leur version dans leur nom : ils ne changent jamais
    cache = "public, max-age=31536000, immutable" if name.startswith("vendor/") else "no-cache"
    return FileResponse(
        STATIC / name,
        media_type=STATIC_FILES[name],
        headers={"X-Robots-Tag": ROBOTS["X-Robots-Tag"], "Cache-Control": cache},
    )


@router.get("/login")
async def login_page(request: Request, key: auth.Enabled, next: str = "") -> Response:
    if auth.logged_in(request, key):
        return RedirectResponse(safe_next(next), 303, headers=ROBOTS)
    return await Ctx(request, key, logged=False).render(
        "login.html", next=safe_next(next), error=""
    )


def _session_cookie(request: Request, response: Response, key: str) -> None:
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


@router.post("/login")
async def login(request: Request, key: auth.Enabled) -> Response:
    """Deux formes : le formulaire de la page (303 vers `next`, erreur rendue dans la page) et le
    JSON des scripts (`{"key": …}` avec `X-Champis-Admin: 1`, 204), inchangé depuis le lot BO1."""
    auth.same_origin(request)
    as_json = request.headers.get("content-type", "").startswith("application/json")
    if as_json:
        if request.headers.get(auth.ACTION_HEADER) != "1":
            raise HTTPException(403, f"en-tête {auth.ACTION_HEADER}: 1 requis")
        try:
            given = json.loads(await request.body()).get("key")
        except (ValueError, AttributeError) as e:
            raise HTTPException(422, 'corps JSON attendu : {"key": …}') from e
        if not isinstance(given, str):
            raise HTTPException(422, "key : chaîne attendue")
        next_ = HOME
    else:
        form = await read_form(request)
        given, next_ = form.get("key"), safe_next(form.get("next"))
    ctx = Ctx(request, key, logged=False)
    wait = _login_buckets(request).take(client_ip(scope_headers(request.scope), request.client))
    if wait > 0:
        retry = {"Retry-After": str(int(wait + 1))}
        if as_json:
            raise HTTPException(429, "trop d'essais", headers=retry)
        error = "Trop d'essais : attends une minute avant de recommencer."
        return await ctx.render("login.html", 429, headers=retry, next=next_, error=error)
    if not auth.key_matches(key, given):
        log.warning("backoffice : clé refusée")
        if as_json:
            raise HTTPException(401, "clé refusée")
        return await ctx.render("login.html", 401, next=next_, error="Clé refusée.")
    response: Response
    if as_json:
        response = Response(status_code=204, headers=ROBOTS)
    else:
        response = RedirectResponse(next_, 303, headers=ROBOTS)
    _session_cookie(request, response, key)
    return response


@router.post("/logout")
async def logout(request: Request, key: auth.Enabled) -> Response:
    auth.same_origin(request)
    response: Response
    if request.headers.get(auth.ACTION_HEADER) == "1":  # les scripts, comme au lot BO1
        if not auth.logged_in(request, key):
            raise HTTPException(401, "clé requise")
        response = Response(status_code=204, headers=ROBOTS)
    else:
        form = await read_form(request)
        if not auth.csrf_valid(key, request.cookies.get(auth.COOKIE), form.get("csrf")):
            raise HTTPException(403, f"en-tête {auth.ACTION_HEADER}: 1 ou jeton csrf requis")
        response = RedirectResponse("/admin/login", 303, headers=ROBOTS)
    response.delete_cookie(auth.COOKIE, path="/admin")
    return response


# ------------------------------------------------------------------ santé, usage


@router.get("/health")
async def health_page(ctx: Session) -> Response:
    started = getattr(ctx.request.app.state, "started_at", datetime.now(UTC))
    data = await asyncio.to_thread(health.gather, ctx.settings, ctx.state, started)
    return await ctx.render(
        "health.html", partials={"health-live": "partials/_health.html"}, h=data
    )


USAGE_SPANS = (30, 90, 365)


@router.get("/usage")
async def usage_page(ctx: Session, days: str = "90") -> Response:
    span = views.to_int(days, 90, 1, api.MAX_USAGE_DAYS)
    u = await asyncio.to_thread(api.usage, ctx.need_store(), ctx.settings, span)
    names = list(u["labels"]) + sorted(
        n for n in u["counters"] if n not in u["labels"] and not n.startswith("layer_")
    )
    charts = []
    for name in names:
        values = u["counters"].get(name, {})
        charts.append(
            {
                "name": name,
                "label": u["labels"].get(name),
                "total": sum(values.get(d) or 0 for d in u["days"]),
                "last": values.get(u["days"][-1]),
                "spark": views.sparkline(u["days"], values),
            }
        )
    layers = [
        {
            "title": f"{label} · {title}",
            **views.share_bars((u["layers"].get(window) or {}).get(client) or {}),
        }
        for client, label in (("app", "App"), ("web", "Site"))
        for window, title in (("7d", "7 jours"), ("30d", "30 jours"))
    ]
    return await ctx.render(
        "usage.html",
        u=u,
        days=span,
        spans=USAGE_SPANS,
        charts=charts,
        layers=layers,
        devices_spark=views.sparkline(u["days"], u["devices"]["daily"]),
    )


# ------------------------------------------------------------------ espèces


def _gather_species(ctx: Ctx, only: str | None = None, with_reports: bool = True) -> dict:
    try:
        workers = ctx.store.list_workers() if ctx.store is not None else []
    except Exception:  # base injoignable : la page se passe de l'inventaire du volume
        log.exception("admin : service d'exécution illisible")
        workers = []
    return _species_view().gather(
        ctx.settings, ctx.bucket, ctx.store, workers, only=only, with_reports=with_reports
    )


def _usage_total(sp: dict[str, Any]) -> int:
    return (sp["usage"].get("app") or 0) + (sp["usage"].get("web") or 0)


SPECIES_SORTS = {
    "name": lambda sp: sp["name"].lower(),
    "group": lambda sp: (sp["group"]["name"].lower(), sp["name"].lower()),
    "state": lambda sp: (list(views.STATES).index(sp["state"]), sp["name"].lower()),
    "complete": lambda sp: (len(sp["completeness"]["missing"]), sp["name"].lower()),
    "usage": _usage_total,
}


@router.get("/species")
async def species_list(ctx: Session) -> Response:
    lst = views.listing(
        "/admin/species",
        ctx.request.query_params,
        ("q", "state", "group", "complete"),
        tuple(SPECIES_SORTS),
        "name",
    )
    d = await asyncio.to_thread(_gather_species, ctx, None, False)
    groups = sorted({(sp["group"]["id"], sp["group"]["name"]) for sp in d["species"]}, key=lambda g: g[1])  # fmt: skip
    f = lst.params
    rows = [
        sp
        for sp in d["species"]
        if (
            not f["q"]
            or views.matches(f["q"], sp["name"], sp["latin"], sp["id"], sp["group"]["name"])
        )
        and (not f["state"] or sp["state"] == f["state"])
        and (not f["group"] or sp["group"]["id"] == f["group"])
        and (not f["complete"] or sp["completeness"]["complete"] == (f["complete"] == "oui"))
    ]
    page = views.paginate(views.sort_items(rows, SPECIES_SORTS, lst), lst.page)
    lst.page = page.page
    return await ctx.render(
        "species.html",
        partials={"species-table": "partials/_species_table.html"},
        d=d,
        lst=lst,
        page=page,
        groups=groups,
        total=len(d["species"]),
    )


def _known_species(sid: str) -> None:
    from champis_pipeline.species import list_species

    if sid not in list_species():
        raise PageError(
            404, "Espèce inconnue", f"Aucun fichier species/{sid}.json dans le référentiel."
        )


@router.get("/species/{sid}")
async def species_page(sid: str, ctx: Session, zoom: str = "9", live: str = "") -> Response:
    _known_species(sid)
    d = await asyncio.to_thread(_gather_species, ctx, sid)
    sp = d["species"][0]
    if ctx.htmx and live and not sp["jobs"]:
        # la tâche suivie vient de finir : cartes et rapports ont pu changer, la page se recharge
        return Response(status_code=204, headers={**ROBOTS, "HX-Refresh": "true"})
    z = views.to_int(zoom, tools.RENDER_ZOOMS[0], tools.RENDER_ZOOMS[0], tools.RENDER_ZOOMS[-1])
    return await ctx.render(
        "species_detail.html",
        partials={"species-live": "partials/_species_steps.html"},
        d=d,
        sp=sp,
        zoom=z,
        zooms=tools.RENDER_ZOOMS,
        steps=views.species_steps(sp, d["grid"], z),
    )


@router.get("/species/{sid}/file")
async def species_file(sid: str, ctx: Session) -> Response:
    _known_species(sid)
    data = await asyncio.to_thread(_species_view().file_view, sid)
    return await ctx.render(
        "species_file.html", data=data, f=data["file"], s=views.file_sections(data)
    )


# ------------------------------------------------------------------ tâches


JOB_PARTIALS = {"jobs-table": "partials/_jobs_table.html"}


@router.get("/jobs")
async def jobs_list(ctx: Session) -> Response:
    store = ctx.need_store()
    lst = views.listing(
        "/admin/jobs",
        ctx.request.query_params,
        ("q", "kind", "status", "origin"),
        store.JOB_SORTS,
        "created_at",
        default_desc=True,
    )
    f = lst.params
    where = {
        "kind": f["kind"] or None,
        "status": f["status"] or None,
        "origin": f["origin"] or None,
        "search": f["q"] or None,
    }

    def gather():
        total = store.count_jobs(**where)
        number, pages = views.page_of(total, lst.page)
        rows = store.list_jobs(
            views.PER_PAGE,
            offset=(number - 1) * views.PER_PAGE,
            sort=lst.sort,
            descending=lst.desc,
            **where,
        )
        return views.Page(rows, number, pages, total, views.PER_PAGE)

    page = await asyncio.to_thread(gather)
    lst.page = page.page
    return await ctx.render(
        "jobs.html",
        partials=JOB_PARTIALS,
        lst=lst,
        page=page,
        kinds=jobs.KINDS,
        statuses=views.STATUSES,
        origins=views.ORIGINS,
        live=any(j["status"] in views.ACTIVE for j in page.items),
    )


async def _job_context(ctx: Ctx, job_id: str) -> dict[str, Any]:
    store = ctx.need_store()

    def gather():
        job = store.job(job_id)
        return job, store.events_of(job_id) if job else []

    job, events = await asyncio.to_thread(gather)
    if job is None:
        raise PageError(404, "Tâche inconnue", "Elle a pu être purgée (le journal garde 13 mois).")
    log_url = None
    if job.get("log_key") and ctx.bucket is not None:
        try:
            log_url = ctx.bucket.presign(job["log_key"], 3600)
        except Exception:  # le journal reste lisible dans les événements
            log_url = None
    return {"job": job, "events": events, "log_url": log_url}


@router.get("/jobs/{job_id}")
async def job_page(job_id: str, ctx: Session) -> Response:
    context = await _job_context(ctx, job_id)
    return await ctx.render("job.html", partials={"job-live": "partials/_job_live.html"}, **context)


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: str, ctx: Session, form: Posted) -> Response:
    store = ctx.need_store()
    state = await asyncio.to_thread(store.request_cancel, job_id, datetime.now(UTC))
    if ctx.htmx and ctx.target == "job-live":
        context = await _job_context(ctx, job_id)
        return await ctx.render(
            "job.html", partials={"job-live": "partials/_job_live.html"}, **context
        )
    if ctx.htmx and ctx.target == f"job-row-{job_id}":
        job = await asyncio.to_thread(store.job, job_id)
        if job is not None:
            return await ctx.render(
                "jobs.html", partials={ctx.target: "partials/_job_row.html"}, j=job
            )
    back = safe_next(form.get("next") or f"/admin/jobs/{job_id}")
    if state is None:
        return ctx.redirect(back, ("warning", "Tâche inconnue ou déjà terminée : rien à annuler."))
    text = (
        "Tâche annulée."
        if state == "cancelled"
        else "Annulation demandée : le worker arrête la tâche."
    )
    return ctx.redirect(back, ("success", text))


# ------------------------------------------------------------------ outils


def _tools_state(store) -> tuple[list[dict], list[dict]]:
    return store.list_workers(), store.jobs_with_status(views.ACTIVE)


@router.get("/tools")
async def tools_page(ctx: Session) -> Response:
    workers, queue = await asyncio.to_thread(_tools_state, ctx.need_store())
    return await ctx.render(
        "tools.html",
        partials={"tools-queue": "partials/_queue.html"},
        tools=tools.describe(),
        workers=workers,
        queue=queue,
    )


def _tool(kind: str) -> dict[str, Any]:
    for t in tools.describe():
        if t["kind"] == kind:
            return t
    raise PageError(404, "Outil inconnu", f"« {kind} » n'est pas dans le catalogue des outils.")


def _species_choices() -> list[dict[str, str]]:
    from champis_pipeline.species import list_species, load_species

    out = []
    for sid in list_species():
        spec = load_species(sid)
        out.append(
            {
                "id": sid,
                "name": spec.get("names", {}).get("fr", sid),
                "group": spec["group"].get("fr", spec["group"]["id"]),
            }
        )
    return sorted(out, key=lambda s: (s["group"], s["name"]))


def _tool_values(tool: dict[str, Any], given: dict[str, str]) -> dict[str, Any]:
    """Valeurs du formulaire : celles reçues (query string ou envoi refusé), sinon les défauts."""
    values: dict[str, Any] = {}
    for f in tool["fields"]:
        raw = given.get(f["name"])
        if f["kind"] == "bool":
            values[f["name"]] = (
                bool(f["default"])
                if raw is None
                else raw.strip().lower() in ("1", "true", "oui", "on", "yes")
            )
        elif f["kind"] == "species":
            values[f["name"]] = (
                (raw if raw is not None else f["default"] or "").replace(",", " ").split()
            )
        elif raw is not None:
            values[f["name"]] = raw
        else:
            values[f["name"]] = "" if f["default"] is None else f"{f['default']:g}" if isinstance(f["default"], float) else str(f["default"])  # fmt: skip
    return values


async def _tool_form(
    ctx: Ctx, tool: dict[str, Any], given: dict[str, str], errors: dict[str, str], status: int = 200,
    busy: dict[str, Any] | None = None,
) -> Response:  # fmt: skip
    workers, queue = await asyncio.to_thread(_tools_state, ctx.need_store())
    return await ctx.render(
        "tool.html",
        status,
        tool=tool,
        values=_tool_values(tool, given),
        errors=errors,
        form_error=errors.get(tools.FORM, ""),
        busy=busy or next((j for j in queue if j["kind"] == tool["kind"]), None),
        workers=workers,
        species_choices=_species_choices()
        if any(f["kind"] == "species" for f in tool["fields"])
        else [],
    )


@router.get("/tools/{kind}")
async def tool_page(kind: str, ctx: Session) -> Response:
    tool = _tool(kind)
    names = {f["name"] for f in tool["fields"]}
    given = {k: v for k, v in ctx.request.query_params.items() if k in names}
    return await _tool_form(ctx, tool, given, {})


@router.post("/tools/{kind}")
async def tool_launch(kind: str, ctx: Session, form: Posted) -> Response:
    """Lance un outil : erreurs rendues sous leur champ (422), outil exclusif déjà en file (409),
    confirmation servie par le serveur quand l'action écrase quelque chose, puis 303 vers la tâche."""
    tool = _tool(kind)
    store = ctx.need_store()
    given: dict[str, str] = {}
    for f in tool["fields"]:
        if f["kind"] == "species":
            given[f["name"]] = " ".join(form.getlist(f["name"]))
        elif f["kind"] == "bool":
            given[f["name"]] = form.get(f["name"], "false")
        else:
            given[f["name"]] = form.get(f["name"])
    args, errors = tools.validate_fields(kind, given)
    if errors:
        return await _tool_form(ctx, tool, given, errors, 422)
    if tool["exclusive"]:
        queue = await asyncio.to_thread(store.jobs_with_status, views.ACTIVE)
        busy = next((j for j in queue if j["kind"] == kind), None)
        if busy:
            message = f"{tool['label']} : une tâche est déjà {busy['status']}. Une seule à la fois."
            return await _tool_form(ctx, tool, given, {tools.FORM: message}, 409, busy)
    question = views.confirmation(kind, args)
    if question and form.get("confirmed") != "1":
        return await ctx.render(
            "confirm.html",
            title=f"{tool['label']} : confirmer",
            question=question,
            action=f"/admin/tools/{kind}",
            fields=[(k, v) for k, v in args.items() if v != ""],
            submit="Lancer",
            back=safe_next(form.get("back") or f"/admin/tools/{kind}"),
            details=[
                (f["label"], args[f["name"]]) for f in tool["fields"] if args[f["name"]] != ""
            ],
        )
    job_id = await asyncio.to_thread(store.create_job, kind, args, "admin", datetime.now(UTC))
    log.info("backoffice : %s mis en file (%s)", kind, job_id)
    return ctx.redirect(
        f"/admin/jobs/{job_id}", ("success", f"{tool['label']} : tâche mise en file.")
    )


# ------------------------------------------------------------------ cartes HD, rapports, sorties


def _presigned(ctx: Ctx, key: str) -> Response:
    """Redirige vers une URL signée fraîche (une heure) : un lien du backoffice n'expire pas."""
    try:
        url = ctx.bucket.presign(key, 3600)
    except Exception as e:
        raise PageError(502, "Bucket injoignable", type(e).__name__) from e
    return RedirectResponse(url, 302, headers=ROBOTS)


def _need_bucket(ctx: Ctx):
    if ctx.bucket is None:
        raise PageError(503, "Bucket non configuré", "Sans bucket, il n'y a ni carte, ni rapport, ni sortie à ouvrir.")  # fmt: skip
    return ctx.bucket


def _species_names() -> dict[str, str]:
    return {s["id"]: s["name"] for s in _species_choices()}


def _renders(bucket) -> list[dict[str, Any]]:
    from champis_pipeline.storage import MANIFEST_KEY

    manifest = bucket.get_json(MANIFEST_KEY) or {}
    names = _species_names()
    out = []
    for info in render.list_renders(bucket):
        entry = render.france_entry(manifest, info["species"])
        out.append(
            {
                **info,
                "name": names.get(info["species"], info["species"]),
                "current": bool(entry) and info["hash"] == entry["hash"],
            }
        )
    return out


RENDER_SORTS = {
    "species": lambda r: (r["name"].lower(), r["zoom"]),
    "zoom": lambda r: (r["zoom"], r["name"].lower()),
    "bytes": lambda r: r["bytes"],
}


async def _bucket_rows(ctx: Ctx, fn) -> tuple[list[dict[str, Any]], str]:
    """Les lignes lues dans le bucket et, s'il ne répond pas, pourquoi la liste est vide."""
    if ctx.bucket is None:
        return [], "Bucket non configuré : rien à lister ici."
    try:
        return await asyncio.to_thread(fn, ctx.bucket), ""
    except Exception as e:
        log.exception("admin : bucket illisible")
        return [], f"Bucket injoignable ({type(e).__name__}) : recharge la page dans un instant."


@router.get("/renders")
async def renders_page(ctx: Session) -> Response:
    lst = views.listing(
        "/admin/renders",
        ctx.request.query_params,
        ("q", "zoom", "set"),
        tuple(RENDER_SORTS),
        "species",
    )
    rows, problem = await _bucket_rows(ctx, _renders)
    f = lst.params
    kept = [
        r
        for r in rows
        if (not f["q"] or views.matches(f["q"], r["name"], r["species"], r["key"]))
        and (not f["zoom"] or str(r["zoom"]) == f["zoom"])
        and (not f["set"] or r["current"] == (f["set"] == "courant"))
    ]
    page = views.paginate(views.sort_items(kept, RENDER_SORTS, lst), lst.page)
    lst.page = page.page
    return await ctx.render(
        "renders.html",
        partials={"renders-table": "partials/_renders_table.html"},
        lst=lst,
        page=page,
        total=len(rows),
        problem=problem,
        zooms=tools.RENDER_ZOOMS,
    )


def _render_key(key: str) -> dict[str, Any]:
    info = render.parse_render_key(key)
    if info is None:
        raise PageError(422, "Clé de carte HD invalide", "Seules les clés renders/T-<espèce>-<hash>-z<niveau>.png sont acceptées.")  # fmt: skip
    return info


@router.get("/renders/open")
async def render_open(ctx: Session, key: str = "") -> Response:
    _render_key(key)
    _need_bucket(ctx)
    return _presigned(ctx, key)


@router.get("/renders/delete")
async def render_delete_page(ctx: Session, key: str = "", next: str = "") -> Response:
    info = _render_key(key)
    return await ctx.render(
        "confirm.html",
        title="Supprimer une carte HD",
        question=f"Supprimer {key} du bucket ? La fiche de l'espèce pourra la régénérer.",
        action="/admin/renders/delete",
        fields=[("key", key), ("next", safe_next(next or "/admin/renders"))],
        submit="Supprimer",
        danger=True,
        back=safe_next(next or "/admin/renders"),
        details=[("espèce", info["species"]), ("niveau", info["zoom"]), ("jeu", info["hash"])],
    )


@router.post("/renders/delete")
async def render_delete(ctx: Session, form: Posted) -> Response:
    key = form.get("key")
    _render_key(key)
    bucket = _need_bucket(ctx)

    def remove() -> bool:
        if key not in bucket.list(key):
            return False
        bucket.delete([key])
        return True

    removed = await asyncio.to_thread(remove)
    if removed:
        log.info("backoffice : carte HD supprimée (%s)", key)
    if ctx.htmx:  # la ligne disparaît de la table
        return HTMLResponse("", headers=ROBOTS)
    back = safe_next(form.get("next") or "/admin/renders")
    if not removed:
        return ctx.redirect(
            back, ("warning", "Carte absente du bucket : déjà supprimée ou remplacée.")
        )
    return ctx.redirect(back, ("success", f"Carte supprimée : {key}"))


REPORT_SORTS = {
    "name": lambda r: r["name"],
    "species": lambda r: (r["species_name"].lower(), r["name"]),
    "dept": lambda r: (str(r.get("dept") or ""), r["name"]),
    "auc": lambda r: r.get("auc"),
}


def _reports(bucket) -> list[dict[str, Any]]:
    names = _species_names()
    return [
        {**r, "species_name": names.get(r["species"], r["species"])}
        for r in _species_view().list_reports(bucket)
    ]


@router.get("/reports")
async def reports_page(ctx: Session) -> Response:
    lst = views.listing(
        "/admin/reports",
        ctx.request.query_params,
        ("q", "species", "dept", "entry"),
        tuple(REPORT_SORTS),
        "name",
    )
    rows, problem = await _bucket_rows(ctx, _reports)
    f = lst.params

    def verdict(r: dict[str, Any]) -> str:
        entry = r.get("entry")
        return "" if not entry else "atteint" if entry.get("met") else "non"

    kept = [
        r
        for r in rows
        if (not f["q"] or views.matches(f["q"], r["name"], r["species_name"], r.get("variant")))
        and (not f["species"] or r["species"] == f["species"])
        and (not f["dept"] or str(r.get("dept")) == f["dept"])
        and (not f["entry"] or verdict(r) == f["entry"])
    ]
    page = views.paginate(views.sort_items(kept, REPORT_SORTS, lst), lst.page)
    lst.page = page.page
    return await ctx.render(
        "reports.html",
        partials={"reports-table": "partials/_reports_table.html"},
        lst=lst,
        page=page,
        total=len(rows),
        problem=problem,
        species=sorted({(r["species"], r["species_name"]) for r in rows}, key=lambda s: s[1]),
        depts=sorted({str(r["dept"]) for r in rows if r.get("dept")}),
    )


@router.get("/reports/open")
async def report_open(ctx: Session, key: str = "") -> Response:
    from champis_pipeline.check.report import REPORTS_PREFIX

    name = key[len(REPORTS_PREFIX) :]
    if (
        not key.startswith(REPORTS_PREFIX)
        or not name.endswith(".html")
        or "/" in name
        or ".." in name
    ):
        raise PageError(422, "Clé de rapport invalide", "Seuls les rapports reports/<nom>.html s'ouvrent ici.")  # fmt: skip
    _need_bucket(ctx)
    return _presigned(ctx, key)


def _artifacts(bucket) -> list[dict[str, Any]]:
    from champis_pipeline import artifacts

    return artifacts.list_artifacts(bucket)


@router.get("/artifacts")
async def artifacts_page(ctx: Session) -> Response:
    rows, problem = await _bucket_rows(ctx, _artifacts)
    return await ctx.render("artifacts.html", rows=rows, problem=problem)


@router.get("/artifacts/open")
async def artifact_open(ctx: Session, key: str = "") -> Response:
    from champis_pipeline import artifacts

    name = key[len(artifacts.PREFIX) :]
    if not key.startswith(artifacts.PREFIX) or not name or "/" in name or ".." in name:
        raise PageError(422, "Clé de sortie invalide", "Seules les sorties artifacts/<nom> s'ouvrent ici.")  # fmt: skip
    _need_bucket(ctx)
    return _presigned(ctx, key)


# ------------------------------------------------------------------ recherche


@router.get("/search")
async def search(ctx: Session, q: str = "") -> Response:
    """Barre de recherche de l'en-tête : espèces, outils, pages, et une tâche par le début de son
    identifiant. Un seul résultat : on y va directement."""
    q = views.clean(q)
    hits: list[dict[str, str]] = []
    if q:
        for s in _species_choices():
            if views.matches(q, s["name"], s["id"], s["group"]):
                hits.append({"kind": "Espèce", "label": s["name"], "hint": f"{s['id']} · {s['group']}", "url": f"/admin/species/{s['id']}"})  # fmt: skip
        for t in tools.describe():
            if views.matches(q, t["label"], t["kind"]):
                hits.append({"kind": "Outil", "label": t["label"], "hint": t["kind"], "url": f"/admin/tools/{t['kind']}"})  # fmt: skip
        for label, url in NAV:
            if views.matches(q, label):
                hits.append({"kind": "Page", "label": label, "hint": "", "url": url})
        if ctx.store is not None and len(q) >= 6 and " " not in q:
            found = await asyncio.to_thread(ctx.store.list_jobs, 5, search=q)
            for j in found:
                if j["id"].startswith(q):
                    hits.append({"kind": "Tâche", "label": views.kind_label(j["kind"]), "hint": f"{j['id'][:8]} · {j['status']}", "url": f"/admin/jobs/{j['id']}"})  # fmt: skip
    if len(hits) == 1:
        return RedirectResponse(hits[0]["url"], 303, headers=ROBOTS)
    return await ctx.render(
        "search.html", q=q, search_q=q, hits=hits, jobs_url="/admin/jobs?" + urlencode({"q": q})
    )
