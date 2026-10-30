"""Étape « tiles » : mosaïque → Web Mercator → PMTiles de données (T × 254 en gris 8 bits).

Chaîne 100 % Python : rasterio (mosaïque écrite sur disque, reprojection par tuile) → PNG 256 px
→ MBTiles (sqlite) → pmtiles.convert. Les tuiles sont des PNG de données, non colorisées : la
palette est appliquée par les clients. 255 = nodata (alpha 0).

Échelle France (phase 2) : le niveau 15 est rendu depuis la mosaïque par carrés de 64 × 64 tuiles en
parallèle (`--jobs`, les carrés sans donnée sont écartés d'une seule lecture), puis les niveaux 14 → 6 sont construits par réduction 2×2 des tuiles du
niveau supérieur (T : moyenne des pixels valides ; A : plus proche voisin) sans relire la mosaïque.

Deuxième couche, commune à toutes les espèces : `A` = classe d'altitude du pixel (gris = indice
k de la classe [300k, 300k+300[ m, centre 300k+150 = la classe que l'API renvoie), produite
depuis `alt.tif` sur toute l'emprise. Les clients y lisent la classe du pixel pour choisir
M_eff et Φ (spec : Points ouverts › classe d'altitude du pixel).
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import mercantile
import numpy as np
import rasterio
import rasterio.errors
from PIL import Image
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds

from . import SCORING_VERSION
from .config import CRS_TILES, T_DIR, TILES_DIR, dept_dirs
from .progress import tqdm
from .scoring import NODATA_U16
from .species import species_hash
from .weather_grid import ALT_CLASS_M

log = logging.getLogger(__name__)

MIN_ZOOM = 6
MAX_ZOOM = 15
TILE = 256
NODATA = 255
CHUNK_TILES = 64  # carré de tuiles z15 (64 × 64) par tâche parallèle
MERGE_MEM_MB = 1024


def default_jobs() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def _depts_hash(depts: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(depts)).encode()).hexdigest()[:6]


def build(species_id: str, depts: list[str], force: bool = False, jobs: int | None = None) -> Path:
    """`T-<espèce>-<hash espèce>-p<version>-d<hash départements>.pmtiles` : T × 254 en gris."""
    h = species_hash(species_id)
    tag = f"{species_id}-{h}-p{SCORING_VERSION}-d{_depts_hash(depts)}"
    srcs = [T_DIR / species_id / f"{d}-{h}.tif" for d in depts]
    return _build(
        "T",
        tag,
        srcs,
        f"T {species_id}",
        "Champis — potentiel terrain, gris 8 bits = T × 254, alpha 0 = hors forêt",
        force,
        jobs,
    )


def build_alt(depts: list[str], force: bool = False, jobs: int | None = None) -> Path:
    """`A-<largeur>m-d<hash départements>.pmtiles` : indice de classe d'altitude, toutes espèces."""
    tag = f"{ALT_CLASS_M}m-d{_depts_hash(depts)}"
    srcs = [dept_dirs(d)["grid"] / "alt.tif" for d in depts]
    return _build(
        "A",
        tag,
        srcs,
        "A classes d'altitude",
        f"Champis — classe d'altitude, gris 8 bits = indice k de [{ALT_CLASS_M}k, "
        f"{ALT_CLASS_M}(k+1)[ m, alpha 0 = altitude inconnue",
        force,
        jobs,
    )


def _build(
    kind: str,
    tag: str,
    srcs: list[Path],
    name: str,
    description: str,
    force: bool,
    jobs: int | None = None,
) -> Path:
    TILES_DIR.mkdir(parents=True, exist_ok=True)
    out = TILES_DIR / f"{kind}-{tag}.pmtiles"
    if out.exists() and not force:
        return out
    missing = [p for p in srcs if not p.exists()]
    if missing:
        raise FileNotFoundError(f"{kind} manquants : {missing}")

    work = TILES_DIR / "_work" / f"{kind}-{tag}"
    work.mkdir(parents=True, exist_ok=True)
    mosaic = work / f"{kind}_2154.tif"
    mbt = work / f"{kind}.mbtiles"
    (_mosaic if kind == "T" else _mosaic_alt)(srcs, mosaic)
    _mbtiles(mosaic, mbt, name, description, kind, jobs or default_jobs())
    from pmtiles.convert import mbtiles_to_pmtiles

    out.unlink(missing_ok=True)
    mbtiles_to_pmtiles(str(mbt), str(out), MAX_ZOOM)
    log.info("écrit %s", out)
    return out


