"""`clean` : occupation du dossier des données et retrait de ce qui est périmé, rien d'autre."""

from __future__ import annotations

from pathlib import Path

import pytest
from champis_pipeline import clean, config
from champis_pipeline import cli as pipeline_cli
from champis_pipeline.publish import expected_names
from champis_pipeline.species import list_species, species_hash

DEPTS = ["25", "39"]


@pytest.fixture
def data(tmp_path: Path, monkeypatch):
    for name, folder in (
        ("DATA_DIR", "."),
        ("RAW_DIR", "raw"),
        ("GRID_DIR", "grid"),
        ("T_DIR", "T"),
        ("VARIANTS_DIR", "T-variants"),
        ("TILES_DIR", "tiles"),
    ):
        monkeypatch.setattr(config, name, tmp_path / folder)
    return tmp_path


def _touch(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _fill(data: Path) -> dict[str, list[Path]]:
    sid = list_species()[0]
    h = species_hash(sid)
    france = expected_names(DEPTS, list_species(), groups=True)
    one = expected_names(["25"], list_species(), groups=True)
    keep = [
        _touch(data / "raw" / "25" / "bdforet.7z", 500),
        _touch(data / "grid" / "25" / "dem.tif", 300),
        _touch(data / "T" / sid / f"25-{h}.tif", 100),
        _touch(data / "tiles" / france[0], 40),
        _touch(data / "tiles" / france[-1], 40),  # un G du jeu complet
        _touch(data / "tiles" / one[0], 40),  # jeu d'un seul département (hors ligne de l'app)
        _touch(data / "weather_seed.db", 7),
    ]
    stale = [
        _touch(data / "T" / sid / "25-00000000.tif", 60),  # ancienne empreinte
        _touch(data / "T" / "espece_retiree" / "25-abcdef12.tif", 30),  # plus dans le référentiel
        _touch(data / "tiles" / f"T-{sid}-00000000-p1.0-d123456.pmtiles", 20),
        _touch(data / "tiles" / "A-300m-d123456.pmtiles", 10),  # autre jeu de départements
        _touch(data / "T-variants" / sid / "25-variant.tif", 5),
    ]
    return {"keep": keep, "stale": stale}


def test_folder_sizes_by_top_level_entry_biggest_first(data: Path):
    _fill(data)
    sizes = clean.folder_sizes()
    assert list(sizes)[:2] == ["raw", "grid"]
    assert sizes == {
        "raw": 500,
        "grid": 300,
        "T": 190,
        "tiles": 150,
        "weather_seed.db": 7,
        "T-variants": 5,
    }
    assert clean.folder_sizes(data / "absent") == {}


def test_plan_names_only_what_no_step_reads_anymore(data: Path):
    files = _fill(data)
    p = clean.plan(DEPTS)
    assert {f.name for f, _ in p.stale_t} == {"25-00000000.tif", "25-abcdef12.tif"}
    assert len(p.other_tiles) == 2 and len(p.variants) == 1
    assert p.total_bytes == 60 + 30 + 20 + 10 + 5
    assert all(f.exists() for f in files["keep"] + files["stale"])  # un plan n'écrit rien


def test_apply_removes_the_plan_and_nothing_else(data: Path):
    files = _fill(data)
    assert clean.apply(clean.plan(DEPTS)) == 125
    assert all(f.exists() for f in files["keep"])
    assert not any(f.exists() for f in files["stale"])
    assert not (data / "T" / "espece_retiree").exists()  # dossier vidé : retiré
    assert clean.plan(DEPTS).items == []
    assert clean.apply(clean.plan(DEPTS)) == 0


def test_cli_is_dry_by_default(data: Path, monkeypatch, capsys):
    from champis_pipeline import batch

    monkeypatch.setattr(batch, "DEPTS_ALL", DEPTS)
    monkeypatch.setenv("CHAMPIS_LOG_DIR", str(data / "logs"))
    files = _fill(data)
    assert pipeline_cli.main(["clean"]) == 0
    assert all(f.exists() for f in files["stale"])
    assert "rien n'est effacé sans --apply" in capsys.readouterr().err
    assert pipeline_cli.main(["clean", "--apply"]) == 0
    assert not any(f.exists() for f in files["stale"])
    assert all(f.exists() for f in files["keep"])
