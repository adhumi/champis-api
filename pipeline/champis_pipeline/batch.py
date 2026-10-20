"""Étape « batch » : download → grid → terrain pour une liste de départements, en parallèle.

Un processus par département (mémoire ~2 Go chacun, skill geo-pipeline), les erreurs n'arrêtent
pas les autres, et `data/batch_status.json` garde l'état pour reprendre : un département « ok »
n'est pas refait sans `--force`. Les étapes elles-mêmes restent idempotentes (fichiers présents =
rien à faire), donc relancer après une coupure ne coûte que ce qui manque.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import BDFORET_EDITIONS, DATA_DIR

log = logging.getLogger(__name__)

STATUS_FILE = DATA_DIR / "batch_status.json"
DEPTS_ALL = sorted(BDFORET_EDITIONS)


@dataclass
class DeptResult:
    dept: str
    ok: bool
    message: str
    seconds: float
    finished: str


def run_dept(dept: str, species: list[str] | None, force: bool) -> DeptResult:
    """Les trois étapes d'un département, dans ce processus. Exceptions capturées en message."""
    os.environ.setdefault("CHAMPIS_QUIET", "1")  # pas de barres tqdm entrelacées entre processus
    from . import download, rasterize, terrain
    from .species import list_species

    t0 = time.monotonic()
    try:
        shp = download.bdforet(dept)
        grid = rasterize.dept_grid(shp)
        bbox = rasterize.grid_bbox_4326(grid)
        rasterize.host_cover(dept, shp, grid, force)
        rasterize.ph(dept, download.soilgrids_ph(bbox), grid, force)
        rasterize.dem_products(dept, download.copernicus_dem_tiles(bbox), grid, force)
        for sid in species or list_species():
            terrain.compute(dept, sid, force)
        msg = f"grille {grid.width}×{grid.height} px"
        ok = True
    except Exception as e:
        msg = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        ok = False
    return DeptResult(dept, ok, msg, time.monotonic() - t0, datetime.now(UTC).isoformat())


def load_status(path: Path = STATUS_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_status(status: dict[str, dict], path: Path = STATUS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def batch(
    depts: list[str],
    species: list[str] | None = None,
    jobs: int = 2,
    force: bool = False,
    runner: Callable[[str, list[str] | None, bool], DeptResult] = run_dept,
    executor_factory: Callable[[int], Executor] | None = None,  # défaut : « spawn »
    status_path: Path = STATUS_FILE,
) -> dict[str, dict]:
    """Lance `runner` sur chaque département manquant, `jobs` à la fois ; écrit l'état au fil de l'eau."""
    if executor_factory is None:

        def executor_factory(n: int) -> Executor:
            return ProcessPoolExecutor(n, mp_context=multiprocessing.get_context("spawn"))

    status = load_status(status_path)
    todo = [d for d in depts if force or status.get(d, {}).get("ok") is not True]
    log.info("batch : %d départements à faire, %d déjà ok", len(todo), len(depts) - len(todo))
    if not todo:
        return status
    with executor_factory(jobs) as ex:
        futures = {ex.submit(runner, d, species, force): d for d in todo}
        for fut in as_completed(futures):
            r = fut.result()
            status[r.dept] = asdict(r)
            save_status(status, status_path)
            (log.info if r.ok else log.error)(
                "%s : %s (%.0f s)", r.dept, "ok" if r.ok else "ÉCHEC", r.seconds
            )
            if not r.ok:
                log.error("%s", r.message)
    return status


def parse_depts(values: list[str]) -> list[str]:
    """`all` ou une liste de codes (zéro initial ajouté : `1` → `01`), dans l'ordre demandé."""
    if values == ["all"]:
        return list(DEPTS_ALL)
    out = []
    for v in values:
        d = v.upper().zfill(2)
        if d not in BDFORET_EDITIONS:
            raise SystemExit(f"département inconnu : {v}")
        out.append(d)
    return out
