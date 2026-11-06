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

Troisième couche, un fichier par groupe d'affichage : `G` = PNG RGBA dont le canal i porte
T × 254 de la i-ème espèce du groupe (ordre alphabétique des identifiants), 255 = sans donnée
pour ce canal ; l'alpha est un canal de données, jamais une transparence. Construit en empilant
les tuiles des MBTiles de travail des jeux T, sans rien re-rendre ni réduire (spec : Points
ouverts › PMTiles par groupe, 4 nov. 2026) : deux requêtes par tuile côté clients au lieu de cinq.

La version du rendu (`RENDER_VERSION`) entre dans l'empreinte `d<…>` du nom (un correctif du
rendu = un nouveau jeu, voir `_depts_hash`) et dans les métadonnées (`champis_render`) : un fichier
local d'une version antérieure est refait sans `--force` (`_done`), les extraits par département et
les G héritent de la version du jeu complet, `status` signale les anciens jeux encore sur disque.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import math
import multiprocessing
import os
import shutil
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
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
from .species import GROUP_CHANNELS, group_hash, species_hash
from .weather_grid import ALT_CLASS_M

log = logging.getLogger(__name__)

MIN_ZOOM = 6
MAX_ZOOM = 15
TILE = 256
NODATA = 255
# Version du rendu : entre dans l'empreinte `d<…>` du nom (`_depts_hash`, donc un nouveau jeu à
# chaque correctif) et dans les métadonnées (`champis_render`) de chaque PMTiles, comparée par
# `_done`. À incrémenter à chaque correctif qui change les pixels sans changer l'espèce, le
# scoring ni les départements.
#   1 : fenêtre source arrondie au plus proche (sans clé) : perdait une colonne ou une ligne de
#       25 m au bord des tuiles, d'où des traits transparents en « L » aux coins (la grille
#       Lambert-93 est tournée de ~2° par rapport au Web Mercator dans l'Est de la France).
#   2 : fenêtre source couvrante (floor / ceil + marge), 30 oct. 2026.
RENDER_VERSION = 2
RENDER_KEY = "champis_render"
CHUNK_TILES = 64  # carré de tuiles z15 (64 × 64) par tâche parallèle
MERGE_MEM_MB = 1024
GROUP_BATCH = 500  # tuiles G par tâche parallèle (quelques dizaines de Mo de PNG RGBA par lot)
T_DESCRIPTION = "Champis — potentiel terrain, gris 8 bits = T × 254, alpha 0 = hors forêt"


def default_jobs() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def _depts_hash(depts: list[str]) -> str:
    """Empreinte `d<…>` du jeu : les départements *et* la version du rendu (`RENDER_VERSION`).

    Les clients (site, API, app) traitent `depts_hash` comme un identifiant opaque et tout le
    système suppose un nom de PMTiles immuable (`_done`, `publish` par taille, cache disque du
    relais, `immutable` côté navigateurs, cache de l'app par nom). Un correctif du rendu change
    donc l'empreinte : nouveau nom, nouveau jeu, l'ancien est écarté par `publish --prune` et plus
    rien ne peut servir d'anciens octets sous le nouveau nom (décision du 5 nov. 2026).
    """
    key = ",".join(sorted(depts)) + f"|r{RENDER_VERSION}"
    return hashlib.sha256(key.encode()).hexdigest()[:6]


def t_tag(species_id: str, depts: list[str]) -> str:
    """`<espèce>-<hash espèce>-p<version>-d<hash départements>` : le nom du jeu T sans `T-` ni suffixe."""
    return f"{species_id}-{species_hash(species_id)}-p{SCORING_VERSION}-d{_depts_hash(depts)}"


def group_tag(group_id: str, species_ids: list[str], depts: list[str], part: int = 1) -> str:
    """`<groupe>-<hash>-p<version>-d<hash départements>[-<partie>]`, `<hash>` = empreinte des hashes
    d'espèces dans l'ordre des canaux (`species.group_hash`) ; `-2` pour une seconde partie."""
    tag = f"{group_id}-{group_hash(species_ids)}-p{SCORING_VERSION}-d{_depts_hash(depts)}"
    return tag if part == 1 else f"{tag}-{part}"


