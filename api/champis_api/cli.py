"""CLI : `champis-api [-v|-q] warm [--interval 2] [--pull] [--publish]` — pré-chauffe du cache
météo puis indice par département ; sur Railway, dans le service cron `champis-warm` (`.railway/railway.ts`) avec
`--pull` (repart de l'instantané publié dans le bucket, puis d'un amorçage plus récent) et
`--publish` (dépose l'instantané que l'API importe) ; `champis-api notify [--pull]` — notification
récapitulative « mes coins », une fois (cron `champis-notify`) ; `champis-api publish-cache` —
dépose le cache local dans le bucket (migration d'une API qui pré-chauffait elle-même) ;
`champis-api seed [--out data/weather_seed.db] [--interval 1] [--climatology]` — amorçage de
l'historique depuis l'archive gratuite (hors Railway, une fois par grille ; la climatologie vient
de `champis-pipeline soil-clim`) ; `champis-api publish-seed [--seed …]` — dépôt dans le bucket ;
`champis-api doctor [--offline]` — l'environnement est-il prêt ; `champis-api stats [--days 14]` —
mesure minimale, les compteurs agrégés par jour (étape C, lot C2) ; `champis-api render
[--species …] [--zoom 9] [--force]` — carte HD de la France par espèce dans le bucket (backoffice,
lot BO3) ; `champis-api worker [--once]` — le service d'exécution `champis-jobs`, qui sert la file
des tâches du backoffice.

Affichage : module `champis_pipeline.console` (étapes, statut, journal dans data/logs/). Journal
des tâches (spec › Backoffice, lot BO1) : quand `DATABASE_URL` est fournie, chaque commande qui
fait quelque chose s'inscrit dans la table `jobs` avec ses étapes et sa progression (`jobs.py`), et
son journal complet part dans le bucket s'il y en a un ; `doctor` et `stats` ne s'inscrivent pas."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import httpx
from champis_pipeline import console
from champis_pipeline.progress import tqdm
from champis_pipeline.species import list_species

from . import counts, jobs
from .apns import ApnsClient
from .cache import WeatherCache
from .coins import notify_all
from .departements import compute as compute_dept_index
from .grid import WeatherGrid
from .seed import publish_seed, seed, sync_seed
from .settings import Settings
from .snapshot import publish_cache, sync_cache
from .store import Store
from .warm import WARM_INTERVAL_S, warm
from .weather_client import OpenMeteoClient


def _bucket(settings: Settings, why: str):
    """Le bucket Railway, ou une erreur nommant ce qui manque."""
    from champis_pipeline.storage import Bucket

    if settings.s3 is None:
        raise SystemExit(
            f"bucket non configuré ({why}) : AWS_ENDPOINT_URL et AWS_S3_BUCKET_NAME "
            "(ou CHAMPIS_S3_*) requis"
        )
    return Bucket(settings.s3)


def _pull(steps, settings: Settings, cache: WeatherCache, bucket) -> None:
    """Repart de l'état publié : l'instantané du cache du cron, puis un amorçage plus récent.
    Idempotent : rien n'est téléchargé si la base locale est déjà à jour."""
    work = settings.cache_db.parent
    with steps.step("cache publié dans le bucket") as step:
        meta = sync_cache(cache, bucket, work)
        if meta is None:
            step.skip("rien de nouveau" if cache.meta_get("cache_published") else "aucun")
        else:
            step.note(f"{meta['published']}, {meta.get('cells', 0)} cellules")
    with steps.step("amorçage") as step:
        n = sync_seed(cache, bucket, settings.weather_seed, work)
        if n:
            step.note(f"{n} cellules importées")
        else:
            step.skip(f"{cache.seeded_cells()} cellules amorcées")


async def _warm(interval: float, pull: bool, publish: bool) -> int:
    c = console.get()
    settings = Settings()
    bucket = _bucket(settings, "--pull/--publish") if pull or publish else None
    grid = WeatherGrid.load(settings.weather_grid)
    settings.cache_db.parent.mkdir(parents=True, exist_ok=True)
    cache = WeatherCache(settings.cache_db, shared=True)
    steps = c.steps(
        2 + 2 * pull + publish + bool(settings.database_url),
        f"warm · {len(grid.cells)} cellules · une requête toutes les {interval:g} s",
    )
    try:
        if pull:
            _pull(steps, settings, cache, bucket)
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
                        step.progress(
                            done,
                            total,
                            "cellules",
                            f"{unavailable} indisponibles, {stale} périmées",
                        )

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
        if publish:  # même incomplète, la passe vaut mieux que l'instantané de la veille
            _publish_cache_step(steps, settings, cache, bucket)
        if (
            settings.database_url
        ):  # mesure minimale (étape C2) : le bilan du jour, si la base est là
            with steps.step("bilan dans la mesure") as step:
                store = Store(settings.database_url)
                try:
                    counts.record_warm(store, report.cells, len(report.unavailable))
                finally:
                    store.close()
                step.note(f"{len(report.unavailable)} cellules sans météo")
    finally:
        cache.close()
    for cid, reason in sorted(report.unavailable.items()):
        c.warn(f"{cid}: {reason}")
    c.summary("pré-chauffe terminée" if report.ok else "pré-chauffe incomplète")
    return 0 if report.ok else 1