_MOSAIC_KWDS = {"compress": "DEFLATE", "tiled": True, "blockxsize": 512, "blockysize": 512}


def _mosaic(srcs: list[Path], out: Path) -> None:
    """Mosaïque uint8 écrite directement sur disque (France ≈ 1,6 Gpx : jamais en RAM)."""
    if out.exists():
        return
    tmp = out.with_suffix(".part.tif")
    merge(
        [str(p) for p in srcs],
        nodata=NODATA,
        dtype="uint8",
        dst_path=str(tmp),
        dst_kwds=_MOSAIC_KWDS,
        mem_limit=MERGE_MEM_MB,
    )
    tmp.replace(out)


def _mosaic_alt(srcs: list[Path], out: Path) -> None:
    """alt.tif (uint16 m) → indice de classe uint8 par blocs ; altitude inconnue → NODATA."""
    if out.exists():
        return
    raw = out.with_suffix(".alt16.tif")
    if not raw.exists():
        tmp = raw.with_suffix(".part.tif")
        merge(
            [str(p) for p in srcs],
            nodata=NODATA_U16,
            dtype="uint16",
            dst_path=str(tmp),
            dst_kwds=_MOSAIC_KWDS,
            mem_limit=MERGE_MEM_MB,
        )
        tmp.replace(raw)
    tmp = out.with_suffix(".part.tif")
    with rasterio.open(raw) as src:
        profile = src.profile.copy()
        profile.update(dtype="uint8", nodata=NODATA, **_MOSAIC_KWDS)
        with rasterio.open(tmp, "w", **profile) as dst:
            for _, win in src.block_windows(1):
                alt = src.read(1, window=win)
                cls = np.where(
                    alt == NODATA_U16, NODATA, np.minimum(alt // ALT_CLASS_M, NODATA - 1)
                )
                dst.write(cls.astype(np.uint8), 1, window=win)
    tmp.replace(out)
    raw.unlink()


def alt_class_center(index: int) -> int:
    """Centre (m) de la classe d'indice k, tel que `weather_grid` et l'API le nomment."""
    return index * ALT_CLASS_M + ALT_CLASS_M // 2


def _png(arr: np.ndarray) -> bytes:
    """Gris 8 bits + alpha (0 sur nodata) → PNG."""
    alpha = np.where(arr == NODATA, 0, 255).astype(np.uint8)
    img = Image.fromarray(np.dstack([arr, alpha]), mode="LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def _unpng(data: bytes) -> np.ndarray:
    """PNG LA → tableau uint8 avec NODATA là où alpha = 0 (inverse exact de `_png`)."""
    la = np.asarray(Image.open(io.BytesIO(data)).convert("LA"))
    return np.where(la[..., 1] == 0, NODATA, la[..., 0]).astype(np.uint8)


def reduce_t(children: np.ndarray) -> np.ndarray:
    """512×512 → 256×256 : moyenne des pixels valides de chaque bloc 2×2, NODATA si aucun."""
    b = children.reshape(TILE, 2, TILE, 2).transpose(0, 2, 1, 3).reshape(TILE, TILE, 4)
    valid = b != NODATA
    n = valid.sum(axis=2)
    total = np.where(valid, b, 0).sum(axis=2, dtype=np.int32)
    mean = np.rint(total / np.maximum(n, 1)).astype(np.uint8)
    return np.where(n > 0, mean, NODATA).astype(np.uint8)


def reduce_nearest(children: np.ndarray) -> np.ndarray:
    """512×512 → 256×256 : premier pixel valide du bloc 2×2 (classes : pas de moyenne)."""
    b = children.reshape(TILE, 2, TILE, 2).transpose(0, 2, 1, 3).reshape(TILE, TILE, 4)
    valid = b != NODATA
    first = np.argmax(valid, axis=2)
    out = np.take_along_axis(b, first[..., None], axis=2)[..., 0]
    return np.where(valid.any(axis=2), out, NODATA).astype(np.uint8)


def _render_chunk(
    mosaic: str, z: int, x0: int, x1: int, y0: int, y1: int
) -> list[tuple[int, int, bytes]]:
    """Tuiles (x, y, png) du carré de colonnes [x0, x1[ × lignes [y0, y1[ ; un processus.

    Un carré entièrement hors de la mosaïque ou sans donnée (mer, hors emprise) est écarté par
    une seule lecture de sa fenêtre source, sans passer tuile par tuile.
    """
    with rasterio.open(mosaic) as src:
        bl, tr = mercantile.xy_bounds(x0, y1 - 1, z), mercantile.xy_bounds(x1 - 1, y0, z)
        win = _source_window(src, bl.left, bl.bottom, tr.right, tr.top)
        if win is None or np.all(src.read(1, window=win) == NODATA):
            return []
        out = []
        for x in range(x0, x1):
            for y in range(y0, y1):
                arr = _render_tile(src, mercantile.Tile(x, y, z))
                if arr is not None:
                    out.append((x, y, _png(arr)))
    return out


WINDOW_MARGIN = 1  # pixels source ajoutés autour de la fenêtre d'une tuile


def _source_window(src: rasterio.DatasetReader, left, bottom, right, top) -> Window | None:
    """Fenêtre source couvrant *entièrement* l'emprise Web Mercator donnée, ou None si hors mosaïque.

    Bords arrondis vers l'extérieur (floor / ceil) plus `WINDOW_MARGIN` pixel : un arrondi au plus
    proche perdait la dernière colonne ou ligne de pixels 25 m au bord de la tuile, d'où une bande
    nodata (transparente) le long de chaque couture de tuile. Lire un pixel de trop est sans effet,
    la reprojection ne garde que ce qui tombe dans la tuile.
    """
    sw, ss, se, sn = transform_bounds(CRS_TILES, src.crs, left, bottom, right, top)
    raw = from_bounds(sw, ss, se, sn, src.transform)
    col0 = math.floor(raw.col_off) - WINDOW_MARGIN
    row0 = math.floor(raw.row_off) - WINDOW_MARGIN
    col1 = math.ceil(raw.col_off + raw.width) + WINDOW_MARGIN
    row1 = math.ceil(raw.row_off + raw.height) + WINDOW_MARGIN
    win = Window(col0, row0, col1 - col0, row1 - row0)
    try:
        win = win.intersection(Window(0, 0, src.width, src.height))
    except (ValueError, rasterio.errors.WindowError):  # pas de recouvrement
        return None
    return None if win.width < 1 or win.height < 1 else win


def _render_tile(src: rasterio.DatasetReader, t: mercantile.Tile) -> np.ndarray | None:
    """Lit la fenêtre source couvrant la tuile, la reprojette en 256×256 Web Mercator."""
    b = mercantile.xy_bounds(t)
    win = _source_window(src, b.left, b.bottom, b.right, b.top)
    if win is None:
        return None
    data = src.read(1, window=win)
    if np.all(data == NODATA):
        return None
    dst = np.full((TILE, TILE), NODATA, dtype=np.uint8)
    reproject(
        data,
        dst,
        src_transform=src.window_transform(win),
        src_crs=src.crs,
        dst_transform=transform_from_bounds(b.left, b.bottom, b.right, b.top, TILE, TILE),
        dst_crs=CRS_TILES,
        resampling=Resampling.nearest,
        src_nodata=NODATA,
        dst_nodata=NODATA,
    )
    return None if np.all(dst == NODATA) else dst


def _mbtiles(src_path: Path, out: Path, name: str, description: str, kind: str, jobs: int) -> None:
    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.executescript(
        """
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
        """
    )
    with rasterio.open(src_path) as src:
        w, south, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    db.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [
            ("name", name),
            ("format", "png"),
            ("type", "overlay"),
            ("version", "1"),
            ("minzoom", str(MIN_ZOOM)),
            ("maxzoom", str(MAX_ZOOM)),
            ("bounds", f"{w},{south},{e},{n}"),
            ("description", description),
        ],
    )
    count = _render_max_zoom(db, src_path, (w, south, e, n), jobs)
    log.info("z%d : %d tuiles", MAX_ZOOM, count)
    reduce = reduce_t if kind == "T" else reduce_nearest
    for z in range(MAX_ZOOM - 1, MIN_ZOOM - 1, -1):
        log.info("z%d : %d tuiles", z, _reduce_zoom(db, z, reduce))
    db.close()


def _insert(db: sqlite3.Connection, z: int, rows: list[tuple[int, int, bytes]]) -> None:
    db.executemany(
        "INSERT INTO tiles VALUES (?, ?, ?, ?)",
        [(z, x, (2**z - 1) - y, png) for x, y, png in rows],  # schéma TMS : ligne inversée
    )
    db.commit()


def _render_max_zoom(db: sqlite3.Connection, src_path: Path, bounds, jobs: int) -> int:
    """Niveau 15 depuis la mosaïque, par carrés de CHUNK_TILES² tuiles, `jobs` processus."""
    w, south, e, n = bounds
    ul, lr = mercantile.tile(w, n, MAX_ZOOM), mercantile.tile(e, south, MAX_ZOOM)
    chunks = [
        (
            str(src_path),
            MAX_ZOOM,
            x0,
            min(x0 + CHUNK_TILES, lr.x + 1),
            y0,
            min(y0 + CHUNK_TILES, lr.y + 1),
        )
        for x0 in range(ul.x, lr.x + 1, CHUNK_TILES)
        for y0 in range(ul.y, lr.y + 1, CHUNK_TILES)
    ]
    count = 0
    if jobs <= 1:
        for chunk in tqdm(chunks, desc=f"z{MAX_ZOOM}", unit="carré", leave=False):
            rows = _render_chunk(*chunk)
            _insert(db, MAX_ZOOM, rows)
            count += len(rows)
        return count
    ctx = multiprocessing.get_context("spawn")  # pas de fork sous GDAL multi-threadé
    with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as ex:
        for rows in tqdm(
            ex.map(_render_chunk, *zip(*chunks, strict=True)),
            total=len(chunks),
            desc=f"z{MAX_ZOOM}",
            unit="carré",
            leave=False,
        ):
            _insert(db, MAX_ZOOM, rows)
            count += len(rows)
    return count


def _reduce_zoom(db: sqlite3.Connection, z: int, reduce) -> int:
    """Niveau z à partir du niveau z+1 déjà en base : 4 enfants → 1 parent."""
    child_z = z + 1
    parents = db.execute(
        "SELECT DISTINCT tile_column / 2, tile_row / 2 FROM tiles WHERE zoom_level = ?",
        (child_z,),
    ).fetchall()
    rows: list[tuple[int, int, bytes]] = []
    count = 0
    for px, prow in tqdm(parents, desc=f"z{z}", unit="tuile", leave=False):
        children = np.full((2 * TILE, 2 * TILE), NODATA, dtype=np.uint8)
        for cx, crow, png in db.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ? "
            "AND tile_column IN (?, ?) AND tile_row IN (?, ?)",
            (child_z, 2 * px, 2 * px + 1, 2 * prow, 2 * prow + 1),
        ):
            i, j = cx - 2 * px, crow - 2 * prow  # j = 0 : ligne TMS basse = moitié sud
            r0 = (1 - j) * TILE
            children[r0 : r0 + TILE, i * TILE : (i + 1) * TILE] = _unpng(png)
        arr = reduce(children)
        if np.all(arr == NODATA):
            continue
        rows.append((px, (2**z - 1) - prow, _png(arr)))  # (x, y XYZ) : _insert remet en TMS
        if len(rows) >= 2000:
            _insert(db, z, rows)
            count += len(rows)
            rows = []
    _insert(db, z, rows)
    return count + len(rows)