def build(species_id: str, depts: list[str], force: bool = False, jobs: int | None = None) -> Path:
    """`T-<espèce>-<hash espèce>-p<version>-d<hash départements>.pmtiles` : T × 254 en gris."""
    h = species_hash(species_id)
    srcs = [T_DIR / species_id / f"{d}-{h}.tif" for d in depts]
    return _build(
        "T", t_tag(species_id, depts), srcs, f"T {species_id}", T_DESCRIPTION, force, jobs
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
    if _done(out, force):
        return out
    missing = [p for p in srcs if not p.exists()]
    if missing:
        raise FileNotFoundError(_missing_message(kind, missing, len(srcs)))

    work = TILES_DIR / "_work" / f"{kind}-{tag}"
    work.mkdir(parents=True, exist_ok=True)
    mosaic = work / f"{kind}_2154.tif"
    mbt = work / f"{kind}.mbtiles"
    (_mosaic if kind == "T" else _mosaic_alt)(srcs, mosaic)
    _mbtiles(mosaic, mbt, name, description, kind, jobs or default_jobs())
    _write_pmtiles(mbt, out)
    log.info("écrit %s", out)
    return out


def _dept_list(depts: list[str], shown: int = 10) -> str:
    head = " ".join(depts[:shown])
    return head if len(depts) <= shown else f"{head} … (+{len(depts) - shown})"


def _missing_message(kind: str, missing: list[Path], total: int) -> str:
    """Une ligne (seule la première ligne d'une exception atteint la ligne de l'étape) : quels
    départements, pourquoi le plus souvent, et la commande qui répare."""
    remedy = "relance `champis-pipeline batch` sur les mêmes départements"
    if kind != "T":
        depts = sorted(p.parent.name for p in missing)
        return (
            f"{missing[0].name} manquant pour {len(depts)}/{total} département(s) "
            f"({_dept_list(depts)}) : {remedy}"
        )
    folder, h = missing[0].parent, missing[0].stem.rsplit("-", 1)[-1]
    depts = sorted(p.stem.rsplit("-", 1)[0] for p in missing)
    stale = [d for d in depts if any(folder.glob(f"{d}-*.tif"))]
    why = (
        f" ; {len(stale)} ont un T sous un autre hash : le fichier espèce a changé depuis le "
        "dernier terrain"
        if stale
        else ""
    )
    return (
        f"T {folder.name} manquant sous le hash {h} pour {len(depts)}/{total} département(s) "
        f"({_dept_list(depts)}){why} ; {remedy} (seuls les T manquants sont recalculés)"
    )


def _pmtiles_info(path: Path) -> tuple[int, int] | None:
    """(tuiles adressées, version du rendu) d'un PMTiles, `None` s'il est illisible (vide,
    tronqué). Sans clé `champis_render` dans les métadonnées, le rendu est la version 1."""
    from pmtiles.reader import MmapSource, Reader
    from pmtiles.tile import MagicNumberNotFound, SpecVersionUnsupported

    try:
        with open(path, "rb") as f:
            reader = Reader(MmapSource(f))
            count = reader.header()["addressed_tiles_count"]
            render = int(reader.metadata().get(RENDER_KEY, 1))
    except (
        OSError,
        ValueError,
        IndexError,
        KeyError,
        TypeError,
        MagicNumberNotFound,
        SpecVersionUnsupported,
    ):
        return None
    return count, render


def _pmtiles_count(path: Path) -> int | None:
    """Tuiles adressées par un PMTiles, `None` s'il est illisible (vide, tronqué)."""
    info = _pmtiles_info(path)
    return None if info is None else info[0]


def render_version(path: Path) -> int | None:
    """Version du rendu (`RENDER_VERSION`) qui a produit ce PMTiles ; `None` s'il est illisible."""
    info = _pmtiles_info(path)
    return None if info is None or not info[0] else info[1]


def _done(out: Path, force: bool) -> bool:
    """`out` est-il un PMTiles fini qu'on peut sauter ? Un fichier vide ou illisible (laissé par un
    plantage d'une version antérieure, qui écrivait en place) est refait, avec un avertissement ;
    un fichier rendu par une version antérieure de ce module (`RENDER_VERSION`) aussi : c'est ce
    qui garantit qu'un correctif du rendu finit dans le bucket sans qu'on pense à `--force`."""
    if force or not out.exists():
        return False
    info = _pmtiles_info(out)
    if info is None or not info[0]:
        log.warning("%s illisible ou vide (écriture interrompue) : refait", out.name)
    elif info[1] < RENDER_VERSION:
        log.warning(
            "%s rendu par le tuilage v%d (traits transparents aux bords des tuiles) : refait en v%d",
            out.name,
            info[1],
            RENDER_VERSION,
        )
    else:
        return True
    out.unlink()
    return False


def _write_pmtiles(mbt: Path, out: Path) -> None:
    """MBTiles → PMTiles par un fichier `.part` renommé à la fin : un plantage (disque plein, jeu
    vide) ne laisse jamais un `out` que le passage suivant prendrait pour « déjà fait »."""
    from pmtiles.convert import mbtiles_to_pmtiles

    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)
    try:
        mbtiles_to_pmtiles(str(mbt), str(part), MAX_ZOOM)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


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


