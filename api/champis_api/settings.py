"""Configuration par variables d'environnement (préfixe CHAMPIS_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from champis_pipeline.config import DATA_DIR, ROOT, load_env
from champis_pipeline.storage import S3Config

load_env()  # .env à la racine du dépôt (poste de travail) ; Railway fournit les variables


def _hours(value: str) -> tuple[int, ...]:
    return tuple(int(h) for h in value.split(",") if h.strip())


@dataclass(frozen=True)
class Settings:
    weather_grid: Path = Path(
        os.environ.get("CHAMPIS_WEATHER_GRID", DATA_DIR / "weather_grid.json")
    )
    cache_db: Path = Path(os.environ.get("CHAMPIS_CACHE_DB", DATA_DIR / "weather_cache.db"))
    # Amorçage local de repli (historique J−200 → J−1 + climatologie), `.db` ou ancien `.json` ;
    # en service, l'amorçage vient du bucket (weather/weather_seed.db) s'il y en a un
    weather_seed: Path = Path(
        os.environ.get("CHAMPIS_WEATHER_SEED", DATA_DIR / "weather_seed.json")
    )
    # Sortie de `champis-api seed` et source de `publish-seed`
    weather_seed_db: Path = Path(
        os.environ.get("CHAMPIS_WEATHER_SEED_DB", DATA_DIR / "weather_seed.db")
    )
    cors_origins: tuple[str, ...] = tuple(
        o for o in os.environ.get("CHAMPIS_CORS_ORIGINS", "*").split(",") if o
    )
    # Heures locales (Europe/Paris) de la pré-chauffe intégrée ; vide = aucune (cron externe)
    warm_hours: tuple[int, ...] = _hours(os.environ.get("CHAMPIS_WARM_HOURS", ""))
    # Pré-chauffer au démarrage (déploiement avec cache vide) ; attention au quota
    warm_on_start: bool = os.environ.get("CHAMPIS_WARM_ON_START", "") == "1"
    # Servir la carte web statique (web/, shared/, species/) sous /web, /shared, /species
    serve_web: bool = os.environ.get("CHAMPIS_SERVE_WEB", "1") == "1"
    repo_root: Path = ROOT
    # Clé API Open-Meteo (plan payant) : hôtes customer-*, quota dédié. Vide = gratuit.
    open_meteo_api_key: str = os.environ.get("CHAMPIS_OPEN_METEO_API_KEY", "")
    # Bucket des PMTiles (CHAMPIS_S3_ENDPOINT, CHAMPIS_S3_BUCKET, AWS_*) ; None = pas de /tiles
    s3: S3Config | None = field(default_factory=S3Config.from_env)
    # « presign » : URL présignées lues directement par le navigateur ; « proxy » : relais /tiles/{name}
    tiles_mode: str = os.environ.get("CHAMPIS_TILES_MODE", "presign")
    tiles_url_hours: int = int(os.environ.get("CHAMPIS_TILES_URL_HOURS", "24"))
