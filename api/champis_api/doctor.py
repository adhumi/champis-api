"""`champis-api doctor` : l'environnement est-il prêt ? Une ligne par point, lecture seule.

Vérifie ce qui, dans la pratique, bloque une publication ou un déploiement : `.env`, espace disque,
grille météo (version 2, hash des espèces), amorçage, clé Open-Meteo, bucket (joignable), cache disque
du relais de tuiles, base des observations, clé APNs, identifiants CDS, vecteurs de parité. Rien n'est modifié ; le réseau n'est
touché que pour le bucket (`--offline` l'évite).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from champis_pipeline import console
from champis_pipeline.config import DATA_DIR, ENV_FILE
from champis_pipeline.species import all_species_hash, list_species

from .settings import Settings

OK = "ok"
MISSING = "manque"  # à régler avant la mise en service
INFO = "info"  # état normal ou facultatif, dit pour mémoire

MIN_FREE_GB = 5  # un département prend ~5 Go de rasters et de tuiles


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def run_checks(settings: Settings | None = None, offline: bool = False) -> list[Check]:
    settings = settings or Settings()
    return [
        _env_file(),
        _disk(),
        _grid(settings),
        _seed(settings),
        _open_meteo(settings),
        _bucket(settings, offline),
        _tile_cache(settings),
        _database(settings),
        _apns(settings),
        _cds(),
        _species(),
        _vectors(),
    ]


def _env_file() -> Check:
    if ENV_FILE.exists():
        return Check(".env", OK, str(ENV_FILE))
    return Check(
        ".env",
        INFO,
        "absent : seules les variables de l'environnement comptent (normal sur Railway)",
        "`cp .env.example .env` sur ton poste",
    )


def _disk() -> Check:
    probe = DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent
    free = shutil.disk_usage(probe).free
    detail = f"{console.size(free)} libres pour {DATA_DIR}"
    if free < MIN_FREE_GB * 1e9:
        return Check("disque", MISSING, detail, f"un département demande ~{MIN_FREE_GB} Go")
    return Check("disque", OK, detail)


def _grid(settings: Settings) -> Check:
    path = settings.weather_grid
    if not path.exists():
        return Check("grille météo", MISSING, f"{path} absent", "`make weather-grid DEPTS=all`")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(
            "grille météo", MISSING, f"{path} illisible : {e}", "régénérer avec `make weather-grid`"
        )
    version = raw.get("version")
    cells = len(raw.get("cells", []))
    detail = f"{cells} cellules, version {version}"
    if version != 2:
        return Check(
            "grille météo",
            MISSING,
            detail,
            "version 2 requise pour l'indice par département : `make weather-grid`",
        )
    if raw.get("species_hash") != all_species_hash():
        return Check(
            "grille météo",
            MISSING,
            detail + f", hash espèces {raw.get('species_hash')} ≠ {all_species_hash()}",
            "les espèces ont changé : `make terrain` puis `make weather-grid`",
        )
    return Check("grille météo", OK, detail + ", espèces à jour")


def _seed(settings: Settings) -> Check:
    path = settings.weather_seed_db
    if not path.exists():
        return Check(
            "amorçage météo",
            INFO,
            f"{path} absent",
            "`make soil-clim` puis `make seed` et `make publish-seed` (une fois par grille)",
        )
    from .cache import WeatherCache

    db = WeatherCache(path)
    try:
        detail = (
            f"{db.seeded_cells()} cellules avec climatologie, {len(db.history_ids())} avec historique, "
            f"{console.size(path.stat().st_size)}"
        )
    finally:
        db.close()
    return Check("amorçage météo", OK, detail)


def _open_meteo(settings: Settings) -> Check:
    if settings.open_meteo_api_key:
        return Check("Open-Meteo", OK, "clé présente (plan payant, quota dédié)")
    return Check(
        "Open-Meteo",
        INFO,
        "sans clé : API gratuite, quota partagé",
        "CHAMPIS_OPEN_METEO_API_KEY pour le plan Standard",
    )


def _bucket(settings: Settings, offline: bool) -> Check:
    if settings.s3 is None:
        return Check(
            "bucket PMTiles",
            MISSING,
            "non configuré : `publish` et `GET /tiles` indisponibles",
            "AWS_ENDPOINT_URL, AWS_S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (.env.example)",
        )
    if offline:
        return Check(
            "bucket PMTiles", INFO, f"{settings.s3.bucket} configuré, non interrogé (--offline)"
        )
    from champis_pipeline.storage import MANIFEST_KEY, TILES_PREFIX, Bucket

    try:
        bucket = Bucket(settings.s3)
        present = bucket.list(TILES_PREFIX)
        manifest = bucket.get_json(MANIFEST_KEY) or {}
    except Exception as e:
        return Check(
            "bucket PMTiles",
            MISSING,
            f"{settings.s3.bucket} injoignable : {type(e).__name__}: {e}",
            "identifiants ou réseau",
        )
    pmtiles = [k for k in present if k.endswith(".pmtiles")]
    detail = f"{settings.s3.bucket} : {len(pmtiles)} PMTiles"
    if manifest.get("generated"):
        detail += f", manifeste du {manifest['generated'][:16]}"
    else:
        return Check("bucket PMTiles", MISSING, detail + ", pas de manifeste", "`make publish`")
    return Check("bucket PMTiles", OK, detail)


def _tile_cache(settings: Settings) -> Check:
    from .tile_cache import TileCache

    cache = TileCache.from_settings(settings)
    if cache is None:
        return Check(
            "cache relais tuiles",
            INFO,
            "désactivé (CHAMPIS_TILE_CACHE_MB=0) : chaque Range repasse par le bucket",
        )
    bound = f"{settings.tile_cache_mb} Mo"
    if settings.tiles_mode != "proxy":
        return Check(
            "cache relais tuiles", INFO, f"{cache.root}, {bound} ; inutilisé en mode presign"
        )
    if not cache.root.exists():
        return Check("cache relais tuiles", INFO, f"{cache.root} absent, créé au premier relais")
    st = cache.stats()
    return Check(
        "cache relais tuiles",
        OK,
        f"{cache.root} : {st.entries} plages, {console.size(st.bytes)} sur {bound}",
    )


def _database(settings: Settings) -> Check:
    url = settings.database_url
    if not url:
        return Check(
            "observations et coins",
            MISSING,
            "pas de base : endpoints en 503",
            "DATABASE_URL (Postgres Railway)",
        )
    if url.startswith("sqlite"):
        return Check(
            "observations et coins",
            INFO,
            "SQLite locale (développement)",
            "sur Railway : service Postgres connecté, DATABASE_URL injectée",
        )
    return Check("observations et coins", OK, url.split("://", 1)[0] + " (mot de passe masqué)")


def _apns(settings: Settings) -> Check:
    if settings.apns_configured:
        return Check(
            "notifications APNs", OK, f"clé {settings.apns_key_id}, sujet {settings.apns_topic}"
        )
    missing = [
        name
        for name, value in (
            ("CHAMPIS_APNS_KEY", settings.apns_key_pem),
            ("CHAMPIS_APNS_KEY_ID", settings.apns_key_id),
            ("CHAMPIS_APNS_TEAM_ID", settings.apns_team_id),
        )
        if not value
    ]
    return Check(
        "notifications APNs",
        MISSING,
        "non configurées : aucune notification « mes coins »",
        "manque " + ", ".join(missing) if missing else "clé .p8 illisible",
    )


def _cds() -> Check:
    if os.environ.get("CDSAPI_KEY") or (Path.home() / ".cdsapirc").exists():
        return Check("Climate Data Store", OK, "identifiants présents (soil-clim)")
    return Check(
        "Climate Data Store",
        INFO,
        "pas d'identifiants : `soil-clim` impossible",
        "CDSAPI_URL / CDSAPI_KEY ou ~/.cdsapirc",
    )


def _species() -> Check:
    ids = list_species()
    return Check("espèces", OK, f"{len(ids)} fichiers, hash {all_species_hash()}")


def _vectors() -> Check:
    from .vectors import OUT_DIR, build

    stale = []
    for name, content in build().items():
        text = json.dumps(content, ensure_ascii=False, indent=1) + "\n"
        path = OUT_DIR / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(name)
    if stale:
        return Check(
            "vecteurs de parité", MISSING, "périmés : " + ", ".join(stale), "`make vectors`"
        )
    return Check("vecteurs de parité", OK, f"{OUT_DIR.relative_to(OUT_DIR.parents[1])} à jour")


_STYLE = {OK: console.GREEN, MISSING: console.RED + console.BOLD, INFO: console.DIM}


def render(checks: list[Check], c: console.Console | None = None) -> int:
    """Une ligne par point, puis le compte de ce qui manque ; renvoie ce compte."""
    c = c or console.get()
    width = max(len(k.name) for k in checks)
    for k in checks:
        plain = f"{k.status:<7} {k.name:<{width}}  {k.detail}"
        c.write(
            c.style(f"{k.status:<7}", _STYLE[k.status])
            + " "
            + c.style(f"{k.name:<{width}}", console.BOLD)
            + f"  {k.detail}",
            plain,
        )
        if k.hint and k.status != OK:
            c.info(k.hint)
    missing = sum(k.status == MISSING for k in checks)
    c.summary(f"{missing} point(s) à régler" if missing else "tout est en place")
    return missing