def _png_rgba(arr: np.ndarray) -> bytes:
    """(256, 256, 4) uint8 → PNG RGBA 8 bits non entrelacé ; chaque canal est une donnée (255 =
    nodata du canal), l'alpha compris : rien n'est prémultiplié ni rendu transparent ici."""
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def _unpng_rgba(data: bytes) -> np.ndarray:
    """PNG RGBA → (256, 256, 4) uint8, octets tels quels (inverse exact de `_png_rgba`)."""
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA")).astype(np.uint8)


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
    db.executescript(MBTILES_SCHEMA)
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
            (RENDER_KEY, str(RENDER_VERSION)),
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


# ---------------------------------------------------------------- jeux par département

MBTILES_SCHEMA = """
CREATE TABLE metadata (name TEXT, value TEXT);
CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
"""


def work_mbtiles(kind: str, tag: str) -> Path:
    """MBTiles de travail laissé par `_build` (`data/tiles/_work/<kind>-<tag>/<kind>.mbtiles`)."""
    return TILES_DIR / "_work" / f"{kind}-{tag}" / f"{kind}.mbtiles"


def _tile_count(mbtiles: Path) -> int | None:
    """Nombre de tuiles d'un MBTiles, `None` s'il est illisible (rendu interrompu, fichier tronqué)."""
    try:
        db = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True)
        try:
            return db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        finally:
            db.close()
    except sqlite3.DatabaseError:
        return None


def full_set_source(kind: str, tag: str) -> Path | None:
    """MBTiles de travail du jeu complet `<kind>-<tag>`, prêt pour `extract`, ou `None` si le jeu
    complet n'a pas de PMTiles. Le PMTiles n'est écrit qu'à la fin d'un rendu réussi : c'est lui qui
    fait foi. Un MBTiles de travail absent, illisible ou qui n'a pas autant de tuiles que lui (rendu
    interrompu puis repris par un autre chemin, `_work` nettoyé) est reconstruit depuis le PMTiles,
    sans rien re-rendre."""
    pm = TILES_DIR / f"{kind}-{tag}.pmtiles"
    expected = _pmtiles_count(pm) if pm.exists() else None
    if not expected:
        return None
    mbt = work_mbtiles(kind, tag)
    if mbt.exists() and _tile_count(mbt) == expected:
        return mbt
    log.warning(
        "MBTiles de travail %s absent ou incomplet (%d tuiles sur %d) : reconstruit depuis %s",
        mbt,
        (_tile_count(mbt) or 0) if mbt.exists() else 0,
        expected,
        pm.name,
    )
    from pmtiles.convert import pmtiles_to_mbtiles

    mbt.parent.mkdir(parents=True, exist_ok=True)
    mbt.unlink(missing_ok=True)
    # le MBTiles ne déduplique pas les tuiles : compter large avant de lancer des minutes d'écriture
    need, free = 2 * pm.stat().st_size, shutil.disk_usage(mbt.parent).free
    if free < need:
        raise OSError(
            f"espace disque insuffisant pour reconstruire {mbt} : {free / 1e9:.1f} Go libres, "
            f"~{need / 1e9:.1f} Go nécessaires"
        )
    part = mbt.with_name(mbt.name + ".part")
    part.unlink(missing_ok=True)
    try:
        pmtiles_to_mbtiles(str(pm), str(part))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, mbt)
    return mbt


