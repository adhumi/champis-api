"""CLI : `champis-api [-v|-q] warm [--interval 2]` — pré-chauffe du cache météo puis indice par
département (cron, sur Railway) ; `champis-api notify` — notification récapitulative « mes coins »,
une fois ; `champis-api seed [--out data/weather_seed.db] [--interval 1] [--climatology]` — amorçage
de l'historique depuis l'archive gratuite (hors Railway, une fois par grille ; la climatologie vient
de `champis-pipeline soil-clim`) ; `champis-api publish-seed [--seed …]` — dépôt dans le bucket ;
`champis-api doctor [--offline]` — l'environnement est-il prêt.

Affichage : module `champis_pipeline.console` (étapes, statut, journal dans data/logs/)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from champis_pipeline import console
from champis_pipeline.progress import tqdm
from champis_pipeline.species import list_species

from .apns import ApnsClient
from .cache import WeatherCache
from .coins import notify_all
from .departements import compute as compute_dept_index
from .grid import WeatherGrid
from .seed import publish_seed, seed
from .settings import Settings
from .store import Store
from .warm import WARM_INTERVAL_S, warm
from .weather_client import OpenMeteoClient


async def _warm(interval: float) -> int:
    c = console.get()
    settings = Settings()
    grid = WeatherGrid.load(settings.weather_grid)
    settings.cache_db.parent.mkdir(parents=True, exist_ok=True)
    cache = WeatherCache(settings.cache_db)
    steps = c.steps(2, f"warm · {len(grid.cells)} cellules · une requête toutes les {interval:g} s")
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "champis-api warm"}) as http:
            client = OpenMeteoClient(
                http, cache, min_interval_s=interval, api_key=settings.open_meteo_api_key
            )
            with steps.step("pré-chauffe du cache météo") as step:
                with tqdm(total=len(grid.cells), desc="cellules", unit="cellule") as bar:

                    def progress(done: int, total: int, unavailable: int, stale: int) -> None:
                        bar.n = done
                        bar.set_postfix_str(f"{unavailable} indisponibles, {stale} périmées")
                        bar.refresh()

                    report = await warm(grid, client, progress=progress)
                step.note(
                    f"{report.cells} cellules, {len(report.unavailable)} indisponibles, "
                    f"{report.stale} servies depuis un cache périmé, ≈ {report.weight:.0f} appels pondérés"
                )
            with steps.step("indice par département") as step:
                index = await compute_dept_index(grid, client, cache, list_species())
                step.note(
                    f"{index.departements} départements, {index.cells} cellules "
                    f"({index.cells_without_weather} sans météo)"
                )
    finally:
        cache.close()
    for cid, reason in sorted(report.unavailable.items()):
        c.warn(f"{cid}: {reason}")
    c.summary("pré-chauffe terminée" if report.ok else "pré-chauffe incomplète")
    return 0 if report.ok else 1


async def _seed(out: Path, interval: float, climatology: bool) -> int:
    c = console.get()
    settings = Settings()
    grid = WeatherGrid.load(settings.weather_grid)
    steps = c.steps(1, f"seed · {len(grid.cells)} cellules -> {out}")
    async with httpx.AsyncClient(headers={"User-Agent": "champis-api seed"}) as http:
        client = OpenMeteoClient(
            http, WeatherCache(), min_interval_s=interval, api_key=settings.open_meteo_api_key
        )
        with steps.step("historique" + (" + climatologie" if climatology else "")) as step:
            bars: dict[str, tqdm] = {}

            def progress(what: str, done: int, total: int, weight: float) -> None:
                bar = bars.get(what)
                if bar is None:
                    bar = bars[what] = tqdm(total=total, desc=what, unit="cellule")
                bar.n = done
                bar.set_postfix_str(f"≈ {weight:.0f} appels pondérés")
                bar.refresh()

            try:
                info = await seed(grid, client, out, climatology=climatology, progress=progress)
            finally:
                for bar in bars.values():
                    bar.close()
            step.note(
                f"historique {info['history_cells']}/{info['grid_cells']} cellules, "
                f"climatologie {info['cells']}/{info['grid_cells']} ({console.size(info['bytes'])}, "
                f"≈ {client.weight:.0f} appels pondérés)"
            )
    complete = info["history_cells"] == info["grid_cells"]
    c.summary("amorçage complet" if complete else "amorçage incomplet : relance pour reprendre")
    return 0 if complete else 1


async def _notify() -> int:
    c = console.get()
    settings = Settings()
    if not settings.apns_configured:
        raise SystemExit(
            "APNs non configuré : CHAMPIS_APNS_KEY, _KEY_ID, _TEAM_ID (et _TOPIC) requis"
        )
    grid = WeatherGrid.load(settings.weather_grid)
    cache = WeatherCache(settings.cache_db)
    store = Store(settings.database_url)
    try:
        async with (
            httpx.AsyncClient(headers={"User-Agent": "champis-api notify"}) as http,
            httpx.AsyncClient(http2=True, timeout=10.0) as http2,
        ):
            client = OpenMeteoClient(http, cache, api_key=settings.open_meteo_api_key)
            apns = ApnsClient(
                http2,
                settings.apns_key_pem,
                settings.apns_key_id,
                settings.apns_team_id,
                settings.apns_topic,
            )
            with c.steps(1).step("notification « mes coins »") as step:
                r = await notify_all(store, grid, client, apns)
                step.note(
                    f"{r.devices} appareils, {r.sent} envoyées, {r.skipped} sans rien à annoncer, "
                    f"{r.failed} échecs ({r.tokens_cleared} jetons retirés)"
                )
    finally:
        store.close()
        cache.close()
    return 0 if r.failed == 0 else 1


def _publish_seed(path: Path) -> int:
    from champis_pipeline.storage import Bucket, S3Config

    c = console.get()
    cfg = S3Config.from_env()
    if cfg is None:
        raise SystemExit(
            "bucket non configuré : AWS_ENDPOINT_URL et AWS_S3_BUCKET_NAME (ou CHAMPIS_S3_*) requis ; "
            "`cp .env.example .env` et compléter"
        )
    if not path.exists():
        raise SystemExit(
            f"{path} absent : lancer `champis-api seed` (et `champis-pipeline soil-clim`)"
        )
    with c.steps(1).step(
        f"publish-seed · {path.name} ({console.size(path.stat().st_size)})"
    ) as step:
        with tqdm(
            total=path.stat().st_size,
            desc="téléversement",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as bar:
            info = publish_seed(Bucket(cfg), path, progress=bar.update)
        step.note(f"{info['cells']} cellules ({info['generated']}) -> {cfg.bucket}")
    c.summary("amorçage publié ; l'API l'importe à son prochain démarrage")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="champis-api")
    ap.add_argument("-v", "--verbose", action="store_true", help="journaux bruts et traces")
    ap.add_argument("-q", "--quiet", action="store_true", help="sans barres ni détails")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="commande")
    p = sub.add_parser("warm", help="remplit le cache météo pour toute la grille, puis l'indice")
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
    sub.add_parser("notify", help="envoie la notification récapitulative « mes coins » (une fois)")
    p = sub.add_parser(
        "doctor", help="l'environnement est-il prêt ? (.env, grille, bucket, base, APNs…)"
    )
    p.add_argument("--offline", action="store_true", help="ne pas interroger le bucket")
    return ap


def _doctor(offline: bool) -> int:
    from .doctor import render, run_checks

    render(run_checks(offline=offline))
    return 0  # un état des lieux, pas un échec : `make doctor` reste silencieux côté make


def _dispatch(args) -> int:
    if args.cmd == "doctor":
        return _doctor(args.offline)
    if args.cmd == "notify":
        return asyncio.run(_notify())
    if args.cmd == "seed":
        return asyncio.run(_seed(args.out, args.interval, args.climatology))
    if args.cmd == "publish-seed":
        return _publish_seed(args.seed)
    return asyncio.run(_warm(args.interval))


def main(argv: list[str] | None = None) -> int:
    from champis_pipeline.config import load_env

    load_env()
    args = build_parser().parse_args(argv)
    console.setup("api-" + args.cmd, verbose=args.verbose, quiet=args.quiet)
    return console.run(_dispatch, args)


if __name__ == "__main__":
    sys.exit(main())
