"""`status` : état d'un département sur disque, jeu complet en local et dans le bucket ; `publish --dry-run`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from champis_pipeline import config, publish, status
from champis_pipeline.species import species_hash
from champis_pipeline.storage import MANIFEST_KEY, TILES_PREFIX

SPECIES = ["boletus_edulis", "cantharellus_cibarius"]


@pytest.fixture
def data(tmp_path: Path, monkeypatch):
    for name in ("DATA_DIR", "RAW_DIR", "GRID_DIR", "T_DIR", "TILES_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    return tmp_path


def _touch(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class _Bucket:
    def __init__(self, objects: dict[str, int] | None = None, manifest: dict | None = None):
        self.objects = objects or {}
        self.manifest = manifest

    def list(self, prefix):
        return {k: v for k, v in self.objects.items() if k.startswith(prefix)}

    def get_json(self, key):
        return self.manifest if key == MANIFEST_KEY else None


class _Broken:
    def list(self, prefix):
        raise ConnectionError("pas de réseau")

    def get_json(self, key):
        raise ConnectionError("pas de réseau")


def test_dept_status_counts_what_is_on_disk(data: Path):
    empty = status.dept_status("25", SPECIES, batch_status={})
    assert empty.cells() == ["25", "-", "0/6", "0/2", "-", "0/3", "incomplet"]
    assert not empty.ready

    _touch(config.RAW_DIR / "25" / "bdforet" / "extracted" / "x" / "FORMATION_VEGETALE.shp")
    for k in status.GRID_RASTERS:
        _touch(config.GRID_DIR / "25" / f"{k}.tif")
    for sid in SPECIES:
        _touch(config.T_DIR / sid / f"25-{species_hash(sid)}.tif")
    names = publish.expected_names(["25"], SPECIES)
    _touch(config.TILES_DIR / names[0])
    full = status.dept_status("25", SPECIES, batch_status={"25": {"ok": True}, "39": {"ok": False}})
    assert full.cells() == ["25", "oui", "6/6", "2/2", "ok", "1/3", "prêt"]
    assert status.dept_status("39", SPECIES, batch_status={"39": {"ok": False}}).batch == "ECHEC"

    text = status.table([full.cells(), empty.cells()], status.COLUMNS)
    lines = text.splitlines()
    assert lines[0].split() == [*status.COLUMNS[:5], "tuiles", "dept", "état"]
    assert lines[2].startswith("25    oui  6/6   2/2  ok     1/3          prêt")


def test_set_status_local_and_bucket(data: Path):
    names = publish.expected_names(["25", "39"], SPECIES)
    _touch(config.TILES_DIR / names[0], 10)
    _touch(config.TILES_DIR / names[2], 5)
    s = status.set_status(["39", "25"], SPECIES)
    assert s.depts == ["25", "39"] and s.expected == names
    assert s.local == [names[0], names[2]] and s.bucket is None

    bucket = _Bucket(
        {TILES_PREFIX + names[0]: 10, TILES_PREFIX + "autre.pmtiles": 1},
        {"generated": "2026-10-31T03:00:00+00:00"},
    )
    s = status.set_status(["25", "39"], SPECIES, bucket)
    assert s.bucket == [names[0]] and s.manifest_generated.startswith("2026-10-31")
    assert s.bucket_error is None

    s = status.set_status(["25"], SPECIES, _Broken())
    assert s.bucket is None and s.bucket_error.startswith("ConnectionError")


def test_set_status_lists_other_local_sets(data: Path):
    """Les PMTiles locaux d'autres jeux (autres départements, ou rendu antérieur : l'empreinte
    `d<…>` porte RENDER_VERSION) sont signalés avec leur taille ; pas ceux du jeu attendu ni des
    jeux d'un seul département du jeu (hors ligne de l'app)."""
    from champis_pipeline.tiles import _depts_hash

    names = publish.expected_names(["25", "39"], SPECIES)
    _touch(config.TILES_DIR / names[0], 10)
    dept_only = publish.expected_names(["25"], SPECIES)[0]
    _touch(config.TILES_DIR / dept_only, 3)
    other_depts = (
        f"T-boletus_edulis-{species_hash('boletus_edulis')}-p1.0.0-d{_depts_hash(['70'])}.pmtiles"
    )
    _touch(config.TILES_DIR / other_depts, 7)
    old_render = "T-boletus_edulis-00000000-p1.0.0-dabcdef.pmtiles"
    _touch(config.TILES_DIR / old_render, 20)
    _touch(config.TILES_DIR / "notes.txt", 1)
    s = status.set_status(["25", "39"], SPECIES)
    assert [(n, b) for n, b, _ in s.other_local] == [(old_render, 20), (other_depts, 7)]
    assert s.other_local_bytes == 27
    assert all(render is None for _, _, render in s.other_local)  # fichiers factices, illisibles
    assert status.set_status(["25"], SPECIES).other_local[0][0] == old_render


def test_publish_plan_and_dry_run(data: Path, capsys, monkeypatch):
    from champis_pipeline import cli
    from champis_pipeline.tiles import _depts_hash

    class Bucket(_Bucket):
        def upload(self, *a, **k):
            raise AssertionError("dry-run : rien ne doit être téléversé")

        put_json = delete = upload

    names = publish.expected_names(["25"], SPECIES)
    for i, n in enumerate(names):
        _touch(config.TILES_DIR / n, 100 + i)
    other = (
        f"T-boletus_edulis-{species_hash('boletus_edulis')}-p1.0.0-d{_depts_hash(['39'])}.pmtiles"
    )
    bucket = Bucket(
        {TILES_PREFIX + names[0]: 100, TILES_PREFIX + names[1]: 1, TILES_PREFIX + other: 7}
    )

    p = publish.plan(bucket, ["25"], SPECIES, prune=True)
    assert p.already == [names[0]]
    assert p.todo == [names[1], names[2]]  # taille différente, absent
    assert p.todo_bytes == 101 + 102
    assert p.stale == [TILES_PREFIX + other]
    assert publish.plan(bucket, ["25"], SPECIES, prune=False).stale == []
    with pytest.raises(FileNotFoundError):
        publish.plan(bucket, ["25", "39"], SPECIES)

    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://bucket.test")
    monkeypatch.setenv("AWS_S3_BUCKET_NAME", "champis")
    monkeypatch.setattr("champis_pipeline.storage.Bucket", lambda cfg: bucket)
    code = cli.main(
        [
            "publish",
            "--depts",
            "25",
            "--prune",
            "--dry-run",
            *[a for s in SPECIES for a in ("--species", s)],
        ]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert (
        "DRY RUN" in err
        and "2 à téléverser" in err
        and "1 déjà là" in err
        and "1 à supprimer" in err
    )
    assert f"+ {names[1]}" in err and f"= {names[0]}" in err and f"- {TILES_PREFIX}{other}" in err
    assert "rien n'a été écrit" in err


def test_cli_status_prints_table_on_stdout(data: Path, capsys):
    from champis_pipeline import cli

    _touch(config.DATA_DIR / "batch_status.json")
    (config.DATA_DIR / "batch_status.json").write_text(
        json.dumps({"25": {"ok": True}}), encoding="utf-8"
    )
    code = cli.main(["status", "--depts", "25", "39", "--no-bucket", "--species", "boletus_edulis"])
    assert code == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0].startswith("dept  raw")
    assert lines[2].startswith("25") and "ok" in lines[2] and lines[3].startswith("39")
    assert "0/2 département(s) prêts" in out
    assert "jeu complet · 2 département(s)" in err and "bucket non configuré" in err
