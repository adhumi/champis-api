"""Rapport de contrôle `reports/<espèce>-<dept>.html` (+ `.json`) : T aux occurrences GBIF contre
T sur des pixels forestiers tirés au hasard (AUC), histogrammes, mois d'observation, quantiles de
pH SoilGrids aux occurrences (pour recalibrer `terrain.ph`, skill species-file) et carte miniature.

Critère de phase 1 : AUC > 0,7. Rien d'espèce ici : noms et palette lus depuis le dépôt.

Contrôle groupé (`check --depts`, 12 nov. 2026) : un département seul a rarement 30 occurrences
précises ; `evaluate_pooled` réunit les occurrences de plusieurs départements (dédoublonnées), évalue
chacune sur le raster T du premier département où elle tombe en forêt, et tire le fond dans les
mêmes départements au prorata de leur surface forestière. Un seul rapport, sans carte miniature
au-delà d'un département.

Variante (`check --edge`, 12 nov. 2026) : le même contrôle sur un T calculé avec un bloc candidat
(`check/variant.py`), comparé au T de référence sur les mêmes occurrences ; `entry_verdict` dit
si le critère d'entrée du facteur est atteint (au moins `ENTRY_MIN_IN_FOREST` occurrences en forêt,
gain d'AUC ≥ `ENTRY_MIN_AUC_GAIN`, T moyen aux occurrences qui ne baisse pas).
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import logging
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.warp import transform as rio_transform

from ..config import REPORTS_DIR, T_DIR, dept_dirs
from ..scoring import NODATA_U8
from ..species import load_palette, load_species, species_hash
from ..storage import BucketLike
from ..terrain import summary
from .gbif import Occurrence, Rejected

log = logging.getLogger(__name__)

AUC_TARGET = 0.7
BACKGROUND_RATIO = 10  # points de fond par occurrence
BACKGROUND_MIN = 2000
PH_QUANTILES = (1, 10, 90, 99)
MAP_PX = 600
ENTRY_MIN_IN_FOREST = 30  # en dessous, « non évalué »
ENTRY_MIN_AUC_GAIN = 0.01
REPORTS_PREFIX = "reports/"  # dans le bucket, quand le contrôle tourne sur le worker


def auc(positives: np.ndarray, background: np.ndarray) -> float:
    """AUC = statistique de Mann-Whitney (rangs moyens sur les ex æquo), sans dépendance."""
    pos = np.asarray(positives, dtype=float)
    neg = np.asarray(background, dtype=float)
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = np.argsort(allv, kind="mergesort")
    ranks = np.empty(allv.size, dtype=float)
    sorted_v = allv[order]
    i = 0
    while i < allv.size:
        j = i
        while j + 1 < allv.size and sorted_v[j + 1] == sorted_v[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1  # rang moyen, base 1
        i = j + 1
    r_pos = ranks[: pos.size].sum()
    return float((r_pos - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size))


def sample_raster(path: Path, lons: list[float], lats: list[float]) -> np.ndarray:
    """Valeurs brutes du raster aux points WGS84 ; nodata du raster → NaN, hors emprise → NaN."""
    if not lons:
        return np.array([], dtype=float)
    with rasterio.open(path) as s:
        xs, ys = rio_transform("EPSG:4326", s.crs, lons, lats)
        vals = np.array([v[0] for v in s.sample(zip(xs, ys, strict=True))], dtype=float)
        rows, cols = rasterio.transform.rowcol(s.transform, xs, ys)
        inside = (
            (np.asarray(rows) >= 0)
            & (np.asarray(rows) < s.height)
            & (np.asarray(cols) >= 0)
            & (np.asarray(cols) < s.width)
        )
        nodata = s.nodata
    vals = np.where(inside, vals, np.nan)
    if nodata is not None:
        vals = np.where(vals == nodata, np.nan, vals)
    return vals


def background_t(t_path: Path, n: int, rng: np.random.Generator) -> np.ndarray:
    """T (0–1) sur n pixels forestiers (non nodata) tirés uniformément."""
    with rasterio.open(t_path) as s:
        v = s.read(1)
    rows, cols = np.nonzero(v != NODATA_U8)
    if rows.size == 0:
        return np.array([], dtype=float)
    idx = rng.integers(0, rows.size, size=n)  # avec remise : n peut dépasser la forêt
    return v[rows[idx], cols[idx]] / 254.0


def default_t_path(dept: str, species_id: str) -> Path:
    return T_DIR / species_id / f"{dept}-{species_hash(species_id)}.tif"


def depts_tag(depts: list[str]) -> str:
    """Le groupe de départements dans les noms de rapports : les codes jusqu'à cinq, sinon leur
    nombre et une empreinte (`12depts-a1b2c3`)."""
    if len(depts) <= 5:
        return "-".join(depts)
    digest = hashlib.sha1("-".join(depts).encode()).hexdigest()[:6]
    return f"{len(depts)}depts-{digest}"


def pooled_occurrences(
    depts: list[str], occ_by_dept: dict[str, list[Occurrence]]
) -> list[Occurrence]:
    """Les occurrences de plusieurs emprises réunies, dans l'ordre des départements : une
    occurrence déjà vue depuis une emprise précédente (mêmes coordonnées et date) ne compte
    qu'une fois ; à l'intérieur d'une emprise, rien n'est retiré (un département = l'ancien
    rapport). C'est ce que `evaluate_pooled` évalue."""
    seen: set[tuple[float, float, str]] = set()
    out: list[Occurrence] = []
    for d in depts:
        here = occ_by_dept.get(d, [])
        out += [o for o in here if (round(o.lat, 6), round(o.lon, 6), o.date) not in seen]
        seen |= {(round(o.lat, 6), round(o.lon, 6), o.date) for o in here}
    return out


