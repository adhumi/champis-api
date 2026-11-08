"""Carte HD de la France du potentiel toute saison d'une espèce (spec › Backoffice › Carte HD,
tranché le 8 nov. 2026 : fond blanc et limites des départements).

Le potentiel toute saison, c'est T : la carte est rendue depuis les PMTiles T du jeu France
publiés (manifeste du bucket, entrée `kind: T` sans `dept`, le jeu qui couvre le plus de
départements), lus dans le bucket par plages avec le lecteur de la bibliothèque `pmtiles`
(en-tête, répertoires, une tuile à la fois), sans télécharger le fichier. Valeur du pixel =
`T × 254` du PNG gris, la même que le calque « toute saison » de la carte, à la résolution de la
pyramide au niveau demandé (9 : ≈ 5 600 × 5 600 px ; 10 : ≈ 11 300 × 11 300 px). Couleur par le
contrat palette v2 (`palette_color` : violet, alpha = T) composée sur blanc ; limites des
départements en trait fin (`departements.geojson` du site, le fichier que l'app lit déjà) ;
cartouche en bas (noms, « potentiel toute saison », date du jeu, empreinte, crédits, licence).
Résultat : PNG dans le bucket sous `renders/T-<espèce>-<hash>-z<niveau>.png`, nom immuable
(nouveau jeu ou espèce modifiée = autre nom) ; déjà présent = rien à faire sans `force`.
"""

from __future__ import annotations

import io
import math
import re
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import mercantile
import numpy as np
from champis_pipeline.species import load_palette, load_species
from champis_pipeline.storage import MANIFEST_KEY, TILES_PREFIX, BucketLike
from PIL import Image, ImageDraw, ImageFont
from pmtiles.reader import Reader

from .scoring import palette_color

# Mêmes constantes que `champis_pipeline.tiles`, recopiées : ce module tourne dans l'API, dont
# l'image n'a pas les bibliothèques système de rasterio (importer `tiles` ferait tomber l'API au
# démarrage, constaté le 8 nov. 2026) ; `test_render.py` vérifie qu'elles restent égales.
NODATA = 255
RENDERS_PREFIX = "renders/"
FRANCE_BOUNDS = (-5.4, 41.2, 9.8, 51.2)  # lon/lat, France métropolitaine (Corse comprise)
TILE = 256
T_SCALE = 254  # T × 254 dans le PNG gris (255 = sans donnée)
BOUNDARY_RGB = (96, 96, 96)
CREDITS = "BD Forêt V2 © IGN · Copernicus DEM © ESA/Airbus · SoilGrids © ISRIC · données Champis CC BY 4.0"
_NAME = re.compile(r"^renders/T-(?P<species>[a-z_]+)-(?P<hash>[0-9a-f]+)-z(?P<zoom>\d+)\.png$")

Progress = Callable[[int, int], None]


def render_key(species_id: str, species_hash: str, zoom: int) -> str:
    return f"{RENDERS_PREFIX}T-{species_id}-{species_hash}-z{zoom}.png"


def parse_render_key(key: str) -> dict[str, Any] | None:
    m = _NAME.match(key)
    if not m:
        return None
    return {"species": m["species"], "hash": m["hash"], "zoom": int(m["zoom"]), "key": key}


def list_renders(bucket: BucketLike) -> list[dict[str, Any]]:
    """Les cartes présentes dans le bucket, avec leur taille."""
    out = []
    for key, size in sorted(bucket.list(RENDERS_PREFIX).items()):
        info = parse_render_key(key)
        if info:
            out.append({**info, "bytes": size})
    return out


def france_entry(manifest: dict[str, Any], species_id: str) -> dict[str, Any] | None:
    """L'entrée T du jeu le plus couvrant pour l'espèce, sans les jeux par département."""
    depts = manifest.get("depts_by_hash", {})
    best = None
    for t in manifest.get("tiles", []):
        if t.get("kind", "T") != "T" or t.get("species") != species_id or t.get("dept"):
            continue
        n = len(depts.get(t.get("depts_hash", ""), []))
        if best is None or n > best[0]:
            best = (n, t)
    return best[1] if best else None


