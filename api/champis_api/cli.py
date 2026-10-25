"""CLI : `champis-api warm [--interval 2]` — pré-chauffe du cache météo (cron, sur Railway) ;
`champis-api seed [--out data/weather_seed.db] [--interval 1] [--climatology]` — amorçage de
l'historique depuis l'archive gratuite (hors Railway, une fois par grille ; la climatologie vient
de `champis-pipeline soil-clim`) ; `champis-api publish-seed [--seed …]` — dépôt dans le bucket."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

from .cache import WeatherCache
from .grid import WeatherGrid
from .seed import publish_seed, seed
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


async def _seed(out: Path, interval: float, climatology: bool) -> int:
    settings = Settings()
    grid = WeatherGrid.load(settings.weather_grid)
    async with httpx.AsyncClient(headers={"User-Agent": "champis-api seed"}) as http:
        client = OpenMeteoClient(
            http, WeatherCache(), min_interval_s=interval, api_key=settings.open_meteo_api_key
        )
        info = await seed(grid, client, out, climatology=climatology)
    print(
        f"{out} : historique {info['history_cells']}/{info['grid_cells']} cellules, "
        f"climatologie {info['cells']}/{info['grid_cells']} ({info['bytes'] / 1e6:.0f} Mo, "
        f"≈ {client.weight:.0f} appels pondérés)"
    )
    return 0 if info["history_cells"] == info["grid_cells"] else 1


def _publish_seed(path: Path) -> int:
    from champis_pipeline.progress import tqdm
    from champis_pipeline.storage import Bucket, S3Config

    cfg = S3Config.from_env()
    if cfg is None:
        raise SystemExit("bucket non configuré : CHAMPIS_S3_ENDPOINT et CHAMPIS_S3_BUCKET requis")
    with tqdm(
        total=path.stat().st_size, desc="publish-seed", unit="B", unit_scale=True, unit_divisor=1024
    ) as bar:
        info = publish_seed(Bucket(cfg), path, progress=bar.update)
    print(f"{info['cells']} cellules ({info['generated']}, {info['bytes'] / 1e6:.0f} Mo) publiées")
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
    p.add_argument("--out", type=Path, default=Settings().weather_seed_db)
    p.add_argument("--interval", type=float, default=1.0, help="secondes entre requêtes")
    p.add_argument(
        "--climatology",
        action="store_true",
        help="aussi la climatologie des cellules qui n'en ont pas (lourd : 26 appels/cellule)",
    )
    p = sub.add_parser("publish-seed", help="dépose la base d'amorçage dans le bucket")
    p.add_argument("--seed", type=Path, default=Settings().weather_seed_db)
    args = ap.parse_args(argv)
    if args.cmd == "seed":
        return asyncio.run(_seed(args.out, args.interval, args.climatology))
    if args.cmd == "publish-seed":
        return _publish_seed(args.seed)
    return asyncio.run(_warm(args.interval))


if __name__ == "__main__":
    sys.exit(main())
