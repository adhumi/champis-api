"""Ce que les pages du backoffice affichent, préparé hors des gabarits (lot BO6).

Fonctions pures, sans requête ni base : mise en forme (tailles, dates à l'heure de Paris, durées),
état d'une liste porté par la query string (recherche, filtres, tri, page), petits graphiques en
SVG calculés ici (barres par jour, courbe Φ, trapèzes) et les étapes d'une espèce avec la tâche
que chacune lance. Les gabarits ne calculent rien : ils posent ce que ces fonctions renvoient.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .. import jobs, tools

PARIS = ZoneInfo("Europe/Paris")
PER_PAGE = 50
NBSP = " "

STATES = {
    "a_jour": "à jour",
    "perimee": "périmée",
    "jamais_publiee": "jamais publiée",
    "en_cours": "en cours",
}
STATE_COLORS = {
    "a_jour": "green",
    "perimee": "orange",
    "jamais_publiee": "secondary",
    "en_cours": "blue",
}
STATUS_COLORS = {
    "ok": "green",
    "running": "blue",
    "queued": "azure",
    "failed": "red",
    "cancelled": "secondary",
    "manque": "orange",
    "absent": "orange",
    "warn": "orange",
    "error": "red",
    "info": "azure",
}
STATUSES = ("queued", "running", "ok", "failed", "cancelled")
ORIGINS = ("cron", "admin", "cli")
MISSING = {
    "about": "fiche en mots",
    "photo": "photo",
    "illustration": "illustration",
    "tiles": "tuiles publiées",
}
ACTIVE = ("queued", "running")


# ------------------------------------------------------------------ mise en forme


def fmt_n(value: Any) -> str:
    """Entier avec séparateur de milliers ; « — » quand il n'y a rien."""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}".replace(",", NBSP)
    except (TypeError, ValueError):
        return str(value)


def fmt_num(value: Any, digits: int = 2) -> str:
    """Nombre à la française, sans zéros inutiles ; le reste tel quel."""
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def fmt_size(value: Any) -> str:
    if value is None:
        return "—"
    n = float(value)
    for unit, div in (("Go", 1e9), ("Mo", 1e6), ("ko", 1e3)):
        if n >= div:
            v = n / div
            return (
                f"{v:.1f}".replace(".", ",") + f"{NBSP}{unit}"
                if v < 100
                else f"{v:.0f}{NBSP}{unit}"
            )
    return f"{int(n)}{NBSP}o"


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fmt_when(value: Any) -> str:
    """Date et heure à Paris (`13/11/2026 16:05`) ; « jamais » sans valeur."""
    dt = parse_iso(value)
    if dt is None:
        return str(value) if value else "jamais"
    return dt.astimezone(PARIS).strftime("%d/%m/%Y %H:%M")


def fmt_time(value: Any) -> str:
    dt = parse_iso(value)
    return dt.astimezone(PARIS).strftime("%H:%M:%S") if dt else ""


def fmt_ago(value: Any, now: datetime | None = None) -> str:
    dt = parse_iso(value)
    if dt is None:
        return ""
    hours = max(0.0, ((now or datetime.now(UTC)) - dt).total_seconds() / 3600)
    if hours < 1:
        return f"il y a {round(hours * 60)} min"
    if hours < 48:
        return f"il y a {hours:.0f} h"
    return f"il y a {round(hours / 24)} j"


def fmt_duration(start: Any, end: Any = None, now: datetime | None = None) -> str:
    a = parse_iso(start)
    if a is None:
        return "—"
    b = parse_iso(end) or now or datetime.now(UTC)
    s = max(0, int((b - a).total_seconds()))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def kind_label(kind: str) -> str:
    return jobs.KINDS.get(kind, kind)


def last_word(job: Mapping[str, Any]) -> str:
    """Ce qu'une tâche a dit en dernier : résumé, message, ou l'étape en cours."""
    if job.get("summary") or job.get("message"):
        return job.get("summary") or job.get("message") or ""
    if job.get("step_name"):
        return f"[{job.get('step_index')}/{job.get('step_total')}] {job['step_name']}"
    return ""


