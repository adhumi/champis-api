"""Stockage SQLite du service météo : cache des prévisions brutes, historique accumulé,
climatologie du sol.

- `raw` : réponses prévision normalisées, par cellule et seau de 12 h. Si Open-Meteo est
  injoignable, `latest` sert la dernière réponse connue, marquée périmée par l'appelant.
- `history` : une ligne par (cellule, jour) pour les jours passés. Alimentée chaque jour par les
  ~60 jours de passé de la prévision (`source = forecast`) et, une fois, par l'amorçage ERA5-Land
  (`source = seed`, jamais écrasé par un amorçage ultérieur mais remplacé par la prévision quand
  elle couvre la date). Décision du 17 oct. 2026 : « amorcer, puis accumuler », l'API ne parle
  plus qu'à la prévision.
- `soil_clim` : 11 quantiles mensuels d'humidité du sol par cellule (amorçage), permanents.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

BUCKET_HOURS = 12  # deux rafraîchissements par jour (décision du 17 oct. 2026)
HISTORY_VARS = ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil")
HISTORY_KEEP_DAYS = 400  # au-delà, plus aucune fenêtre du modèle ne lit


def bucket_now(now: datetime) -> str:
    """Seau de 12 h en UTC : `2026-10-17T00` ou `2026-10-17T12`."""
    h = (now.hour // BUCKET_HOURS) * BUCKET_HOURS
    return now.astimezone(UTC).strftime("%Y-%m-%d") + f"T{h:02d}"


class WeatherCache:
    def __init__(self, path: Path | str = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(
            """
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
        )
        self.db.commit()

    # ------------------------------------------------------------------ prévisions brutes

    def get(self, cell_id: str, kind: str, bucket: str) -> Any | None:
        row = self.db.execute(
            "SELECT payload FROM raw WHERE cell_id = ? AND kind = ? AND bucket = ?",
            (cell_id, kind, bucket),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, cell_id: str, kind: str) -> tuple[Any, str] | None:
        """Dernière réponse connue (payload, fetched_at), quel que soit le seau."""
        row = self.db.execute(
            "SELECT payload, fetched_at FROM raw WHERE cell_id = ? AND kind = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (cell_id, kind),
        ).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def put(self, cell_id: str, kind: str, bucket: str, payload: Any, now: datetime) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO raw VALUES (?, ?, ?, ?, ?)",
            (cell_id, kind, bucket, now.astimezone(UTC).isoformat(), json.dumps(payload)),
        )
        # On ne garde qu'un seau par (cellule, type) : le plus récent suffit au mode périmé.
        self.db.execute(
            "DELETE FROM raw WHERE cell_id = ? AND kind = ? AND bucket != ?",
            (cell_id, kind, bucket),
        )
        self.db.commit()

    # ------------------------------------------------------------------ historique accumulé

    def put_history(
        self, cell_id: str, rows: dict[str, dict[str, float | None]], source: str
    ) -> int:
        """Range des jours passés. `forecast` écrase ; `seed` ne remplit que les trous.

        Un jour dont une variable manque n'est pas rangé : l'historique ne contient que des jours
        complets, la règle « jamais d'approximation silencieuse » s'applique au moment du calcul.
        """
        verb = "INSERT OR REPLACE" if source == "forecast" else "INSERT OR IGNORE"
        n = 0
        for day, vals in rows.items():
            if any(vals.get(v) is None for v in HISTORY_VARS):
                continue
            self.db.execute(
                f"{verb} INTO history VALUES (?, ?, ?, ?)",  # verbe contrôlé ci-dessus
                (cell_id, day, source, json.dumps([float(vals[v]) for v in HISTORY_VARS])),
            )
            n += 1
        self.db.commit()
        return n

    def history(self, cell_id: str, start: date, end: date) -> dict[str, dict[str, float]]:
        rows = self.db.execute(
            "SELECT day, values_json FROM history WHERE cell_id = ? AND day BETWEEN ? AND ?",
            (cell_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        return {day: dict(zip(HISTORY_VARS, json.loads(js), strict=True)) for day, js in rows}

    def last_history_day(self, cell_id: str) -> date | None:
        """Dernier jour présent dans l'historique (amorçage ou prévision), None si aucun."""
        row = self.db.execute(
            "SELECT MAX(day) FROM history WHERE cell_id = ?", (cell_id,)
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def history_days(self, cell_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM history WHERE cell_id = ?", (cell_id,)
        ).fetchone()[0]

    def prune_history(self, today: date) -> None:
        self.db.execute(
            "DELETE FROM history WHERE day < ?",
            ((today - timedelta(days=HISTORY_KEEP_DAYS)).isoformat(),),
        )
        self.db.commit()

    # ------------------------------------------------------------------ climatologie du sol

    def put_soil_clim(self, cell_id: str, months: dict[str, list[float]]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO soil_clim VALUES (?, ?)", (cell_id, json.dumps(months))
        )
        self.db.commit()

    def soil_clim(self, cell_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT months_json FROM soil_clim WHERE cell_id = ?", (cell_id,)
        ).fetchone()
        return {"months": json.loads(row[0])} if row else None

    def seeded_cells(self) -> int:
        """Cellules disposant d'une climatologie du sol, donc amorcées."""
        return self.db.execute("SELECT COUNT(*) FROM soil_clim").fetchone()[0]

    def seeded_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT cell_id FROM soil_clim")}

    def history_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT cell_id FROM history")}

    def import_seed_db(self, path: Path) -> int:
        """Importe une base d'amorçage (même schéma) par ATTACH : l'historique ne remplit que les
        trous (jamais par-dessus la prévision), la climatologie est remplacée. Rien en mémoire."""
        self.db.execute("ATTACH DATABASE ? AS seed", (str(path),))
        try:
            self.db.execute("INSERT OR IGNORE INTO history SELECT * FROM seed.history")
            self.db.execute("INSERT OR REPLACE INTO soil_clim SELECT * FROM seed.soil_clim")
            n = self.db.execute("SELECT COUNT(*) FROM seed.soil_clim").fetchone()[0]
            gen = self.db.execute(
                "SELECT value FROM seed.meta WHERE key = 'seed_generated'"
            ).fetchone()
            if gen:
                self.meta_set("seed_generated", gen[0])
            self.db.commit()
        finally:
            self.db.execute("DETACH DATABASE seed")
        return n

    # ------------------------------------------------------------------ métadonnées

    def meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
