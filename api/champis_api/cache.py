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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from champis_pipeline.weather_store import SCHEMA

BUCKET_HOURS = 12  # deux rafraîchissements par jour (décision du 17 oct. 2026)
HISTORY_VARS = ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil")
HISTORY_KEEP_DAYS = 400  # au-delà, plus aucune fenêtre du modèle ne lit


@dataclass(frozen=True)
class StoredCellWeather:
    """Météo précalculée d'une cellule pour une espèce : les onze lignes `{"date", "by_alt"}` de
    `/weather/bbox`, le `today` du calcul (les dates sont relatives à lui) et son horodatage."""

    today: str
    computed_at: str
    stale: bool
    rows: list[dict[str, Any]]


def bucket_now(now: datetime) -> str:
    """Seau de 12 h en UTC : `2026-10-17T00` ou `2026-10-17T12`."""
    h = (now.hour // BUCKET_HOURS) * BUCKET_HOURS
    return now.astimezone(UTC).strftime("%Y-%m-%d") + f"T{h:02d}"


class WeatherCache:
    SQL_CHUNK = 500  # identifiants par requête `IN (...)`, loin sous la limite de variables SQLite

    def __init__(self, path: Path | str = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(SCHEMA)  # schéma partagé avec la base d'amorçage du pipeline
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

    # ------------------------------------------------------------------ indice par département

    META_DEPT_INDEX_AT = "dept_index_computed_at"
    META_DEPT_INDEX_TODAY = "dept_index_today"

    def put_dept_index(
        self, rows: list[tuple[str, str, str, float | None, float]], today: date, now: datetime
    ) -> None:
        """Remplace l'indice entier : (département, espèce, jour ISO, valeur ou None, couverture)."""
        self.db.execute("DELETE FROM dept_index")
        self.db.executemany("INSERT INTO dept_index VALUES (?, ?, ?, ?, ?)", rows)
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (self.META_DEPT_INDEX_AT, now.astimezone(UTC).isoformat(timespec="seconds")),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (self.META_DEPT_INDEX_TODAY, today.isoformat()),
        )
        self.db.commit()

    def dept_index(
        self, species: list[str] | None = None, day: date | None = None
    ) -> list[tuple[str, str, str, float | None, float]]:
        sql = "SELECT dept, species, day, value, coverage FROM dept_index"
        where, params = [], []
        if species:
            where.append(f"species IN ({','.join('?' * len(species))})")
            params += species
        if day is not None:
            where.append("day = ?")
            params.append(day.isoformat())
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.db.execute(sql + " ORDER BY dept, species, day", params).fetchall()

    def dept_index_meta(self) -> tuple[str, str] | None:
        """(computed_at, today) du dernier calcul, None si jamais calculé."""
        at, today = (
            self.meta_get(self.META_DEPT_INDEX_AT),
            self.meta_get(self.META_DEPT_INDEX_TODAY),
        )
        return (at, today) if at and today else None

    # ------------------------------------------------------------------ météo par cellule

    def put_cell_weather(
        self,
        entries: Iterable[tuple[str, str, bool, list[dict[str, Any]]]],
        today: date,
        now: datetime,
    ) -> int:
        """Range les lignes `M`, `M_eff`, `phi` par jour et classe d'altitude d'une cellule et
        d'une espèce, telles que `/weather/bbox` les sert : (cellule, espèce, périmé, lignes).
        Remplace l'entrée existante de la paire ; renvoie le nombre d'entrées écrites."""
        at = now.astimezone(UTC).isoformat(timespec="seconds")
        rows = [
            (cell_id, species, today.isoformat(), at, int(stale), json.dumps(day_rows))
            for cell_id, species, stale, day_rows in entries
        ]
        self.db.executemany("INSERT OR REPLACE INTO cell_weather VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.db.commit()
        return len(rows)

    def cell_weather(
        self, cell_ids: Iterable[str], species: Iterable[str]
    ) -> dict[tuple[str, str], StoredCellWeather]:
        """Les entrées précalculées pour ces cellules et ces espèces, par (cellule, espèce) ;
        absentes : jamais calculées (cellule nouvelle, indisponible à la pré-chauffe)."""
        ids = list(cell_ids)
        sp = list(species)
        if not ids or not sp:
            return {}
        out: dict[tuple[str, str], StoredCellWeather] = {}
        sp_sql = ",".join("?" * len(sp))
        for i in range(0, len(ids), self.SQL_CHUNK):
            chunk = ids[i : i + self.SQL_CHUNK]
            rows = self.db.execute(
                "SELECT cell_id, species, today, computed_at, stale, rows_json FROM cell_weather "
                f"WHERE species IN ({sp_sql}) AND cell_id IN ({','.join('?' * len(chunk))})",
                sp + chunk,
            ).fetchall()
            for cell_id, sid, today, at, stale, rows_json in rows:
                out[cell_id, sid] = StoredCellWeather(today, at, bool(stale), json.loads(rows_json))
        return out

    def cell_weather_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM cell_weather").fetchone()[0]

    # ------------------------------------------------------------------ métadonnées

    def meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