def job_percent(job: Mapping[str, Any]) -> float:
    if job.get("progress_total"):
        return min(100.0, 100.0 * (job.get("progress_done") or 0) / job["progress_total"])
    if job.get("status") == "ok":
        return 100.0
    if job.get("step_total"):
        return 100.0 * ((job.get("step_index") or 1) - 1) / job["step_total"]
    return 0.0


def job_progress_text(job: Mapping[str, Any]) -> str:
    parts = []
    if job.get("step_name"):
        parts.append(f"étape {job.get('step_index')}/{job.get('step_total')} : {job['step_name']}")
    if job.get("progress_total"):
        parts.append(
            f"{fmt_n(job.get('progress_done') or 0)} / {fmt_n(job['progress_total'])} "
            f"{job.get('progress_unit') or ''}".strip()
        )
    if job.get("message"):
        parts.append(job["message"])
    return " · ".join(parts)


def job_args_text(job: Mapping[str, Any]) -> str:
    args = job.get("args") or {}
    return " · ".join(f"{k} = {v}" for k, v in args.items() if v not in ("", None))


def completeness_text(c: Mapping[str, Any] | None) -> str:
    if not c:
        return ""
    if c.get("complete"):
        return "complète"
    return "manque : " + ", ".join(MISSING.get(k, k) for k in c.get("missing", []))


# ------------------------------------------------------------------ listes : query string, tri, pages


def clean(value: str | None, limit: int = 80) -> str:
    return (value or "").strip()[:limit]


def to_int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value or default)))
    except ValueError:
        return default


@dataclass
class Listing:
    """L'état d'une liste tel que l'URL le porte : la page marche sans JavaScript, se recharge et
    se partage. `url(page=2)` rend l'adresse de la même liste avec ce changement."""

    path: str
    params: dict[str, str] = field(default_factory=dict)
    sort: str = ""
    desc: bool = False
    default_sort: str = ""
    default_desc: bool = False
    page: int = 1

    def query(self, **changes: Any) -> dict[str, str]:
        out = {k: v for k, v in self.params.items() if v}
        if self.sort and (self.sort, self.desc) != (self.default_sort, self.default_desc):
            out["sort"] = self.sort
            out["dir"] = "desc" if self.desc else "asc"
        if self.page > 1:
            out["page"] = str(self.page)
        for k, v in changes.items():
            if v in (None, "", False):
                out.pop(k, None)
            else:
                out[k] = str(v)
        if out.get("page") == "1":
            out.pop("page")
        return out

    def url(self, **changes: Any) -> str:
        q = self.query(**changes)
        return f"{self.path}?{urlencode(q)}" if q else self.path

    def sort_url(self, column: str) -> str:
        """Clic sur un en-tête : trie par cette colonne, ou inverse le sens si c'est déjà elle."""
        desc = not self.desc if column == self.sort else False
        if (column, desc) == (self.default_sort, self.default_desc):
            return self.url(sort=None, dir=None, page=None)
        return self.url(sort=column, dir="desc" if desc else "asc", page=None)

    def arrow(self, column: str) -> str:
        return ("↓" if self.desc else "↑") if column == self.sort else ""

    @property
    def filtered(self) -> bool:
        return any(self.params.values())


def listing(
    path: str,
    query: Mapping[str, str],
    filters: Sequence[str],
    sorts: Sequence[str],
    default_sort: str,
    default_desc: bool = False,
) -> Listing:
    """Lit `?q=…&sort=…&dir=…&page=…` ; une colonne de tri inconnue retombe sur le défaut."""
    sort = query.get("sort", "")
    known = sort in sorts
    return Listing(
        path=path,
        params={name: clean(query.get(name)) for name in filters},
        sort=sort if known else default_sort,
        desc=(query.get("dir") == "desc") if known else default_desc,
        default_sort=default_sort,
        default_desc=default_desc,
        page=to_int(query.get("page"), 1, 1, 100_000),
    )


@dataclass
class Page:
    items: list[Any]
    page: int
    pages: int
    total: int
    per: int

    @property
    def first(self) -> int:
        return (self.page - 1) * self.per + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.per)

    @property
    def window(self) -> list[int]:
        """Les numéros proposés autour de la page courante."""
        return list(range(max(1, self.page - 2), min(self.pages, self.page + 2) + 1))


def page_of(total: int, page: int, per: int = PER_PAGE) -> tuple[int, int]:
    """(page ramenée dans les bornes, nombre de pages)."""
    pages = max(1, math.ceil(total / per))
    return min(max(1, page), pages), pages