def forest_pixels(t_path: Path) -> int:
    with rasterio.open(t_path) as s:
        return int((s.read(1) != NODATA_U8).sum())


def evaluate(
    dept: str,
    species_id: str,
    occ: list[Occurrence],
    seed: int = 0,
    rejected: Rejected | None = None,
    t_path: Path | None = None,
    variant: str | None = None,
) -> dict[str, Any]:
    """Toutes les mesures du rapport pour un département (le `.json` à côté du `.html`). `t_path`
    et `variant` : le T d'une variante du fichier espèce, nommée dans le rapport."""
    return evaluate_pooled(
        [dept],
        species_id,
        {dept: occ},
        seed=seed,
        rejected=rejected,
        t_paths={dept: t_path} if t_path is not None else None,
        variant=variant,
    )


def evaluate_pooled(
    depts: list[str],
    species_id: str,
    occ_by_dept: dict[str, list[Occurrence]],
    seed: int = 0,
    rejected: Rejected | None = None,
    t_paths: dict[str, Path] | None = None,
    variant: str | None = None,
) -> dict[str, Any]:
    """Les mêmes mesures sur plusieurs départements réunis (12 nov. 2026) : occurrences
    dédoublonnées (mêmes coordonnées et date), chacune évaluée sur le raster T du premier
    département où elle tombe en forêt ; fond tiré dans chaque département au prorata de sa
    surface forestière ; pH, mois et surfaces réunis. Un département = l'ancien rapport."""
    paths = {d: (t_paths or {}).get(d) or default_t_path(d, species_id) for d in depts}
    for d, p in paths.items():
        if not p.exists():
            raise FileNotFoundError(f"T manquant : {p} (lancer `terrain --dept {d}`)")
    occ = pooled_occurrences(depts, occ_by_dept)  # une occurrence vue de deux emprises : une fois
    lons, lats = [o.lon for o in occ], [o.lat for o in occ]
    t_pos_list: list[float] = []
    ph_list: list[float] = []
    assigned = np.zeros(len(occ), dtype=bool)
    per_dept: dict[str, dict[str, int]] = {}
    for d in depts:
        t_raw = sample_raster(paths[d], lons, lats)
        here = ~np.isnan(t_raw) & ~assigned  # en forêt BD Forêt de ce département, pas déjà prise
        assigned |= here
        t_pos_list += (t_raw[here] / 254.0).tolist()
        ph_path = dept_dirs(d)["grid"] / "ph.tif"
        if ph_path.exists() and here.any():
            idx = np.nonzero(here)[0]
            ph_vals = sample_raster(ph_path, [lons[i] for i in idx], [lats[i] for i in idx])
            ph_list += (ph_vals[~np.isnan(ph_vals)] / 10.0).tolist()
        per_dept[d] = {"n_in_forest": int(here.sum())}
    t_pos = np.array(t_pos_list, dtype=float)
    rng = np.random.default_rng(seed)
    n_bg = max(BACKGROUND_MIN, BACKGROUND_RATIO * t_pos.size)
    forest = {d: forest_pixels(paths[d]) for d in depts}
    total_forest = sum(forest.values()) or 1
    t_bg_parts = []
    for d in depts:  # le fond au prorata de la forêt de chaque département
        n_d = round(n_bg * forest[d] / total_forest)
        per_dept[d]["n_background"] = n_d
        if n_d:
            t_bg_parts.append(background_t(paths[d], n_d, rng))
    t_bg = np.concatenate(t_bg_parts) if t_bg_parts else np.array([], dtype=float)
    ph_vals = np.array(ph_list, dtype=float)
    months = Counter(int(o.date[5:7]) for o in occ if len(o.date) >= 7)
    edges = np.linspace(0, 1, 11)
    surface: dict[str, float] = {}
    for d in depts:
        for k, v in summary(paths[d]).items():
            surface[k] = surface.get(k, 0.0) + v
    single = len(depts) == 1
    return {
        "species": species_id,
        "dept": depts[0] if single else depts_tag(depts),
        "depts": list(depts),
        "per_dept": per_dept,
        "t_hash": species_hash(species_id),
        "t_file": str(paths[depts[0]]) if single else None,
        "t_files": {d: str(p) for d, p in paths.items()},
        "variant": variant,
        "n_occurrences": len(occ),
        "precision": dict(Counter(o.precision for o in occ)),
        "rejected": asdict(rejected) if rejected else None,
        "n_in_forest": int(assigned.sum()),
        "n_background": int(t_bg.size),
        "auc": auc(t_pos, t_bg),
        "auc_target": AUC_TARGET,
        "t_mean_occurrences": float(t_pos.mean()) if t_pos.size else None,
        "t_mean_background": float(t_bg.mean()) if t_bg.size else None,
        "hist_edges": edges.tolist(),
        "hist_occurrences": np.histogram(t_pos, bins=edges)[0].tolist(),
        "hist_background": np.histogram(t_bg, bins=edges)[0].tolist(),
        "months": {str(m): months.get(m, 0) for m in range(1, 13)},
        "ph_quantiles": (
            {str(q): round(float(np.percentile(ph_vals, q)), 2) for q in PH_QUANTILES}
            if ph_vals.size >= 20
            else None
        ),
        "ph_n": int(ph_vals.size),
        "surface_ha": surface,
        "licenses": dict(Counter(o.license for o in occ)),
    }