def _publish_cache_step(steps, settings: Settings, cache: WeatherCache, bucket) -> None:
    with steps.step("publication du cache dans le bucket") as step:
        bar = tqdm(desc="téléversement", unit="B", unit_scale=True, unit_divisor=1024)
        try:
            sent = 0

            def progress(n: int) -> None:
                nonlocal sent
                sent += n
                bar.update(n)
                step.progress(sent, None, "octets")

            info = publish_cache(bucket, cache, settings.cache_db.parent, progress=progress)
        finally:
            bar.close()
        step.note(
            f"{info['published']}, {info['cells']} cellules, {info['cell_weather']} entrées, "
            f"{console.size(info['bytes'])}"
        )


def _publish_cache() -> int:
    c = console.get()
    settings = Settings()
    bucket = _bucket(settings, "publish-cache")
    if not settings.cache_db.exists():
        raise SystemExit(f"{settings.cache_db} absent : rien à publier")
    cache = WeatherCache(settings.cache_db, shared=True)
    try:
        _publish_cache_step(c.steps(1), settings, cache, bucket)
    finally:
        cache.close()
    c.summary("cache publié ; l'API l'importe au prochain passage du meneur")
    return 0


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


async def _notify(pull: bool) -> int:
    c = console.get()
    settings = Settings()
    if not settings.apns_configured:
        raise SystemExit(
            "APNs non configuré : CHAMPIS_APNS_KEY, _KEY_ID, _TEAM_ID (et _TOPIC) requis"
        )
    if not settings.database_url:
        raise SystemExit("pas de base (DATABASE_URL) : aucun coin à annoncer")
    bucket = _bucket(settings, "--pull") if pull else None
    grid = WeatherGrid.load(settings.weather_grid)
    settings.cache_db.parent.mkdir(parents=True, exist_ok=True)
    cache = WeatherCache(settings.cache_db, shared=True)
    store = Store(settings.database_url)
    steps = c.steps(1 + 2 * pull)
    try:
        if pull:  # la météo des coins vient de la pré-chauffe de 3 h, publiée par le cron
            _pull(steps, settings, cache, bucket)
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
            with steps.step("notification « mes coins »") as step:
                r = await notify_all(store, grid, client, apns)
                step.note(
                    f"{r.devices} appareils, {r.sent} envoyées, {r.skipped} sans rien à annoncer, "
                    f"{r.failed} échecs ({r.tokens_cleared} jetons retirés)"
                )
    finally:
        store.close()
        cache.close()
    return 0 if r.failed == 0 else 1