def paginate(items: Sequence[Any], page: int, per: int = PER_PAGE) -> Page:
    page, pages = page_of(len(items), page, per)
    return Page(list(items[(page - 1) * per : page * per]), page, pages, len(items), per)


def matches(needle: str, *haystack: Any) -> bool:
    """Tous les mots cherchés se trouvent dans l'un des champs (sans casse)."""
    text = " ".join(str(h) for h in haystack if h).lower()
    return all(word in text for word in needle.lower().split())


def sort_items(
    items: Iterable[Any], keys: Mapping[str, Callable[[Any], Any]], lst: Listing
) -> list[Any]:
    key = keys.get(lst.sort)
    out = list(items)
    if key is not None:
        # les valeurs absentes restent en fin de liste, quel que soit le sens
        present = sorted((i for i in out if key(i) is not None), key=key, reverse=lst.desc)
        out = present + [i for i in out if key(i) is None]
    return out


# ------------------------------------------------------------------ graphiques (SVG posé par le gabarit)


def sparkline(days: Sequence[str], values: Mapping[str, int], w: int = 300, h: int = 48) -> dict:
    """Une barre par jour ; un jour sans relevé garde un trait d'un pixel, marqué `empty`."""
    n = max(1, len(days))
    bw = w / n
    top = max([1, *(values.get(d) or 0 for d in days)])
    bars = []
    for i, d in enumerate(days):
        v = values.get(d)
        bh = 1.0 if v is None else max(1.0, v / top * h)
        bars.append(
            {
                "x": round(i * bw, 2),
                "y": round(h - bh, 2),
                "w": round(max(0.5, bw - 0.6), 2),
                "h": round(bh, 2),
                "empty": v is None,
                "title": f"{d} : {'·' if v is None else v}",
            }
        )
    return {"w": w, "h": h, "bars": bars}


MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")  # fmt: skip
MONTH_STARTS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def phi_chart(curves: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """La fenêtre Φ jour par jour : une ligne par altitude, repères à 0, 0,25 et 1, mois en abscisse."""
    w, h, left, right, top, bottom = 460, 96, 6, 6, 6, 18

    def x(i: float) -> float:
        return round(left + i / 364 * (w - left - right), 1)

    def y(v: float) -> float:
        return round(top + (1 - v) * (h - top - bottom), 1)

    return {
        "w": w,
        "h": h,
        "x1": left,
        "x2": w - right,
        "levels": [{"y": y(v), "axis": v == 0} for v in (0, 0.25, 1)],
        "months": [
            {"x": x(d), "label": MONTHS[i], "y1": y(1), "y2": y(0)}
            for i, d in enumerate(MONTH_STARTS)
        ],
        "curves": [
            {
                "altitude_m": c["altitude_m"],
                "d": " ".join(
                    f"{'L' if j else 'M'}{x(j)},{y(float(v))}" for j, v in enumerate(c["days"])
                ),
            }
            for c in curves
        ],
    }


TRAPEZOID_KEYS = ("min", "opt_low", "opt_high", "max")


def is_trapezoid(value: Any) -> bool:
    return isinstance(value, Mapping) and all(k in value for k in TRAPEZOID_KEYS)


def trapezoid(t: Mapping[str, Any], unit: str = "") -> dict[str, Any]:
    vals = [float(t[k]) for k in TRAPEZOID_KEYS]
    lo, span = vals[0], (vals[3] - vals[0]) or 1.0
    w, h, pad, top, bottom = 160, 26, 2, 3, 23

    def x(v: float) -> float:
        return round(pad + (v - lo) / span * (w - 2 * pad), 1)

    label = f"{fmt_num(vals[0])} · {fmt_num(vals[1])}–{fmt_num(vals[2])} · {fmt_num(vals[3])}"
    return {
        "w": w,
        "h": h,
        "bottom": bottom,
        "x1": pad,
        "x2": w - pad,
        "points": f"{x(vals[0])},{bottom} {x(vals[1])},{top} {x(vals[2])},{top} {x(vals[3])},{bottom}",
        "label": f"{label} {unit}".strip(),
    }


def weight_bars(obj: Mapping[str, Any], order: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Poids 0–1 en barres : dans l'ordre donné, sinon du plus fort au plus faible, `default` à la fin."""
    numeric = {
        k: v for k, v in obj.items() if isinstance(v, int | float) and not isinstance(v, bool)
    }
    if order:
        known = [k for k in order if k in numeric]
    else:
        known = sorted((k for k in numeric if k != "default"), key=lambda k: -numeric[k])
    rest = [k for k in numeric if k not in known]
    return [
        {"key": k, "value": numeric[k], "pct": round(min(1.0, max(0.0, float(numeric[k]))) * 100)}
        for k in [*known, *rest]
    ]


def share_bars(shares: Mapping[str, int]) -> dict[str, Any]:
    total = sum(shares.values())
    rows = sorted(shares.items(), key=lambda kv: -kv[1])
    return {
        "total": total,
        "rows": [
            {"key": k, "n": n, "pct": round(100 * n / total) if total else 0} for k, n in rows
        ],
    }


def params(obj: Mapping[str, Any], units: Mapping[str, str] | None = None, skip: Sequence[str] = ()) -> list[dict[str, Any]]:  # fmt: skip
    """Paramètres d'un bloc du fichier espèce : trapèzes dessinés, scalaires avec leur unité, le
    reste montré brut (`raw`) ; rien n'est caché."""
    units = units or {}
    out = []
    for k, v in obj.items():
        if k in skip:
            continue
        if is_trapezoid(v):
            out.append({"key": k, "trapezoid": trapezoid(v, units.get(k, ""))})
        elif isinstance(v, Mapping | list):
            out.append({"key": k, "raw": v})
        else:
            out.append({"key": k, "text": f"{fmt_num(v)} {units.get(k, '')}".strip()})
    return out


WEATHER_UNITS = {
    "window_days": "j",
    "c": "°C",
    "mm": "mm",
    "event_mm": "mm",
    "event_days": "j",
    "lag_days": "j",
    "tmin_c": "°C",
    "ramp_c": "°C",
    "nights": "nuits",
    "moderate_mm": "mm",
    "severe_mm": "mm",
    "tmax_c": "°C",
    "days": "j",
}
WEATHER_FACTORS = (
    ("temp_mean", "température moyenne"),
    ("rain", "pluie cumulée"),
    ("trigger", "pluie déclenchante"),
    ("soil_moisture_pct", "humidité du sol (percentile)"),
    ("frost", "gel (rampe)"),
    ("water_balance", "bilan hydrique"),
    ("heat", "chaleur"),
    ("memory", "mémoire"),
    ("persistence", "persistance"),
)
FILE_KNOWN = (
    "id", "schema_version", "group", "names", "notes", "references", "terrain", "phenology",
    "weather", "about",
)  # fmt: skip
ASPECTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW", "flat")
COVERS = ("fermee", "ouverte", "sans_couvert", "plantation", "non_foret")
TERRAIN_WEIGHTS = ("cover", "ph", "altitude", "aspect", "slope")


def file_sections(data: Mapping[str, Any]) -> dict[str, Any]:
    """Le fichier `species/<id>.json` découpé pour sa page : terrain, phénologie, météo, fiche en
    mots, références ; les clés que la page ne connaît pas sont rendues brutes dans `extra`."""
    f = data["file"]
    about = f.get("about") or {}
    refs = {**(f.get("references") or {}), **(about.get("references") or {})}
    terrain = f.get("terrain") or {}
    trefs = terrain.get("refs") or {}
    phenology = f.get("phenology") or {}
    weather = f.get("weather") or {}
    wrefs = weather.get("refs") or {}
    factors = []
    labels = dict(WEATHER_FACTORS)
    ordered = [k for k, _ in WEATHER_FACTORS if weather.get(k) is not None]
    others = [k for k in weather if k not in labels and k not in ("weights", "refs")]
    for k in [*ordered, *others]:
        v = weather[k]
        factors.append(
            {
                "key": k,
                "label": labels.get(k, k),
                "trapezoid": trapezoid(v) if is_trapezoid(v) else None,
                "params": params(v, WEATHER_UNITS)
                if isinstance(v, Mapping) and not is_trapezoid(v)
                else None,
                "text": None if isinstance(v, Mapping) else fmt_num(v),
                "refs": wrefs.get(k) or [],
            }
        )
    weights = terrain.get("weights") or {}
    return {
        "refs": refs,
        "ref_rows": sorted(refs.items()),
        "terrain": {
            "refs": trefs,
            "hosts": weight_bars(terrain["hosts"]) if terrain.get("hosts") else [],
            "cover": weight_bars(terrain["cover"], COVERS) if terrain.get("cover") else [],
            "aspect": weight_bars(terrain["aspect"], ASPECTS) if terrain.get("aspect") else [],
            "weights": weight_bars(weights, TERRAIN_WEIGHTS) if weights else [],
            "weights_sum": sum(float(v) for v in weights.values() if isinstance(v, int | float)),
            "trapezoids": [
                {
                    "label": label,
                    "trapezoid": trapezoid(terrain[key], unit),
                    "refs": trefs.get(key) or trefs.get(key.split("_")[0]) or [],
                }
                for key, label, unit in (
                    ("ph", "pH (plancher 0,15)", ""),
                    ("altitude_m", "altitude", "m"),
                    ("slope_deg", "pente", "°"),
                )
                if is_trapezoid(terrain.get(key))
            ],
            "params": params(
                terrain,
                skip=(
                    "hosts",
                    "cover",
                    "ph",
                    "altitude_m",
                    "slope_deg",
                    "aspect",
                    "weights",
                    "refs",
                ),
            ),
        },
        "phenology": {
            "chart": phi_chart(data["phi"]) if data.get("phi") else None,
            "params": params(
                phenology,
                skip=("season", "peak", "ramp_days", "secondary_peak", "altitude_shift", "refs"),
            ),
        },
        "weather": {
            "factors": factors,
            "weights": weight_bars(weather["weights"]) if weather.get("weights") else [],
        },
        "about_params": params(
            about,
            skip=("summary", "habitat", "season", "confusions", "photo", "refs", "references"),
        ),
        "extra": {k: f[k] for k in f if k not in FILE_KNOWN},
    }


# ------------------------------------------------------------------ étapes d'une espèce


@dataclass
class Step:
    """Une étape de la page d'une espèce : son état, et la tâche que son bouton lance."""

    key: str
    title: str
    detail: str
    dot: str = ""  # ok | warn | "" (inconnu)
    kind: str | None = None  # outil lancé par le bouton
    args: dict[str, str] = field(default_factory=dict)
    label: str = ""
    disabled: str = ""  # pourquoi le bouton est éteint ; vide = actif


def _b(value: bool) -> str:
    return "true" if value else "false"


def species_steps(sp: Mapping[str, Any], grid: Mapping[str, Any], zoom: int) -> list[Step]:
    """Les étapes du pipeline pour une espèce, une à la fois (spec › Backoffice › Espèces) : chaque
    bouton lance l'outil avec les options de la fiche ; le lien « options » ouvre son formulaire."""
    pub, w = sp.get("published"), sp.get("worker")
    busy = "une tâche de l'espèce est en cours" if sp.get("jobs") else ""
    no_worker = "" if w else "volume inconnu : le worker n'a pas encore battu"
    group = " ".join(sp["group"]["species"])
    gname = sp["group"]["name"]
    steps: list[Step] = []

    if w:
        detail = (
            f"T à jour {fmt_n(w['t_current'])} / {fmt_n(w['depts'])} départements · "
            f"{fmt_n(w['t_stale'])} périmés · dernier batch "
            f"{fmt_when(w['batch_last']) if w.get('batch_last') else 'jamais'}"
        )
    else:
        detail = no_worker
    steps.append(
        Step(
            "batch",
            "Terrain sur le worker",
            detail,
            "" if not w else "ok" if w["t_current"] >= w["depts"] else "warn",
            "batch",
            {"depts": "all", "species": group, "jobs": "2", "clean_raw": "true", "force": "false"},
            "Recalculer le terrain du groupe",
            busy or no_worker,
        )
    )

    published = bool(pub and pub.get("hash"))
    if published:
        detail = (
            f"publié {pub['hash']}{'' if pub['hash'] == sp['hash'] else ' (périmé)'} · "
            f"{pub['depts']} dép. · {fmt_size(pub['bytes'])} · {pub['dept_sets']} jeux par "
            f"département · G {gname} {'à jour' if pub['group_current'] else 'à refaire'}"
        )
    else:
        detail = "aucun jeu France publié"
    missing = (w or {}).get("group_missing_t") or []
    if w:
        detail += f" · sur le worker : {w['tiles_current']} à jour, {w['tiles_stale']} périmés"
        if missing:
            detail += f" · T à calculer d'abord : {', '.join(missing)}"
    steps.append(
        Step(
            "tiles",
            "Tuiles",
            detail,
            ""
            if not published
            else "ok"
            if pub["hash"] == sp["hash"] and pub["group_current"]
            else "warn",
            "tiles",
            {
                "depts": "all",
                "species": group,
                "jobs": "2",
                "per_dept": "true",
                "groups": "true",
                "force": "false",
            },
            "Construire les tuiles",
            busy
            or no_worker
            or ("le terrain du groupe n'est pas complet sur le worker" if missing else ""),
        )
    )

    can_publish = bool(w and w.get("tiles_current"))
    cannot = "" if can_publish else "aucune tuile à jour sur le worker"
    publish = {"depts": "all", "species": group, "per_dept": "true", "groups": "true", "prune": "false"}  # fmt: skip
    steps.append(
        Step(
            "publish-dry",
            "Publication à blanc",
            "les PMTiles du worker vers le bucket : d'abord montrer seulement, et lire le journal",
            "ok" if can_publish else "",
            "publish",
            {**publish, "dry_run": "true"},
            "Montrer seulement",
            busy or cannot,
        )
    )
    steps.append(
        Step(
            "publish",
            "Publication réelle",
            f"téléverse les tuiles du groupe {gname} et réécrit le manifeste · jamais --prune depuis une fiche",
            "ok" if can_publish else "",
            "publish",
            {**publish, "dry_run": "false"},
            "Publier",
            busy or cannot,
        )
    )

    if grid.get("current"):
        detail = f"à jour (empreinte {grid.get('hash')})"
    else:
        detail = (
            f"empreinte {grid.get('hash') or 'absente'} ≠ {grid.get('current_hash')} : à refaire "
            "pour toutes les espèces, puis à committer"
        )
    steps.append(
        Step(
            "weather-grid",
            "Grille météo",
            detail,
            "ok" if grid.get("current") else "warn",
            "weather-grid",
            {"depts": "all", "to_bucket": "true"},
            "Refaire la grille",
            busy or no_worker,
        )
    )

    maps = sp.get("maps") or []
    steps.append(
        Step(
            "render",
            "Carte HD",
            f"{len(maps)} dans le bucket" if maps else "aucune carte",
            "ok" if any(m.get("current") for m in maps) else "",
            "render",
            {"species": sp["id"], "zoom": str(zoom), "force": "false"},
            f"Générer (niveau {zoom})",
            busy or ("" if published else "aucun jeu France publié"),
        )
    )
    return steps


def check_summary(report: Mapping[str, Any]) -> str:
    """« AUC 0,712 (+0,015, critère atteint) » pour un rapport GBIF."""
    auc = report.get("auc")
    text = f"AUC {'?' if auc is None else fmt_num(auc, 3)}"
    entry = report.get("entry")
    if entry:
        gain = entry.get("auc_gain") or 0.0
        verdict = (
            "critère atteint"
            if entry.get("met")
            else "critère non atteint"
            if entry.get("evaluated")
            else "non évalué"
        )
        text += f" ({'+' if gain >= 0 else '−'}{fmt_num(abs(gain), 3)}, {verdict})"
    return text


def confirmation(kind: str, args: Mapping[str, str]) -> str | None:
    """Ce qu'il faut confirmer avant de lancer : une publication réelle (avec `--prune`, le texte
    du catalogue) ou une carte HD refaite par-dessus celle du bucket."""
    tool = tools.TOOLS[kind]
    if kind == "publish" and args.get("dry_run") != "True":
        if args.get("prune") == "True":
            return tool.confirm
        target = args.get("species") or "toutes les espèces"
        return f"Publier dans le bucket les tuiles de : {target} ? Le manifeste est réécrit."
    if kind == "render" and args.get("force") == "True":
        return (
            f"Refaire la carte HD z{args.get('zoom')} de {args.get('species')} et remplacer "
            "celle du bucket ?"
        )
    if tool.confirm and kind != "publish":
        return tool.confirm
    return None
