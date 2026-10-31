"""Pré-chauffe du cache météo : parcourt toute la grille, lentement, pour que la navigation ne
touche jamais Open-Meteo. À lancer par cron une fois par seau de 12 h (skill open-meteo)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .grid import Cell, WeatherGrid
from .weather_client import OpenMeteoClient

log = logging.getLogger(__name__)

WARM_BATCH = 50  # cellules par lot : ≤ BATCH_ARCHIVE, le plus petit des lots non climatologiques
WARM_INTERVAL_S = 2.0  # une requête toutes les 2 s : loin sous la limite par minute


@dataclass
class WarmReport:
    cells: int
    unavailable: dict[str, str]
    stale: int
    weight: float = 0.0  # appels pondérés Open-Meteo estimés pour ce passage

    @property
    def ok(self) -> bool:
        return not self.unavailable


async def warm(
    grid: WeatherGrid,
    client: OpenMeteoClient,
    progress: Callable[[int, int, int, int], None] | None = None,
) -> WarmReport:
    """`progress(faites, total, indisponibles, périmées)` après chaque lot ; défaut : journal."""
    cells: list[Cell] = list(grid.cells.values())
    unavailable: dict[str, str] = {}
    stale = 0
    weight0 = client.weight
    for i in range(0, len(cells), WARM_BATCH):
        batch = cells[i : i + WARM_BATCH]
        res = await client.fetch_cells_partial(batch)
        unavailable |= res.unavailable
        stale += sum(1 for r in res.raws.values() if r.stale)
        (progress or _log_progress)(
            min(i + WARM_BATCH, len(cells)), len(cells), len(unavailable), stale
        )
    weight = client.weight - weight0
    log.info(
        "pré-chauffe terminée : %d cellules, ≈ %.0f appels pondérés Open-Meteo (%.2f par cellule)",
        len(cells),
        weight,
        weight / len(cells) if cells else 0.0,
    )
    return WarmReport(len(cells), unavailable, stale, weight)


def _log_progress(done: int, total: int, unavailable: int, stale: int) -> None:
    log.info("pré-chauffe %d/%d : %d indisponibles, %d périmées", done, total, unavailable, stale)