def _render(species: list[str] | None, zoom: int, force: bool) -> int:
    """Carte HD de la France du potentiel toute saison (spec › Backoffice › Carte HD) : une étape
    par espèce, les tuiles lues dans le bucket par plages, le PNG déposé sous `renders/`."""
    from . import render

    c = console.get()
    settings = Settings()
    bucket = _bucket(settings, "render")
    wanted = [s for s in (species or []) if s != "all"] or list_species()
    unknown = [s for s in wanted if s not in list_species()]
    if unknown:
        raise SystemExit(f"espèce inconnue : {', '.join(unknown)}")
    steps = c.steps(1 + len(wanted), f"render · {len(wanted)} espèce(s) · niveau {zoom}")
    with steps.step("limites des départements (site)") as step:
        boundaries = render.fetch_boundaries(settings.site_url)
        step.note(f"{len(boundaries)} départements")
    manifest = bucket.get_json(render.MANIFEST_KEY) or {}
    missing = 0
    for sid in wanted:
        with steps.step(f"carte {sid}") as step:
            try:
                info = render.render(
                    bucket,
                    sid,
                    zoom,
                    settings.cache_db.parent / "renders",
                    boundaries,
                    force=force,
                    manifest=manifest,
                    progress=lambda done, total: step.progress(done, total, "tuiles"),
                )
            except LookupError as e:
                missing += 1
                step.skip(str(e))
                continue
            if info["skipped"]:
                step.skip(f"{info['key']} déjà dans le bucket ({console.size(info['bytes'])})")
            else:
                step.note(
                    f"{info['key']} · {info['width']} × {info['height']} px · "
                    f"{console.size(info['bytes'])} · {info['tiles']} tuiles"
                )
    c.summary(
        "cartes à jour"
        if not missing
        else f"{missing} espèce(s) sans tuiles publiées, pas de carte"
    )
    return 0 if not missing else 1


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
    p.add_argument(
        "--pull",
        action="store_true",
        help="d'abord reprendre le cache publié dans le bucket, puis un amorçage plus récent",
    )
    p.add_argument(
        "--publish",
        action="store_true",
        help="ensuite déposer l'instantané du cache dans le bucket (l'API l'importe)",
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
    p = sub.add_parser(
        "notify", help="envoie la notification récapitulative « mes coins » (une fois)"
    )
    p.add_argument(
        "--pull", action="store_true", help="d'abord reprendre le cache publié dans le bucket"
    )
    sub.add_parser(
        "publish-cache",
        help="dépose un instantané du cache météo local dans le bucket (migration vers le cron)",
    )
    p = sub.add_parser(
        "render",
        help="carte HD de la France du potentiel toute saison (PNG dans le bucket, backoffice)",
    )
    p.add_argument("--species", action="append", help="id d'espèce (répétable) ; défaut : toutes")
    p.add_argument("--zoom", type=int, default=9, choices=(9, 10), help="niveau de tuiles rendu")
    p.add_argument("--force", action="store_true", help="refaire même si la carte existe")
    p = sub.add_parser(
        "worker", help="service d'exécution : sert la file des tâches du backoffice (champis-jobs)"
    )
    p.add_argument("--once", action="store_true", help="traite au plus une tâche puis s'arrête")
    p.add_argument(
        "--poll", type=float, default=5.0, help="secondes entre deux lectures de la file"
    )
    p = sub.add_parser(
        "stats", help="mesure minimale : les compteurs agrégés par jour (Adrien seul)"
    )
    p.add_argument("--days", type=int, default=14, help="nombre de jours, aujourd'hui compris")
    p = sub.add_parser(
        "doctor", help="l'environnement est-il prêt ? (.env, grille, bucket, base, APNs…)"
    )
    p.add_argument("--offline", action="store_true", help="ne pas interroger le bucket")
    return ap


def _doctor(offline: bool) -> int:
    from .doctor import render, run_checks

    render(run_checks(Settings(), offline=offline))
    return 0  # un état des lieux, pas un échec : `make doctor` reste silencieux côté make


def _stats(days: int) -> int:
    """Mesure minimale (étape C, lot C2) : un tableau par jour sur stdout, le plus récent en haut."""
    from champis_pipeline.status import table

    settings = Settings()
    if not settings.database_url:
        raise SystemExit("pas de base (DATABASE_URL) : aucun compteur à lire")
    store = Store(settings.database_url)
    try:
        end = counts.today()
        span = [end - timedelta(days=k) for k in range(max(days, 1))]
        data = store.counts_since(span[-1])
    finally:
        store.close()
    print(table(counts.rows(data, span), counts.header()))
    return 0


def _dispatch(args) -> int:
    if args.cmd == "doctor":
        return _doctor(args.offline)
    if args.cmd == "stats":
        return _stats(args.days)
    if args.cmd == "notify":
        return asyncio.run(_notify(args.pull))
    if args.cmd == "publish-cache":
        return _publish_cache()
    if args.cmd == "seed":
        return asyncio.run(_seed(args.out, args.interval, args.climatology))
    if args.cmd == "publish-seed":
        return _publish_seed(args.seed)
    if args.cmd == "render":
        return _render(args.species, args.zoom, args.force)
    if args.cmd == "worker":
        from . import worker

        return worker.main(Settings(), once=args.once, poll_s=args.poll)
    return asyncio.run(_warm(args.interval, args.pull, args.publish))


def _job_args(args) -> dict:
    return {k: v for k, v in vars(args).items() if k not in ("cmd", "verbose", "quiet")}


def _log_bucket(settings: Settings):
    """Le bucket pour y déposer le journal de la tâche, None s'il n'y en a pas."""
    if settings.s3 is None:
        return None
    from champis_pipeline.storage import Bucket

    return Bucket(settings.s3)


def main(argv: list[str] | None = None) -> int:
    from champis_pipeline.config import load_env

    load_env()
    args = build_parser().parse_args(argv)
    c = console.setup("api-" + args.cmd, verbose=args.verbose, quiet=args.quiet)
    settings = Settings()
    # journal des tâches (backoffice) : la commande tourne même si la base manque ou refuse
    registration = jobs.register(
        args.cmd, {k: str(v) for k, v in _job_args(args).items()}, settings.database_url, c
    )
    code = console.run(_dispatch, args)
    if registration is not None:
        try:
            bucket = _log_bucket(settings)
        except Exception:  # un bucket mal configuré ne doit pas cacher le résultat
            bucket = None
        registration.close(code, c.log_path, bucket)
    return code


if __name__ == "__main__":
    sys.exit(main())
