"""Schéma SQLite partagé par le cache météo de l'API et la base d'amorçage `weather_seed.db`.

Le pipeline écrit la climatologie du sol (`soil_clim`, `champis-pipeline soil-clim`) ; l'API y
ajoute l'historique amorcé (`champis-api seed`) et importe le tout par ATTACH au démarrage. Un
seul endroit définit les tables pour que les deux fichiers restent attachables l'un à l'autre.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    cell_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    bucket TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (cell_id, kind, bucket)
);
CREATE TABLE IF NOT EXISTS history (
    cell_id TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (cell_id, day)
);
CREATE TABLE IF NOT EXISTS soil_clim (
    cell_id TEXT PRIMARY KEY,
    months_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

META_GENERATED = "seed_generated"  # date (ISO) de l'amorçage, comparée par l'API au démarrage


def connect(path: Path | str) -> sqlite3.Connection:
    """Ouvre (ou crée) une base au schéma commun."""
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.executescript(SCHEMA)
    db.commit()
    return db