def dept_bounds(
    kind: str, dept: str, species_id: str | None = None
) -> tuple[float, float, float, float]:
    """Emprise WGS84 (ouest, sud, est, nord) du raster source du département : T de l'espèce
    (`kind = "T"`) ou classes d'altitude (`kind = "A"`)."""
    if kind == "T":
        if species_id is None:
            raise ValueError("species_id requis pour l'emprise T")
        src = T_DIR / species_id / f"{dept}-{species_hash(species_id)}.tif"
    else:
        src = dept_dirs(dept)["grid"] / "alt.tif"
    with rasterio.open(src) as s:
        return transform_bounds(s.crs, "EPSG:4326", *s.bounds)


def extract(src_mbtiles: Path, out: Path, bounds, name: str, description: str) -> int:
    """Copie dans `out` (PMTiles) les tuiles de `src_mbtiles` dont l'emprise touche `bounds`, à tous
    les niveaux, sans rien re-rendre : c'est ainsi qu'un jeu par département sort du jeu France en
    quelques secondes (décision du 31 oct. 2026). Les tuiles de bord gardent les pixels des voisins,
    qui sont exacts. Renvoie le nombre de tuiles copiées."""
    w, s, e, n = bounds
    work = TILES_DIR / "_work" / out.stem
    work.mkdir(parents=True, exist_ok=True)
    kind = out.stem.split("-")[0]
    mbt = work / f"{kind}.mbtiles"
    mbt.unlink(missing_ok=True)
    db = sqlite3.connect(mbt)
    db.executescript(MBTILES_SCHEMA)
    db.execute("ATTACH DATABASE ? AS src", (str(src_mbtiles),))
    meta = dict(db.execute("SELECT name, value FROM src.metadata").fetchall())
    meta.update({"name": name, "description": description, "bounds": f"{w},{s},{e},{n}"})
    db.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
    count = 0
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        ul = mercantile.tile(w, n, z)
        lr = mercantile.tile(e, s, z)
        rows = ((2**z - 1) - lr.y, (2**z - 1) - ul.y)  # schéma TMS : ligne inversée
        cur = db.execute(
            "INSERT INTO tiles SELECT zoom_level, tile_column, tile_row, tile_data FROM src.tiles "
            "WHERE zoom_level = ? AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?",
            (z, ul.x, lr.x, rows[0], rows[1]),
        )
        count += cur.rowcount
    db.commit()
    db.execute("DETACH DATABASE src")
    db.close()
    try:
        if count == 0:
            # pmtiles ne sait pas écrire un jeu vide (IndexError) : dire plutôt d'où vient le vide
            raise ValueError(
                f"aucune tuile de {src_mbtiles} dans l'emprise {w:.3f},{s:.3f},{e:.3f},{n:.3f} "
                f"pour {out.name}"
            )
        _write_pmtiles(mbt, out)
    finally:
        # ce MBTiles ne sert qu'à produire `out` : le garder doublerait le disque des jeux par
        # département (96 × 8 fichiers pour la France)
        mbt.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            work.rmdir()
    return count


def build_dept(
    species_id: str, dept: str, all_depts: list[str], force: bool = False, jobs: int | None = None
) -> Path:
    """Jeu d'un seul département pour une espèce, pour le hors ligne de l'app : extrait du MBTiles
    de travail du jeu `all_depts` (vérifié contre son PMTiles, voir `full_set_source`) quand ce jeu
    existe, sinon rendu complet du département."""
    out = TILES_DIR / f"T-{t_tag(species_id, [dept])}.pmtiles"
    if _done(out, force):
        return out
    src = full_set_source("T", t_tag(species_id, all_depts))
    if src is None:
        return build(species_id, [dept], force, jobs)
    n = extract(
        src, out, dept_bounds("T", dept, species_id), f"T {species_id} {dept}", T_DESCRIPTION
    )
    log.info("écrit %s (%d tuiles extraites)", out, n)
    return out


def build_alt_dept(
    dept: str, all_depts: list[str], force: bool = False, jobs: int | None = None
) -> Path:
    """Couche A d'un seul département (voir `build_dept`)."""
    tag = f"{ALT_CLASS_M}m-d{_depts_hash([dept])}"
    out = TILES_DIR / f"A-{tag}.pmtiles"
    if _done(out, force):
        return out
    src = full_set_source("A", f"{ALT_CLASS_M}m-d{_depts_hash(all_depts)}")
    if src is None:
        return build_alt([dept], force, jobs)
    n = extract(
        src,
        out,
        dept_bounds("A", dept),
        f"A classes d'altitude {dept}",
        f"Champis — classe d'altitude, gris 8 bits = indice k de [{ALT_CLASS_M}k, "
        f"{ALT_CLASS_M}(k+1)[ m, alpha 0 = altitude inconnue",
    )
    log.info("écrit %s (%d tuiles extraites)", out, n)
    return out