def unpng(data: bytes) -> np.ndarray:
    """PNG LA du pipeline → tableau uint8 avec NODATA là où alpha = 0 (inverse de `tiles._png`)."""
    la = np.asarray(Image.open(io.BytesIO(data)).convert("LA"))
    return np.where(la[..., 1] == 0, NODATA, la[..., 0]).astype(np.uint8)


class RangeSource:
    """`get_bytes(offset, length)` du lecteur PMTiles sur le bucket, avec un petit cache : les
    répertoires sont relus à chaque tuile par le lecteur, les tuiles elles-mêmes une seule fois."""

    def __init__(self, bucket: BucketLike, key: str, cache_entries: int = 256) -> None:
        self.bucket = bucket
        self.key = key
        self.cache: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self.cache_entries = cache_entries
        self.requests = 0

    def __call__(self, offset: int, length: int) -> bytes:
        k = (offset, length)
        hit = self.cache.get(k)
        if hit is not None:
            self.cache.move_to_end(k)
            return hit
        self.requests += 1
        data = self.bucket.get_range(self.key, offset, length)
        self.cache[k] = data
        if len(self.cache) > self.cache_entries:
            self.cache.popitem(last=False)
        return data


def tile_grid(zoom: int, bounds=FRANCE_BOUNDS) -> tuple[range, range]:
    """Colonnes et lignes de tuiles qui couvrent l'emprise au niveau donné."""
    west, south, east, north = bounds
    ul = mercantile.tile(west, north, zoom)
    lr = mercantile.tile(east, south, zoom)
    return range(ul.x, lr.x + 1), range(ul.y, lr.y + 1)


def project(lon: float, lat: float, zoom: int, x0: int, y0: int) -> tuple[float, float]:
    """Pixel (Mercator Web, tuiles de 256 px) relatif à la tuile (x0, y0)."""
    n = 2**zoom * TILE
    x = (lon + 180.0) / 360.0 * n - x0 * TILE
    lat_r = math.radians(max(min(lat, 85.05), -85.05))
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n - y0 * TILE
    return x, y


def read_canvas(
    get_bytes: Callable[[int, int], bytes], zoom: int, progress: Progress | None = None
) -> tuple[np.ndarray, int, int, int]:
    """Le T brut de toute l'emprise : tableau uint8 (255 = sans donnée), et le coin (x0, y0)."""
    reader = Reader(get_bytes)
    xs, ys = tile_grid(zoom)
    canvas = np.full((len(ys) * TILE, len(xs) * TILE), NODATA, dtype=np.uint8)
    total = len(xs) * len(ys)
    done = found = 0
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            data = reader.get(zoom, x, y)
            if data:
                canvas[j * TILE : (j + 1) * TILE, i * TILE : (i + 1) * TILE] = unpng(data)
                found += 1
            done += 1
            if progress and (done % 16 == 0 or done == total):
                progress(done, total)
    return canvas, xs.start, ys.start, found


def colorize(canvas: np.ndarray, palette: dict[str, Any]) -> Image.Image:
    """Violet dont l'alpha est T (contrat palette v2), composé sur blanc ; sans donnée = blanc."""
    lut = np.full((256, 3), 255, dtype=np.uint8)
    for v in range(T_SCALE + 1):
        r, g, b, a = palette_color(v / T_SCALE, palette)
        lut[v] = [round(255 + (c - 255) * a / 255) for c in (r, g, b)]
    return Image.fromarray(lut[canvas], mode="RGB")


def _rings(geometry: dict[str, Any]):
    if geometry["type"] == "Polygon":
        yield from geometry["coordinates"]
    elif geometry["type"] == "MultiPolygon":
        for polygon in geometry["coordinates"]:
            yield from polygon


