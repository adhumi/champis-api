"""CLI : `champis-api warm [--interval 2]` — pré-chauffe du cache météo (cron, sur Railway) ;
`champis-api seed [--out data/weather_seed.json] [--interval 5]` — amorçage depuis l'archive
gratuite (hors Railway, une fois par grille)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

from .cache import WeatherCache
from .grid import WeatherGrid
from .seed import seed
from .settings import Settings
from .warm import WARM_INTERVAL_S, warm
from .weather_client import OpenMeteoClient


async def _warm(interval: float) -> int:
    settings = Settings()
    grid = WeatherGrid.load(settings.weather_grid)
    settings.cache_db.parent.mkdir(parents=True, exist_ok=True)
    cache = WeatherCache(settings.cache_db)
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "champis-api warm"}) as http:
            client = OpenMeteoClient(
                http, cache, min_interval_s=interval, api_key=settings.open_meteo_api_key
            )
            report = await warm(grid, client)
    finally:
        cache.close()
    print(
        f"{report.cells} cellules, {len(report.unavailable)} indisponibles, "
        f"{report.stale} servies depuis un cache périmé, ≈ {report.weight:.0f} appels pondérés"
    )
    for cid, reason in sorted(report.unavailable.items()):
        print(f"  {cid}: {reason}")
    return 0 if report.ok else 1


async def _seed(out: Path, interval: float) -> int:
    settings = Settings()
    grid = WeatherGrid.load(settings.weather_grid)
    async with httpx.AsyncClient(headers={"User-Agent": "champis-api seed"}) as http:
        client = OpenMeteoClient(http, WeatherCache(), min_interval_s=interval)
        data = await seed(grid, client, out)
    print(f"{len(data['cells'])} cellules amorcées dans {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="champis-api")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("warm", help="remplit le cache météo pour toute la grille")
    p.add_argument(
        "--interval", type=float, default=WARM_INTERVAL_S, help="secondes entre requêtes"
    )
    p = sub.add_parser(
        "seed", help="amorce historique + climatologie depuis l'archive (clé = plan Professional)"
    )
    p.add_argument("--out", type=Path, default=Settings().weather_seed)
    p.add_argument("--interval", type=float, default=5.0, help="secondes entre requêtes")
    args = ap.parse_args(argv)
    if args.cmd == "seed":
        return asyncio.run(_seed(args.out, args.interval))
    return asyncio.run(_warm(args.interval))


if __name__ == "__main__":
    sys.exit(main())