# ---------------------------------------------------------------- jeux par groupe (G)


def _group_description(species_ids: list[str]) -> str:
    channels = ", ".join(f"{c} = {sid}" for c, sid in zip("RGBA", species_ids, strict=False))
    return (
        f"Champis — potentiel terrain par groupe, PNG RGBA, canal = T × 254 ({channels}), "
        f"255 = sans donnée pour le canal"
    )


def build_group(
    group_id: str,
    species_ids: list[str],
    depts: list[str],
    part: int = 1,
    force: bool = False,
    jobs: int | None = None,
) -> Path:
    """`G-<groupe>-<hash>-p<version>-d<hash départements>.pmtiles` : PNG RGBA, canal i = T × 254 de
    la i-ème espèce de `species_ids` (ordre des canaux = ordre alphabétique, `species.group_sets`),
    255 là où l'espèce n'a pas de donnée ou pas de tuile ; les canaux au-delà des espèces sont
    tout à 255. Empile les tuiles des MBTiles de travail des jeux T (`full_set_source`) : rien n'est
    re-rendu depuis les mosaïques ni réduit, les tuiles T le sont déjà à tous les niveaux."""
    if not 1 <= len(species_ids) <= GROUP_CHANNELS:
        raise ValueError(f"un fichier G porte 1 à {GROUP_CHANNELS} espèces, pas {species_ids}")
    TILES_DIR.mkdir(parents=True, exist_ok=True)
    tag = group_tag(group_id, species_ids, depts, part)
    out = TILES_DIR / f"G-{tag}.pmtiles"
    if _done(out, force):
        return out
    srcs = []
    for sid in species_ids:
        src = full_set_source("T", t_tag(sid, depts))
        if src is None:
            raise FileNotFoundError(
                f"jeu T de {sid} absent pour ces départements (T-{t_tag(sid, depts)}.pmtiles) : "
                "`tiles` le produit avant les groupes"
            )
        srcs.append(src)
    work = TILES_DIR / "_work" / f"G-{tag}"
    work.mkdir(parents=True, exist_ok=True)
    mbt = work / "G.mbtiles"
    _group_mbtiles(
        srcs, mbt, f"G {group_id}", _group_description(species_ids), jobs or default_jobs()
    )
    _write_pmtiles(mbt, out)
    log.info("écrit %s", out)
    return out


def _union_bounds(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    """Emprise (ouest, sud, est, nord) réunissant plusieurs emprises."""
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _group_mbtiles(
    srcs: list[Path], out: Path, name: str, description: str, jobs: int
) -> dict[int, int]:
    """MBTiles G depuis les MBTiles T `srcs` (un par canal) : pour chaque (z, x, y) présent dans
    au moins un jeu, un PNG RGBA empilant les canaux. Renvoie le nombre de tuiles par niveau."""
    out.unlink(missing_ok=True)
    paths = [str(p) for p in srcs]
    # l'union des clés se lit sur une connexion à part de celle qui insère, en lecture seule
    keys = sqlite3.connect("file::memory:", uri=True)
    boxes = []
    for i, p in enumerate(paths):
        keys.execute(f"ATTACH DATABASE ? AS s{i}", (f"file:{p}?mode=ro",))
        value = keys.execute(f"SELECT value FROM s{i}.metadata WHERE name = 'bounds'").fetchone()
        boxes.append(tuple(float(v) for v in value[0].split(",")))
    w, south, e, n = _union_bounds(boxes)
    db = sqlite3.connect(out)
    db.executescript(MBTILES_SCHEMA)
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
            (RENDER_KEY, str(RENDER_VERSION)),
        ],
    )
    db.commit()
    union = " UNION ".join(
        f"SELECT tile_column, tile_row FROM s{i}.tiles WHERE zoom_level = ?"
        for i in range(len(paths))
    )
    counts: dict[int, int] = {}
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        cur = keys.execute(union, (z,) * len(paths))
        counts[z] = _stack_zoom(db, z, cur, paths, jobs)
        log.info("z%d : %d tuiles", z, counts[z])
    keys.close()
    db.close()
    return counts


