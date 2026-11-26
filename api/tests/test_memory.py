"""Mémoire du conteneur (cgroup) et rendu du cache de fichiers après l'import du cache publié."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from champis_api import app as app_module
from champis_api import memory
from champis_api.cache import WeatherCache
from champis_api.snapshot import publish_cache
from conftest import MemoryBucket


def test_container_memory_reads_cgroup_v2(tmp_path: Path):
    (tmp_path / "memory.current").write_text("2441699328\n")
    (tmp_path / "memory.stat").write_text("anon 612000000\nfile 1761000000\nkernel 9000\n")
    assert memory.container_memory(tmp_path) == {
        "current": 2441699328,
        "anon": 612000000,
        "file": 1761000000,
    }
    assert memory.container_memory(tmp_path / "absent") is None  # poste de développement
    (tmp_path / "memory.current").write_text("max\n")
    assert memory.container_memory(tmp_path) is None


def test_drop_file_cache_advises_the_whole_file_and_never_raises(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        os,
        "posix_fadvise",
        lambda fd, off, n, advice: calls.append((off, n, advice)),
        raising=False,
    )
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    f = tmp_path / "base.db"
    f.write_bytes(b"x" * 4096)
    assert memory.drop_file_cache(f) is True
    assert calls == [(0, 0, 4)] and f.read_bytes() == b"x" * 4096
    assert memory.drop_file_cache(tmp_path / "absent.db") is False

    def refused(*a):
        raise OSError("refusé")

    monkeypatch.setattr(os, "posix_fadvise", refused, raising=False)
    assert memory.drop_file_cache(f) is False
    monkeypatch.delattr(os, "posix_fadvise")
    assert memory.drop_file_cache(f) is False  # plateforme sans posix_fadvise


def test_refresh_drops_the_file_cache_only_after_an_import(tmp_path: Path, monkeypatch):
    dropped = []
    monkeypatch.setattr(app_module, "drop_file_cache", lambda p: dropped.append(p))
    bucket = MemoryBucket()
    cron = WeatherCache(tmp_path / "cron.db", shared=True)
    cron.put_soil_clim("c_0930_6680", {"10": [0.1] * 11})
    publish_cache(bucket, cron, tmp_path / "work")
    cron.close()
    api_db = tmp_path / "vol" / "weather_cache.db"
    api_db.parent.mkdir()
    assert app_module._refresh_cache(api_db, bucket, api_db.parent) is not None
    assert dropped == [api_db]
    assert app_module._refresh_cache(api_db, bucket, api_db.parent) is None  # rien de nouveau
    assert dropped == [api_db]


@pytest.mark.skipif(not hasattr(os, "posix_fadvise"), reason="Linux seulement")
def test_drop_file_cache_for_real(tmp_path: Path):
    f = tmp_path / "base.db"
    f.write_bytes(os.urandom(1 << 16))
    assert memory.drop_file_cache(f) is True