def draw_boundaries(
    img: Image.Image, features: list[dict[str, Any]], zoom: int, x0: int, y0: int
) -> None:
    draw = ImageDraw.Draw(img)
    width = max(1, round(img.width / 5000))
    for feature in features:
        for ring in _rings(feature["geometry"]):
            points = [project(lon, lat, zoom, x0, y0) for lon, lat in ring]
            if len(points) > 1:
                draw.line(points, fill=BOUNDARY_RGB, width=width)


def with_cartouche(img: Image.Image, lines: list[str]) -> Image.Image:
    """Bande blanche sous la carte : titre en gras, puis les lignes de détail."""
    band = max(120, img.height // 30)
    size = max(14, band // 6)
    try:
        bold = ImageFont.load_default(size=int(size * 1.4))
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow trop ancien pour `size`
        bold = font = ImageFont.load_default()
    out = Image.new("RGB", (img.width, img.height + band), (255, 255, 255))
    out.paste(img, (0, 0))
    draw = ImageDraw.Draw(out)
    draw.line([(0, img.height), (img.width, img.height)], fill=BOUNDARY_RGB, width=1)
    margin = size
    y = img.height + margin
    draw.text((margin, y), lines[0], fill=(30, 30, 30), font=bold)
    y += int(size * 1.9)
    for line in lines[1:]:
        draw.text((margin, y), line, fill=(60, 60, 60), font=font)
        y += int(size * 1.35)
    return out


def fetch_boundaries(site_url: str, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    """`departements.geojson` du site (champis-web), le fichier que l'app lit déjà."""
    url = site_url.rstrip("/") + "/web/departements.geojson"
    client = http or httpx.Client(timeout=30.0)
    try:
        r = client.get(url)
        r.raise_for_status()
        return r.json().get("features", [])
    finally:
        if http is None:
            client.close()


def render(
    bucket: BucketLike,
    species_id: str,
    zoom: int,
    out_dir: Path,
    boundaries: list[dict[str, Any]] | None = None,
    force: bool = False,
    progress: Progress | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rend et dépose la carte d'une espèce ; renvoie `{key, bytes, width, height, tiles,
    skipped}`. Lève `LookupError` si l'espèce n'a pas de T publié."""
    manifest = manifest or bucket.get_json(MANIFEST_KEY) or {}
    entry = france_entry(manifest, species_id)
    if entry is None:
        raise LookupError(f"{species_id} : aucun PMTiles T publié dans le jeu France")
    key = render_key(species_id, entry["hash"], zoom)
    present = bucket.list(key)
    if present and not force:
        return {"key": key, "bytes": present[key], "skipped": True}
    source = RangeSource(bucket, entry.get("key") or TILES_PREFIX + entry["name"])
    canvas, x0, y0, found = read_canvas(source, zoom, progress)
    img = colorize(canvas, load_palette())
    del canvas
    if boundaries:
        draw_boundaries(img, boundaries, zoom, x0, y0)
    names = load_species(species_id).get("names", {})
    generated = str(manifest.get("generated", ""))[:10]
    lines = [
        f"{names.get('fr', species_id)} ({names.get('la', '')}) · potentiel toute saison",
        f"Champis · champis.app · jeu de tuiles du {generated} · espèce {entry['hash']} · niveau {zoom}",
        "Le potentiel terrain T dit où les conditions sont réunies, jamais « il y en a » : violet d'autant plus foncé que T est haut.",
        CREDITS,
    ]
    img = with_cartouche(img, lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / Path(key).name
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    path.write_bytes(buf.getvalue())
    bucket.upload(path, key)
    return {
        "key": key,
        "bytes": path.stat().st_size,
        "width": img.width,
        "height": img.height,
        "tiles": found,
        "requests": source.requests,
        "skipped": False,
    }