def _insert_tms(db: sqlite3.Connection, z: int, rows: list[tuple[int, int, bytes]]) -> None:
    """Comme `_insert`, mais les lignes sont déjà en TMS (lues telles quelles dans les jeux T)."""
    db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [(z, c, r, png) for c, r, png in rows])
    db.commit()


def _stack_zoom(db: sqlite3.Connection, z: int, cur, paths: list[str], jobs: int) -> int:
    """Niveau z : lots de `GROUP_BATCH` clés (TMS) empilés en PNG RGBA par `jobs` processus, au plus
    2 × jobs lots en vol pour borner la mémoire (France ≈ 1 M de tuiles par groupe)."""
    count = 0
    bar = tqdm(desc=f"z{z}", unit="tuile", leave=False)
    if jobs <= 1:
        _group_worker_init(paths)
        try:
            while batch := cur.fetchmany(GROUP_BATCH):
                rows = _stack_batch(z, batch)
                _insert_tms(db, z, rows)
                count += len(rows)
                bar.update(len(rows))
        finally:
            _group_worker_close()
            bar.close()
        return count
    ctx = multiprocessing.get_context("spawn")  # pas de fork sous GDAL multi-threadé
    with (
        ProcessPoolExecutor(
            max_workers=jobs, mp_context=ctx, initializer=_group_worker_init, initargs=(paths,)
        ) as ex,
        bar,
    ):
        pending = set()
        while True:
            batch = cur.fetchmany(GROUP_BATCH)
            if batch:
                pending.add(ex.submit(_stack_batch, z, batch))
                if len(pending) < 2 * jobs:
                    continue
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                rows = f.result()
                _insert_tms(db, z, rows)
                count += len(rows)
                bar.update(len(rows))
    return count


_GROUP_SRC: list[sqlite3.Connection] = []  # jeux T ouverts par le processus (un par canal)


def _group_worker_init(paths: list[str]) -> None:
    global _GROUP_SRC
    _GROUP_SRC = [sqlite3.connect(f"file:{p}?mode=ro", uri=True) for p in paths]


def _group_worker_close() -> None:
    global _GROUP_SRC
    for db in _GROUP_SRC:
        db.close()
    _GROUP_SRC = []


def _stack_batch(z: int, keys: list[tuple[int, int]]) -> list[tuple[int, int, bytes]]:
    """(colonne, ligne TMS) → PNG RGBA : canal i = tuile T du i-ème jeu, 255 s'il ne l'a pas."""
    out = []
    for col, row in keys:
        arr = np.full((TILE, TILE, GROUP_CHANNELS), NODATA, dtype=np.uint8)
        for i, db in enumerate(_GROUP_SRC):
            found = db.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? "
                "AND tile_row = ?",
                (z, col, row),
            ).fetchone()
            if found is not None:
                arr[..., i] = _unpng(found[0])
        out.append((col, row, _png_rgba(arr)))
    return out


def group_dept_bounds(dept: str, species_ids: list[str]) -> tuple[float, float, float, float]:
    """Emprise WGS84 du département pour un G : réunion des emprises T de ses espèces."""
    return _union_bounds([dept_bounds("T", dept, sid) for sid in species_ids])


def build_group_dept(
    group_id: str,
    species_ids: list[str],
    dept: str,
    all_depts: list[str],
    part: int = 1,
    force: bool = False,
    jobs: int | None = None,
) -> Path:
    """G d'un seul département (voir `build_dept`) : extrait du MBTiles de travail du G complet
    quand il existe, sinon construit depuis les jeux T du département."""
    out = TILES_DIR / f"G-{group_tag(group_id, species_ids, [dept], part)}.pmtiles"
    if _done(out, force):
        return out
    src = full_set_source("G", group_tag(group_id, species_ids, all_depts, part))
    if src is None:
        return build_group(group_id, species_ids, [dept], part, force, jobs)
    n = extract(
        src,
        out,
        group_dept_bounds(dept, species_ids),
        f"G {group_id} {dept}",
        _group_description(species_ids),
    )
    log.info("écrit %s (%d tuiles extraites)", out, n)
    return out
