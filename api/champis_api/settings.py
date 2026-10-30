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
    # Observations et coins : Postgres Railway (DATABASE_URL injectée par le service Postgres) ;
    # sans Postgres, une base SQLite locale pour le développement. Vide = endpoints en 503.
    database_url: str = os.environ.get("CHAMPIS_DATABASE_URL") or os.environ.get(
        "DATABASE_URL", f"sqlite:///{DATA_DIR / 'champis.db'}"
    )
    # Notifications « mes coins » : clé APNs (.p8, contenu PEM ou chemin), identifiants Apple
    apns_key: str = os.environ.get("CHAMPIS_APNS_KEY", "")
    apns_key_id: str = os.environ.get("CHAMPIS_APNS_KEY_ID", "")
    apns_team_id: str = os.environ.get("CHAMPIS_APNS_TEAM_ID", "")
    apns_topic: str = os.environ.get("CHAMPIS_APNS_TOPIC", "fr.adhumi.champis")
    # Heures locales (Europe/Paris) d'envoi de la notification récapitulative ; vide = jamais
    notify_hours: tuple[int, ...] = _hours(os.environ.get("CHAMPIS_NOTIFY_HOURS", "7"))
    # Limitation de débit par IP (requêtes par minute) et taille maximale d'un corps (octets)
    rate_read_per_minute: float = float(os.environ.get("CHAMPIS_RATE_READ", "120"))
    rate_write_per_minute: float = float(os.environ.get("CHAMPIS_RATE_WRITE", "20"))
    max_body_bytes: int = int(os.environ.get("CHAMPIS_MAX_BODY", str(256 * 1024)))

    @property
    def apns_key_pem(self) -> str:
        """La clé peut être donnée en clair (variable Railway) ou par chemin de fichier .p8."""
        if self.apns_key.startswith("-----BEGIN"):
            return self.apns_key
        if self.apns_key and Path(self.apns_key).exists():
            return Path(self.apns_key).read_text(encoding="utf-8")
        return ""

    @property
    def apns_configured(self) -> bool:
        return bool(
            self.apns_key_pem and self.apns_key_id and self.apns_team_id and self.apns_topic
        )