def entry_verdict(baseline: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """Le critère d'entrée d'un facteur optionnel, mesuré sur les mêmes occurrences : assez de
    points en forêt, gain d'AUC, T moyen aux occurrences qui ne baisse pas. Rien n'est décidé ici :
    le bloc entre dans le référentiel par PR, si Adrien le retient."""
    n = min(baseline["n_in_forest"], variant["n_in_forest"])
    gain = variant["auc"] - baseline["auc"]
    mean_ok = (variant["t_mean_occurrences"] or 0.0) >= (baseline["t_mean_occurrences"] or 0.0)
    evaluated = n >= ENTRY_MIN_IN_FOREST
    met = bool(evaluated and gain >= ENTRY_MIN_AUC_GAIN and mean_ok)
    return {
        "min_in_forest": ENTRY_MIN_IN_FOREST,
        "min_auc_gain": ENTRY_MIN_AUC_GAIN,
        "n_in_forest": int(n),
        "auc_gain": float(gain),
        "t_mean_not_lower": bool(mean_ok),
        "evaluated": bool(evaluated),
        "met": met,
        "text": (
            f"non évalué : moins de {ENTRY_MIN_IN_FOREST} occurrences en forêt"
            if not evaluated
            else ("critère d'entrée atteint" if met else "critère d'entrée non atteint")
        ),
    }


def compare(baseline: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """Le rapport de la variante enrichi de la référence et du verdict (le `.json` se suffit)."""
    out = dict(variant)
    out["baseline"] = {
        k: baseline.get(k) for k in ("t_hash", "t_file", "auc", "t_mean_occurrences", "n_in_forest")
    }
    out["entry"] = entry_verdict(baseline, variant)
    return out


def report_stem(result: dict[str, Any]) -> str:
    stem = f"{result['species']}-{result['dept']}"
    return f"{stem}-{result['variant']}" if result.get("variant") else stem


def write_report(
    result: dict[str, Any], occ: list[Occurrence], out_dir: Path | None = None
) -> Path:
    out_dir = out_dir or REPORTS_DIR  # lu à l'appel : les tests le détournent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = report_stem(result)
    (out_dir / f"{stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if len(result.get("depts") or [result["dept"]]) == 1:  # un département : la carte
        t_path = Path(result.get("t_file") or default_t_path(result["dept"], result["species"]))
        map_b64 = _minimap(t_path, occ)
    else:
        map_b64 = ""
    page = _html(result, map_b64)
    path = out_dir / f"{stem}.html"
    path.write_text(page, encoding="utf-8")
    log.info("écrit %s (AUC %.3f)", path, result["auc"])
    return path


def publish_reports(bucket: BucketLike, html_path: Path) -> list[str]:
    """Dépose le rapport (`.html` et son `.json`) dans le bucket sous `reports/` ; renvoie les
    clés. Le backoffice les liste dans la fiche de l'espèce."""
    keys = []
    for path in (html_path, html_path.with_suffix(".json")):
        key = REPORTS_PREFIX + path.name
        bucket.upload(path, key)
        keys.append(key)
    return keys


# ---------------------------------------------------------------------- rendu


def _minimap(t_path: Path, occ: list[Occurrence]) -> str:
    """PNG base64 : T colorisé avec la palette, occurrences en points rouges."""
    pal = load_palette()
    with rasterio.open(t_path) as s:
        scale = MAP_PX / max(s.width, s.height)
        w, h = max(1, int(s.width * scale)), max(1, int(s.height * scale))
        v = s.read(1, out_shape=(h, w))
        xs, ys = rio_transform("EPSG:4326", s.crs, [o.lon for o in occ], [o.lat for o in occ])
        rows, cols = rasterio.transform.rowcol(s.transform, xs, ys) if occ else ([], [])
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    thresholds = sorted(pal["thresholds"], key=lambda t: t["min"])
    t = np.where(v == NODATA_U8, np.nan, v / 254.0)
    for th in thresholds:  # croissant : chaque seuil écrase le précédent
        m = t >= th["min"]
        rgba[m] = [*_rgb(th["hex"]), 255]
    img = Image.fromarray(rgba, "RGBA")
    draw = ImageDraw.Draw(img)
    for r, c in zip(np.atleast_1d(rows), np.atleast_1d(cols), strict=True):
        x, y = c * scale, r * scale
        draw.ellipse(
            [x - 2, y - 2, x + 2, y + 2], fill=(220, 0, 0, 255), outline=(255, 255, 255, 255)
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _bars(values: list[int], color: str, width: int = 420, height: int = 90) -> str:
    """Histogramme SVG inline, barres de largeur égale."""
    n = len(values)
    top = max(values) or 1
    bw = width / n
    rects = "".join(
        f'<rect x="{i * bw + 1:.1f}" y="{height - v / top * height:.1f}" '
        f'width="{bw - 2:.1f}" height="{v / top * height:.1f}" fill="{color}"/>'
        for i, v in enumerate(values)
    )
    return f'<svg width="{width}" height="{height}" role="img">{rects}</svg>'


def _html(r: dict[str, Any], map_b64: str) -> str:
    sp = load_species(r["species"])
    name = html.escape(f"{sp['names']['fr']} ({sp['names']['la']})")
    ok = r["auc"] >= r["auc_target"]
    verdict = (
        ("AUC ≥ 0,7 : zones crédibles" if ok else "AUC < 0,7 : à revoir")
        if r["n_in_forest"]
        else "pas assez d'occurrences en forêt"
    )
    color = "#1B6B3A" if ok else "#B8402E"
    ph = r["ph_quantiles"]
    ph_rows = (
        "".join(f"<tr><td>q{q} %</td><td>{v:.2f}</td></tr>" for q, v in ph.items())
        if ph
        else f"<tr><td colspan=2>trop peu de points pH ({r['ph_n']})</td></tr>"
    )
    surf = r["surface_ha"]
    surf_rows = "".join(
        f"<tr><td>{k}</td><td>{v:,.0f}</td></tr>".replace(",", " ") for k, v in surf.items()
    )
    month_names = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
    months = [r["months"][str(m)] for m in range(1, 13)]
    lic = ", ".join(f"{html.escape(k or '?')} : {v}" for k, v in r["licenses"].items())
    depts = r.get("depts") or [r["dept"]]
    where = (
        f"département {html.escape(r['dept'])}"
        if len(depts) == 1
        else f"{len(depts)} départements réunis ({html.escape(', '.join(depts))})"
    )
    per_dept = r.get("per_dept") or {}
    dept_rows = "".join(
        f"<tr><td>{html.escape(d)}</td><td>{v.get('n_in_forest', 0)}</td>"
        f"<td>{v.get('n_background', 0)}</td></tr>"
        for d, v in per_dept.items()
    )
    map_block = (
        f'<h2>Carte</h2><img src="data:image/png;base64,{map_b64}" alt="T colorisé et occurrences" style="max-width:100%;border:1px solid #ddd">'
        if map_b64
        else f"<h2>Par département</h2><table><tr><th>dépt</th><th>occ. en forêt</th><th>fond</th></tr>{dept_rows}</table>"
        f'<p class="cap">Contrôle groupé : chaque occurrence sur le T de son département, fond au prorata des surfaces forestières ; pas de carte.</p>'
    )
    variant = ""
    if r.get("variant"):
        base, entry = r.get("baseline") or {}, r.get("entry") or {}
        variant = (
            f"<p><b>Variante {html.escape(str(r['variant']), quote=False)}</b> contre le T de référence "
            f"{base.get('t_hash', '?')} sur les mêmes occurrences : AUC {_fmt(base.get('auc'))} → "
            f"{r['auc']:.3f} ({entry.get('auc_gain', 0.0):+.3f}), T moyen aux occurrences "
            f"{_fmt(base.get('t_mean_occurrences'))} → {_fmt(r['t_mean_occurrences'])}. "
            f"<b>{html.escape(entry.get('text', ''), quote=False)}</b> (au moins {entry.get('min_in_forest')} "
            f"occurrences en forêt, gain d'AUC ≥ {entry.get('min_auc_gain')}, T moyen qui ne baisse "
            f"pas). Le fichier espèce n'est pas modifié : le bloc entre par PR.</p>"
        )
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Contrôle {name} — {html.escape(r["dept"])}</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:24px;max-width:960px;color:#1a1a1a}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}} table{{border-collapse:collapse}}
td,th{{padding:3px 10px;border-bottom:1px solid #ddd;text-align:left}} .v{{font-size:22px;color:{color};font-weight:600}}
.row{{display:flex;gap:32px;flex-wrap:wrap}} .cap{{color:#666;font-size:12px}}</style></head><body>
<h1>Contrôle GBIF — {name}, {where}</h1>
<p class="v">AUC = {r["auc"]:.3f} — {verdict}</p>
{variant}<p>{r["n_occurrences"]} occurrences GBIF retenues ({_precision(r)}), dont {r["n_in_forest"]} en forêt BD Forêt ;
fond : {r["n_background"]} pixels forestiers tirés au hasard. T moyen : {_fmt(r["t_mean_occurrences"])} aux occurrences
contre {_fmt(r["t_mean_background"])} sur le fond. T = {r["t_hash"]}.</p>
<div class="row"><div><h2>T aux occurrences</h2>{_bars(r["hist_occurrences"], "#1B6B3A")}<div class="cap">0 → 1 par dixième</div></div>
<div><h2>T sur le fond forestier</h2>{_bars(r["hist_background"], "#999")}<div class="cap">0 → 1 par dixième</div></div></div>
<div class="row"><div><h2>Mois des observations</h2>{_bars(months, "#E3C64B")}<div class="cap">{" ".join(month_names)} — à comparer à <code>phenology</code></div></div>
<div><h2>pH SoilGrids aux occurrences</h2><table>{ph_rows}</table><div class="cap">quantiles pour recalibrer <code>terrain.ph</code> (skill species-file)</div></div>
<div><h2>Surface par classe (ha)</h2><table>{surf_rows}</table></div></div>
{map_block}
<p class="cap">Occurrences GBIF — licences par jeu : {lic or "—"}. Fond IGN BD Forêt V2, Copernicus DEM, SoilGrids.</p>
</body></html>"""


def _precision(r: dict[str, Any]) -> str:
    p = r.get("precision") or {}
    parts = [
        f"{p.get('declared', 0)} avec incertitude déclarée ≤ 100 m",
        f"{p.get('fine_coords', 0)} sans incertitude mais coordonnées ≥ 4 décimales",
    ]
    rj = r.get("rejected")
    if rj:
        parts.append(
            f"écartées : {rj['undated']} sans date, {rj['imprecise']} imprécises, "
            f"{rj['rounded']} arrondies"
        )
        by_m = rj.get("imprecise_by_m") or {}
        if by_m:
            order = sorted(by_m, key=lambda k: float(k.strip(">").split("-")[0]))
            parts.append(
                "incertitudes écartées (m) : " + ", ".join(f"{k} : {by_m[k]}" for k in order)
            )
    return " ; ".join(parts)


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"
