"""Base du cache verrouillée par un autre processus (incident du 21 nov. 2026 : `GET /coins`
répondait 500 « database is locked » à chaque appel) : une écriture qui échoue ne laisse pas de
transaction ouverte, et la prévision reçue d'Open-Meteo est servie même si elle n'a pas pu être
rangée."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest
from champis_api.cache import WeatherCache, bucket_now
from conftest import NOW, make_client

CELL = "c_0930_6680"


@pytest.fixture
def short_wait(monkeypatch):
    monkeypatch.setattr(WeatherCache, "BUSY_TIMEOUT_S", 0.05)


@contextmanager
def write_lock(path):
    """Un autre processus au milieu d'une longue transaction d'écriture (import d'un instantané)."""
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_failed_write_leaves_no_open_transaction(tmp_path, short_wait):
    path = tmp_path / "cache.db"
    cache, other_worker = WeatherCache(path, shared=True), WeatherCache(path, shared=True)
    with write_lock(path), pytest.raises(sqlite3.OperationalError, match="locked"):
        cache.put(CELL, "forecast", "b", {"v": 1}, NOW)
    assert not cache.db.in_transaction
    # la séquence qui bloquait le worker : une lecture, l'écriture d'un autre worker, puis
    # l'écriture de celui-ci, refusée à chaque fois tant que la transaction restait ouverte
    assert cache.get(CELL, "forecast", "b") is None
    other_worker.put("c_0935_6680", "forecast", "b", {}, NOW)
    cache.put(CELL, "forecast", "b", {"v": 2}, NOW)
    assert other_worker.get(CELL, "forecast", "b") == {"v": 2}


def test_error_in_the_middle_of_a_write_is_rolled_back(tmp_path):
    cache = WeatherCache(tmp_path / "cache.db", shared=True)
    day = {"t_mean": 1, "t_min": 0, "t_max": 2, "rain": 0, "snow": 0, "et0": 1, "soil": 0.3}
    with pytest.raises(ValueError):
        cache.put_history(CELL, {"2026-11-01": day, "2026-11-02": day | {"rain": "x"}}, "forecast")
    assert not cache.db.in_transaction
    assert cache.history_days(CELL) == 0


@pytest.mark.asyncio
async def test_forecast_is_served_when_the_cache_cannot_be_written(
    tmp_path, grid, scenario, short_wait, caplog
):
    path = tmp_path / "cache.db"
    client = make_client(scenario, WeatherCache(path, shared=True))
    cells = [grid.get(CELL), grid.get("c_0935_6680")]
    with write_lock(path):
        res = await client.fetch_cells_partial(cells)
    assert set(res.raws) == {c.id for c in cells} and not res.unavailable
    assert not any(r.stale for r in res.raws.values())
    assert caplog.text.count("cache météo non écrit") == 1  # une attente par appel, pas par cellule
    assert client.cache.get(CELL, "forecast", bucket_now(NOW)) is None
    # verrou relâché : l'appel suivant redemande la prévision et la range
    await client.fetch_cells_partial(cells)
    assert client.cache.get(CELL, "forecast", bucket_now(NOW)) is not None


def test_opening_waits_for_a_worker_that_holds_the_database(tmp_path, short_wait):
    """Deux workers démarrent ensemble : celui qui trouve la base tenue plus longtemps que
    l'attente d'un essai recommence, au lieu de faire échouer le démarrage du service."""
    path = tmp_path / "cache.db"
    WeatherCache(path).close()  # pas encore en WAL : l'ouverture partagée doit attendre son tour
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN EXCLUSIVE")
    release = threading.Timer(0.3, lambda: other.execute("ROLLBACK"))
    release.start()
    try:
        cache = WeatherCache(path, shared=True)  # six essais de 50 ms avant le relâchement
    finally:
        release.join()
        other.close()
    cache.put(CELL, "forecast", "b", {"v": 1}, NOW)
    assert cache.get(CELL, "forecast", "b") == {"v": 1}


def test_opening_gives_up_after_the_deadline(tmp_path, short_wait, monkeypatch):
    monkeypatch.setattr(WeatherCache, "OPEN_TIMEOUT_S", 0.1)
    path = tmp_path / "cache.db"
    WeatherCache(path).close()  # pas encore en WAL : l'ouverture partagée doit attendre son tour
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN EXCLUSIVE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        WeatherCache(path, shared=True)
    other.execute("ROLLBACK")
