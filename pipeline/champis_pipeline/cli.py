"""CLI : `champis-pipeline [-v|-q] <étape> --dept 25 [--species boletus_edulis] [--force]`.

Étapes : download · grid · terrain · tiles · weather-grid · all · summary · check · explain · batch ·
publish · soil-clim · status. `make help` à la racine du dépôt les récapitule.

Affichage (module `console`) : étapes `[i/n]` et statut sur stderr, données sur stdout, copie dans
`data/logs/<étape>-<date>.log`. `-v` rétablit les journaux bruts et les traces, `-q` ne garde que
les avertissements et le résultat.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import console, download, rasterize, terrain, tiles, weather_grid
from .config import DATA_DIR
from .species import list_species


def _species(args) -> list[str]:
    return args.species or list_species()


def _depts(args) -> list[str]:
    from .batch import parse_depts

    return parse_depts(args.depts)


# ---------------------------------------------------------------------- étapes d'un département


def _download(step: console.Step, dept: str) -> None:
    shp = download.bdforet(dept)
    grid = rasterize.dept_grid(shp)
    bbox = rasterize.grid_bbox_4326(grid)
    download.soilgrids_ph(bbox)
    dem = download.copernicus_dem_tiles(bbox)
    n_dem = len(dem) if isinstance(dem, list | tuple) else 1
    step.note(
        f"BD Forêt {shp.name} · SoilGrids · DEM {n_dem} dalle(s) · grille {grid.width}x{grid.height} px"
    )


def _grid(step: console.Step, dept: str, force: bool) -> None:
    shp = download.bdforet(dept)
    grid = rasterize.dept_grid(shp)
    bbox = rasterize.grid_bbox_4326(grid)
    rasterize.host_cover(dept, shp, grid, force)
    rasterize.ph(dept, download.soilgrids_ph(bbox), grid, force)
    rasterize.dem_products(dept, download.copernicus_dem_tiles(bbox), grid, force)
    step.note(f"host, cover, ph, alt, slope, aspect · {grid.width}x{grid.height} px")


def _terrain(steps: console.Steps, dept: str, species: list[str], force: bool) -> None:
    for sid in species:
        with steps.step(f"terrain {sid}") as step:
            step.written(terrain.compute(dept, sid, force))


def cmd_download(args) -> None:
    with console.get().steps(1).step(f"download {args.dept}") as step:
        _download(step, args.dept)


def cmd_grid(args) -> None:
    with console.get().steps(1).step(f"grid {args.dept}") as step:
        _grid(step, args.dept, args.force)


def cmd_terrain(args) -> None:
    species = _species(args)
    _terrain(console.get().steps(len(species)), args.dept, species, args.force)


def cmd_all(args) -> None:
    """download → grid → terrain → tiles → weather-grid, sur un département."""
    species = _species(args)
    depts = [args.dept]
    n = 2 + len(species) + (len(species) + 1) + 1
    steps = console.get().steps(n, f"all --dept {args.dept} · {len(species)} espèce(s)")
    with steps.step(f"download {args.dept}") as step:
        _download(step, args.dept)
    with steps.step(f"grid {args.dept}") as step:
        _grid(step, args.dept, args.force)
    _terrain(steps, args.dept, species, args.force)
    _tiles(steps, depts, species, args.force, None, per_dept=False)
    with steps.step("weather-grid") as step:
        step.written(weather_grid.build(depts, args.species))
    console.get().summary(f"{args.dept} prêt")


# ---------------------------------------------------------------------- tuiles et grille météo


def _tiles(
    steps: console.Steps,
    depts: list[str],
    species: list[str],
    force: bool,
    jobs: int | None,
    per_dept: bool,
) -> None:
    for sid in species:
        with steps.step(f"tiles T {sid}") as step:
            step.written(tiles.build(sid, depts, force, jobs))
    with steps.step("tiles A (classes d'altitude)") as step:
        step.written(tiles.build_alt(depts, force, jobs))  # couche A, commune à toutes les espèces
    if per_dept and len(depts) > 1:
        # jeux par département pour le hors ligne de l'app : extraits du jeu complet, pas re-rendus
        for d in depts:
            with steps.step(f"tiles département {d}") as step:
                out = [tiles.build_dept(sid, d, depts, force, jobs) for sid in species]
                out.append(tiles.build_alt_dept(d, depts, force, jobs))
                step.note(
                    f"{len(out)} fichiers · {console.size(sum(p.stat().st_size for p in out))}"
                )


def cmd_tiles(args) -> None:
    depts = _depts(args)
    species = _species(args)
    per_dept = bool(getattr(args, "per_dept", False)) and len(depts) > 1
    n = len(species) + 1 + (len(depts) if per_dept else 0)
    steps = console.get().steps(
        n, f"tiles · {len(depts)} département(s) · {len(species)} espèce(s)"
    )
    _tiles(steps, depts, species, args.force, getattr(args, "jobs", None), per_dept)
    console.get().summary("tuiles prêtes")


def cmd_weather_grid(args) -> None:
    depts = _depts(args)
    with console.get().steps(1).step(f"weather-grid · {len(depts)} département(s)") as step:
        step.written(weather_grid.build(depts, args.species))


# ---------------------------------------------------------------------- diagnostics


def cmd_summary(args) -> None:
    """Surface par classe et par espèce, en JSON sur stdout (le statut reste sur stderr)."""
    from .config import T_DIR
    from .species import species_hash

    for sid in _species(args):
        p = T_DIR / sid / f"{args.dept}-{species_hash(sid)}.tif"
        print(sid, json.dumps(terrain.summary(p), indent=1))


def cmd_explain(args) -> None:
    """Diagnostic : part de chaque facteur dans T sur le département (reports/explain-*.json)."""
    from . import explain

    species = _species(args)
    steps = console.get().steps(len(species))
    for sid in species:
        with steps.step(f"explain {sid}") as step:
            r = explain.explain(args.dept, sid)
            step.note(
                f"{r['excellent_share']:.0%} excellent sur {r['forest_ha']:,.0f} ha ; "
                + "facteurs moyens "
                + ", ".join(f"{k} {v['mean']:.2f}" for k, v in r["factors"].items())
            )


def cmd_check(args) -> None:
    """Contrôle GBIF : rapport HTML + JSON par espèce dans reports/ (réseau : api.gbif.org)."""
    import httpx
    import rasterio
    from rasterio.warp import transform_bounds

    from .check import gbif, report
    from .config import T_DIR
    from .species import load_species, species_hash

    species = _species(args)
    steps = console.get().steps(len(species), f"check GBIF · {args.dept}")
    with httpx.Client(timeout=60.0, headers={"User-Agent": "champis-pipeline check"}) as http:
        for sid in species:
            with steps.step(f"check {sid}") as step:
                t_path = T_DIR / sid / f"{args.dept}-{species_hash(sid)}.tif"
                with rasterio.open(t_path) as s:
                    bbox = transform_bounds(s.crs, "EPSG:4326", *s.bounds)
                occ, rejected = gbif.occurrences(
                    sid, load_species(sid)["names"]["la"], bbox, http, force=args.force
                )
                result = report.evaluate(args.dept, sid, occ, rejected=rejected)
                path = report.write_report(result, occ)
                step.note(
                    f"AUC {result['auc']:.3f} ({result['n_in_forest']}/{result['n_occurrences']} "
                    f"occ. en forêt) -> {path}"
                )


# ---------------------------------------------------------------------- lots et publication


def cmd_batch(args) -> None:
    """download → grid → terrain sur plusieurs départements en parallèle (reprise automatique)."""
    from . import batch

    c = console.get()
    depts = batch.parse_depts(args.depts)
    done_before = {d for d in depts if batch.load_status().get(d, {}).get("ok") is True}
    todo = len(depts) if args.force else len(depts) - len(done_before)
    c.title(
        f"batch · {len(depts)} département(s), {len(depts) - todo} déjà ok, {todo} à faire, "
        f"{args.jobs} en parallèle"
    )
    count = {"n": 0}

    def report(r: batch.DeptResult) -> None:
        count["n"] += 1
        status = "ok" if r.ok else "ECHEC"
        detail = r.message if r.ok else r.message.splitlines()[0]
        plain = f"  {r.dept:<3} {status:<6} {console.duration(r.seconds):>10}  {detail}  [{count['n']}/{todo}]"
        c.write(
            "  "
            + c.style(f"{r.dept:<3}", console.BOLD)
            + " "
            + c.style(f"{status:<6}", console.GREEN if r.ok else console.RED + console.BOLD)
            + " "
            + c.style(f"{console.duration(r.seconds):>10}", console.DIM)
            + f"  {detail}  "
            + c.style(f"[{count['n']}/{todo}]", console.DIM),
            plain,
        )
        if not r.ok:
            for line in r.message.splitlines()[1:]:
                c.info(line)

    status = batch.batch(depts, args.species, jobs=args.jobs, force=args.force, report=report)
    failed = [d for d in depts if not status.get(d, {}).get("ok")]
    c.summary(
        f"{len(depts) - len(failed)}/{len(depts)} départements ok · état : {batch.status_file()}"
    )
    if failed:
        c.error(
            f"{len(failed)} échec(s) : {' '.join(failed)}",
            hint="relance la même commande pour reprendre ; -v pour les traces",
        )
        raise SystemExit(1)


def cmd_soil_clim(args) -> None:
    """Climatologie du sol ERA5-Land (CDS) → soil_clim de data/weather_seed.db (une fois)."""
    from . import soil_clim

    years = None
    if args.years:
        a, b = (int(x) for x in args.years.split("-"))
        years = list(range(a, b + 1))
    with console.get().steps(1).step("soil-clim (ERA5-Land, CDS)") as step:
        n = soil_clim.build(args.grid, years, args.out)
        step.note(f"{n} cellules -> {args.out}")


NO_BUCKET = (
    "bucket non configuré : AWS_ENDPOINT_URL et AWS_S3_BUCKET_NAME (ou CHAMPIS_S3_*) requis ; "
    "`cp .env.example .env` et compléter"
)


def cmd_publish(args) -> None:
    """PMTiles de data/tiles → bucket S3 (variables CHAMPIS_S3_*, AWS_*), manifeste tiles/manifest.json.

    `--dry-run` : affiche ce qui serait téléversé et, avec `--prune`, supprimé ; n'écrit rien.
    """
    from . import publish
    from .storage import Bucket, S3Config

    c = console.get()
    cfg = S3Config.from_env()
    if cfg is None:
        raise SystemExit(NO_BUCKET)
    depts = _depts(args)
    species = _species(args)
    bucket = Bucket(cfg)
    steps = c.steps(
        1 if args.dry_run else 2,
        f"publish · {len(publish.dept_sets(depts, args.per_dept))} jeu(x) · {len(depts)} "
        f"département(s) -> {cfg.bucket}"
        + (" · prune" if args.prune else "")
        + (" · DRY RUN" if args.dry_run else ""),
    )
    with steps.step("comparaison avec le bucket") as step:
        p = publish.plan(bucket, depts, species, prune=args.prune, per_dept=args.per_dept)
        step.note(
            f"{len(p.todo)} à téléverser ({console.size(p.todo_bytes)}), {len(p.already)} déjà là"
            + (f", {len(p.stale)} à supprimer" if args.prune else "")
        )
    for name in p.todo:
        c.info(f"+ {name}  {console.size(p.sizes[name])}")
    for name in p.already:
        c.info(f"= {name}  {console.size(p.sizes[name])}")
    for key in p.stale:
        c.warn(f"- {key}  (autre jeu, supprimé par --prune)")
    if args.dry_run:
        c.summary("rien n'a été écrit (--dry-run)")
        return
    with steps.step("téléversement et manifeste") as step:
        manifest = publish.publish(
            bucket, depts, species, prune=args.prune, per_dept=args.per_dept, plan_=p
        )
        step.note(f"{len(manifest['tiles'])} fichiers dans le manifeste")
    c.summary(f"{len(manifest['tiles'])} fichiers dans {cfg.bucket}/tiles/")


def cmd_status(args) -> None:
    """Où en est chaque département (disque, batch, tuiles) et le jeu complet (local, bucket). Lecture seule."""
    from . import batch, status
    from .storage import Bucket, S3Config

    c = console.get()
    depts = _depts(args)
    species = _species(args)
    batch_status = batch.load_status()
    rows = [status.dept_status(d, species, batch_status) for d in depts]
    print(status.table([r.cells() for r in rows], status.COLUMNS))
    ready = sum(r.ready for r in rows)
    print()
    print(f"{ready}/{len(rows)} département(s) prêts pour `tiles` · {len(species)} espèce(s)")
    cfg = None if args.no_bucket else S3Config.from_env()
    bucket = Bucket(cfg) if cfg else None
    with c.steps(1).step(
        f"jeu complet · {len(depts)} département(s)" + (f" · bucket {cfg.bucket}" if cfg else "")
    ) as step:
        s = status.set_status(depts, species, bucket)
        note = f"local {len(s.local)}/{len(s.expected)}"
        if s.bucket is not None:
            note += f" · bucket {len(s.bucket)}/{len(s.expected)}"
            if s.manifest_generated:
                note += f" · manifeste du {s.manifest_generated[:16]}"
        elif cfg is None:
            note += " · bucket non configuré"
        step.note(note)
    if s.bucket_error:
        c.warn(f"bucket injoignable : {s.bucket_error}")
    for n in s.expected:
        marks = ("local" if n in s.local else "-----") + (
            "" if s.bucket is None else ("  bucket" if n in s.bucket else "  ------")
        )
        c.info(f"{marks}  {n}")


# ---------------------------------------------------------------------- entrée


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="champis-pipeline",
        description="Pipeline terrain Champis : de BD Forêt aux PMTiles. `make help` récapitule tout.",
        epilog="Statut sur stderr, données sur stdout, journal dans data/logs/ (CHAMPIS_LOG_DIR).",
    )
    # --species / --force acceptés avant ou après la sous-commande (les deux formes dans les docs)
    ap.add_argument("--species", action="append", help="id d'espèce (répétable) ; défaut : toutes")
    ap.add_argument("--force", action="store_true", help="refaire même si le résultat existe")
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="journaux bruts (module, niveau) et traces"
    )
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="sans barres ni détails : avertissements et résultat",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--species", action="append", default=argparse.SUPPRESS)
    common.add_argument("--force", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="étape")
    for name, fn, help_ in (
        ("download", cmd_download, "BD Forêt, SoilGrids, DEM du département dans data/raw/"),
        ("grid", cmd_grid, "rasters alignés 25 m dans data/grid/<dept>/"),
        ("terrain", cmd_terrain, "potentiel terrain T par espèce dans data/T/"),
        ("summary", cmd_summary, "surface par classe et par espèce (JSON sur stdout)"),
        ("check", cmd_check, "contrôle GBIF (AUC) : rapports dans reports/"),
        ("explain", cmd_explain, "part de chaque facteur dans T : reports/explain-*.json"),
        ("all", cmd_all, "download, grid, terrain, tiles, weather-grid sur un département"),
    ):
        p = sub.add_parser(name, parents=[common], help=help_)
        p.add_argument("--dept", required=True)
        p.set_defaults(fn=fn)
    for name, fn, help_ in (
        ("tiles", cmd_tiles, "PMTiles T par espèce et A (altitude) dans data/tiles/"),
        ("weather-grid", cmd_weather_grid, "cellules météo 5 km : data/weather_grid.json"),
    ):
        p = sub.add_parser(name, parents=[common], help=help_)
        p.add_argument("--depts", nargs="+", required=True, help="codes ou `all`")
        if name == "tiles":
            p.add_argument("--jobs", type=int, default=None, help="processus pour le niveau 15")
            p.add_argument(
                "--per-dept",
                action="store_true",
                help="aussi un jeu par département, extrait du jeu complet (hors ligne de l'app)",
            )
        p.set_defaults(fn=fn)
    p = sub.add_parser(
        "soil-clim", parents=[common], help="climatologie du sol ERA5-Land -> data/weather_seed.db"
    )
    p.add_argument("--grid", type=Path, default=DATA_DIR / "weather_grid.json")
    p.add_argument("--out", type=Path, default=DATA_DIR / "weather_seed.db")
    p.add_argument("--years", help="ex. 2016-2025 ; défaut : les 10 dernières années civiles")
    p.set_defaults(fn=cmd_soil_clim)
    p = sub.add_parser("publish", parents=[common], help="PMTiles -> bucket + manifeste")
    p.add_argument("--depts", nargs="+", required=True, help="codes ou `all`")
    p.add_argument(
        "--prune", action="store_true", help="supprimer du bucket les autres jeux de départements"
    )
    p.add_argument("--per-dept", action="store_true", help="publier aussi les jeux par département")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="montrer ce qui serait téléversé ou supprimé, sans rien écrire",
    )
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser(
        "status",
        parents=[common],
        help="où en est chaque département (disque, batch, tuiles, bucket)",
    )
    p.add_argument("--depts", nargs="+", default=["all"], help="codes ou `all` (défaut)")
    p.add_argument("--no-bucket", action="store_true", help="ne pas interroger le bucket")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser(
        "batch", parents=[common], help="download+grid+terrain, plusieurs départements, reprise"
    )
    p.add_argument("--depts", nargs="+", required=True, help="codes ou `all`")
    p.add_argument("--jobs", type=int, default=2, help="départements en parallèle (~2 Go chacun)")
    p.set_defaults(fn=cmd_batch)
    return ap


def main(argv: list[str] | None = None) -> int:
    from .config import load_env

    load_env()  # .env local (bucket, clés) ; sans effet si les variables sont déjà posées
    args = build_parser().parse_args(argv)
    console.setup(args.cmd, verbose=args.verbose, quiet=args.quiet)
    return console.run(args.fn, args)


if __name__ == "__main__":
    sys.exit(main())
