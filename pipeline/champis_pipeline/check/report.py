"""Rapport de contrôle `reports/<espèce>-<dept>.html` (+ `.json`) : T aux occurrences GBIF contre
T sur des pixels forestiers tirés au hasard (AUC), histogrammes, mois d'observation, quantiles de
pH SoilGrids aux occurrences (pour recalibrer `terrain.ph`, skill species-file) et carte miniature.

Critère de phase 1 : AUC > 0,7. Rien d'espèce ici : noms et palette lus depuis le dépôt.
"""

from __future__ import annotations

import base64
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
from ..terrain import summary
from .gbif import Occurrence, Rejected

log = logging.getLogger(__name__)

AUC_TARGET = 0.7
BACKGROUND_RATIO = 10  # points de fond par occurrence
BACKGROUND_MIN = 2000
PH_QUANTILES = (1, 10, 90, 99)
MAP_PX = 600


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


def evaluate(
    dept: str,
    species_id: str,
    occ: list[Occurrence],
    seed: int = 0,
    rejected: Rejected | None = None,
) -> dict[str, Any]:
    """Toutes les mesures du rapport, sérialisables (le `.json` à côté du `.html`)."""
    t_path = T_DIR / species_id / f"{dept}-{species_hash(species_id)}.tif"
    if not t_path.exists():
        raise FileNotFoundError(f"T manquant : {t_path} (lancer `terrain --dept {dept}`)")
    ph_path = dept_dirs(dept)["grid"] / "ph.tif"
    lons, lats = [o.lon for o in occ], [o.lat for o in occ]
    t_raw = sample_raster(t_path, lons, lats)
    inside = ~np.isnan(t_raw)  # dans l'emprise et en forêt BD Forêt
    t_pos = t_raw[inside] / 254.0
    rng = np.random.default_rng(seed)
    t_bg = background_t(t_path, max(BACKGROUND_MIN, BACKGROUND_RATIO * t_pos.size), rng)
    ph_vals = sample_raster(ph_path, lons, lats) if ph_path.exists() else np.array([])
    ph_vals = ph_vals[~np.isnan(ph_vals)] / 10.0
    months = Counter(int(o.date[5:7]) for o in occ if len(o.date) >= 7)
    edges = np.linspace(0, 1, 11)
    return {
        "species": species_id,
        "dept": dept,
        "t_hash": species_hash(species_id),
        "n_occurrences": len(occ),
        "precision": dict(Counter(o.precision for o in occ)),
        "rejected": asdict(rejected) if rejected else None,
        "n_in_forest": int(inside.sum()),
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
        "surface_ha": summary(t_path),
        "licenses": dict(Counter(o.license for o in occ)),
    }


def write_report(
    result: dict[str, Any], occ: list[Occurrence], out_dir: Path = REPORTS_DIR
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result['species']}-{result['dept']}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    t_path = T_DIR / result["species"] / f"{result['dept']}-{result['t_hash']}.tif"
    page = _html(result, _minimap(t_path, occ))
    path = out_dir / f"{stem}.html"
    path.write_text(page, encoding="utf-8")
    log.info("écrit %s (AUC %.3f)", path, result["auc"])
    return path


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
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Contrôle {name} — {r["dept"]}</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:24px;max-width:960px;color:#1a1a1a}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}} table{{border-collapse:collapse}}
td,th{{padding:3px 10px;border-bottom:1px solid #ddd;text-align:left}} .v{{font-size:22px;color:{color};font-weight:600}}
.row{{display:flex;gap:32px;flex-wrap:wrap}} .cap{{color:#666;font-size:12px}}</style></head><body>
<h1>Contrôle GBIF — {name}, département {r["dept"]}</h1>
<p class="v">AUC = {r["auc"]:.3f} — {verdict}</p>
<p>{r["n_occurrences"]} occurrences GBIF retenues ({_precision(r)}), dont {r["n_in_forest"]} en forêt BD Forêt ;
fond : {r["n_background"]} pixels forestiers tirés au hasard. T moyen : {_fmt(r["t_mean_occurrences"])} aux occurrences
contre {_fmt(r["t_mean_background"])} sur le fond. T = {r["t_hash"]}.</p>
<div class="row"><div><h2>T aux occurrences</h2>{_bars(r["hist_occurrences"], "#1B6B3A")}<div class="cap">0 → 1 par dixième</div></div>
<div><h2>T sur le fond forestier</h2>{_bars(r["hist_background"], "#999")}<div class="cap">0 → 1 par dixième</div></div></div>
<div class="row"><div><h2>Mois des observations</h2>{_bars(months, "#E3C64B")}<div class="cap">{" ".join(month_names)} — à comparer à <code>phenology</code></div></div>
<div><h2>pH SoilGrids aux occurrences</h2><table>{ph_rows}</table><div class="cap">quantiles pour recalibrer <code>terrain.ph</code> (skill species-file)</div></div>
<div><h2>Surface par classe (ha)</h2><table>{surf_rows}</table></div></div>
<h2>Carte</h2><img src="data:image/png;base64,{map_b64}" alt="T colorisé et occurrences" style="max-width:100%;border:1px solid #ddd">
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
