"""Grille de référence, chemins, départements."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .species import ROOT

DATA_DIR = ROOT / "data"
ENV_FILE = ROOT / ".env"


def load_env() -> bool:
    """Charge `.env` à la racine du dépôt (poste de travail) sans écraser l'environnement existant.

    Sur Railway les variables viennent du service : le fichier n'existe pas, rien ne change.
    """
    from dotenv import load_dotenv

    return load_dotenv(ENV_FILE, override=False)


RAW_DIR = DATA_DIR / "raw"
GRID_DIR = DATA_DIR / "grid"
T_DIR = DATA_DIR / "T"
TILES_DIR = DATA_DIR / "tiles"
REPORTS_DIR = ROOT / "reports"

CRS_CALC = "EPSG:2154"  # Lambert-93
CRS_TILES = "EPSG:3857"
RES_M = 25.0
GRID_X0 = 99_975.0  # origine commune : toutes les emprises s'alignent dessus
GRID_Y0 = 6_049_975.0

BLOCK = 2048

# flux Géoplateforme : date d'édition par département (skill geo-pipeline / rapport BD Forêt)
BDFORET_EDITIONS: dict[str, str] = {
    "01": "2014-04-01",
    "02": "2017-10-04",
    "03": "2014-04-01",
    "04": "2014-04-01",
    "05": "2015-04-01",
    "06": "2021-03-01",
    "07": "2014-04-01",
    "08": "2022-04-01",
    "09": "2017-10-04",
    "10": "2021-03-01",
    "11": "2018-11-20",
    "12": "2014-04-01",
    "13": "2014-04-01",
    "14": "2014-04-01",
    "15": "2015-04-01",
    "16": "2018-01-15",
    "17": "2017-10-04",
    "18": "2014-04-01",
    "19": "2016-02-16",
    "21": "2017-10-04",
    "22": "2018-10-01",
    "23": "2017-05-10",
    "24": "2019-01-09",
    "25": "2016-02-16",
    "26": "2014-04-01",
    "27": "2014-04-01",
    "28": "2017-05-10",
    "29": "2014-04-01",
    "2A": "2017-05-10",
    "2B": "2016-02-16",
    "30": "2018-11-20",
    "31": "2019-01-09",
    "32": "2015-04-01",
    "33": "2017-05-10",
    "34": "2018-01-15",
    "35": "2018-10-01",
    "36": "2021-03-01",
    "37": "2017-05-10",
    "38": "2014-04-01",
    "39": "2016-02-16",
    "40": "2018-10-01",
    "41": "2017-05-10",
    "42": "2014-04-01",
    "43": "2021-11-15",
    "44": "2018-10-01",
    "45": "2015-04-01",
    "46": "2017-05-10",
    "47": "2018-10-01",
    "48": "2017-05-10",
    "49": "2018-10-01",
    "50": "2018-10-01",
    "51": "2021-03-01",
    "52": "2014-04-01",
    "53": "2018-10-01",
    "54": "2014-04-01",
    "55": "2015-04-01",
    "56": "2021-03-01",
    "57": "2014-04-01",
    "58": "2014-04-01",
    "59": "2014-04-01",
    "60": "2015-04-01",
    "61": "2014-04-01",
    "62": "2014-04-01",
    "63": "2014-04-01",
    "64": "2014-04-01",
    "65": "2014-04-01",
    "66": "2018-11-20",
    "67": "2014-04-01",
    "68": "2014-04-01",
    "69": "2014-04-01",
    "70": "2015-06-01",
    "71": "2018-11-20",
    "72": "2022-04-01",
    "73": "2014-04-01",
    "74": "2014-04-01",
    "75": "2018-01-15",
    "76": "2015-06-01",
    "77": "2017-10-04",
    "78": "2019-01-09",
    "79": "2014-04-01",
    "80": "2015-04-01",
    "81": "2014-04-01",
    "82": "2017-10-04",
    "83": "2015-04-01",
    "84": "2022-04-01",
    "85": "2014-04-01",
    "86": "2014-04-01",
    "87": "2016-02-16",
    "88": "2015-04-01",
    "89": "2014-04-01",
    "90": "2015-06-01",
    "91": "2018-01-15",
    "92": "2018-01-15",
    "93": "2018-01-15",
    "94": "2017-10-04",
    "95": "2015-04-01",
}


def bdforet_url(dept: str) -> str:
    ed = BDFORET_EDITIONS[dept]
    name = f"BDFORET_2-0__SHP_LAMB93_D0{dept}_{ed}"
    return f"https://data.geopf.fr/telechargement/download/BDFORET/{name}/{name}.7z"


@dataclass(frozen=True)
class Grid:
    """Emprise alignée sur la grille France, en Lambert-93."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @classmethod
    def aligned(
        cls, xmin: float, ymin: float, xmax: float, ymax: float, pad_m: float = 500
    ) -> Grid:
        import math

        xmin -= pad_m
        ymin -= pad_m
        xmax += pad_m
        ymax += pad_m
        ax = GRID_X0 + math.floor((xmin - GRID_X0) / RES_M) * RES_M
        ay = GRID_Y0 + math.floor((ymin - GRID_Y0) / RES_M) * RES_M
        bx = GRID_X0 + math.ceil((xmax - GRID_X0) / RES_M) * RES_M
        by = GRID_Y0 + math.ceil((ymax - GRID_Y0) / RES_M) * RES_M
        return cls(ax, ay, bx, by)

    @property
    def width(self) -> int:
        return round((self.xmax - self.xmin) / RES_M)

    @property
    def height(self) -> int:
        return round((self.ymax - self.ymin) / RES_M)

    def transform(self):
        from rasterio.transform import from_origin

        return from_origin(self.xmin, self.ymax, RES_M, RES_M)

    def profile(self, dtype: str, nodata: int) -> dict:
        return {
            "driver": "GTiff",
            "width": self.width,
            "height": self.height,
            "count": 1,
            "dtype": dtype,
            "crs": CRS_CALC,
            "transform": self.transform(),
            "nodata": nodata,
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "compress": "DEFLATE",
            "predictor": 2,
        }


def dept_dirs(dept: str) -> dict[str, Path]:
    return {
        "raw": RAW_DIR / dept,
        "grid": GRID_DIR / dept,
    }
