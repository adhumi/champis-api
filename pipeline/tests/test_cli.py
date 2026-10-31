"""CLI `champis-pipeline` : chaque étape enchaîne les bonnes fonctions, affiche ses étapes `[i/n]`
sur stderr, ses données sur stdout, et rend le bon code de retour. Les étapes elles-mêmes sont
remplacées par des doublures : leur contenu est testé dans `test_pipeline_e2e.py`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from champis_pipeline import batch, cli, download, rasterize, terrain, tiles, weather_grid


@pytest.fixture
def calls(monkeypatch, tmp_path: Path) -> list[tuple]:
    """Doublures de toutes les étapes ; chaque appel est noté (nom, arguments utiles)."""
    seen: list[tuple] = []

    class Grid:
        width, height = 3812, 4620

    shp = tmp_path / "FORMATION_VEGETALE.shp"
    shp.write_text("")
    monkeypatch.setattr(download, "bdforet", lambda dept: seen.append(("bdforet", dept)) or shp)
    monkeypatch.setattr(
        download,
        "soilgrids_ph",
        lambda bbox: seen.append(("soilgrids", bbox)) or tmp_path / "ph.tif",
    )
    monkeypatch.setattr(
        download,
        "copernicus_dem_tiles",
        lambda bbox: seen.append(("dem", bbox)) or [tmp_path / "d1.tif"],
    )
    monkeypatch.setattr(rasterize, "dept_grid", lambda shp: Grid())
    monkeypatch.setattr(rasterize, "grid_bbox_4326", lambda grid: (5.7, 46.5, 7.1, 47.6))
    monkeypatch.setattr(
        rasterize,
        "host_cover",
        lambda dept, shp, grid, force: seen.append(("host_cover", dept, force)),
    )
    monkeypatch.setattr(
        rasterize, "ph", lambda dept, src, grid, force: seen.append(("ph", dept, force))
    )
    monkeypatch.setattr(
        rasterize,
        "dem_products",
        lambda dept, dem, grid, force: seen.append(("dem_products", dept, force)),
    )

    def compute(dept, sid, force=False):
        seen.append(("terrain", dept, sid, force))
        out = tmp_path / "T" / sid / f"{dept}.tif"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("t")
        return out

    monkeypatch.setattr(terrain, "compute", compute)
    monkeypatch.setattr(
        tiles, "build", lambda sid, depts, force, jobs: seen.append(("tiles", sid, depts, jobs))
    )
    monkeypatch.setattr(
        tiles, "build_alt", lambda depts, force, jobs: seen.append(("tiles_alt", depts, jobs))
    )
    monkeypatch.setattr(
        tiles,
        "build_dept",
        lambda sid, d, depts, force, jobs: seen.append(("tiles_dept", sid, d)) or shp,
    )
    monkeypatch.setattr(
        tiles,
        "build_alt_dept",
        lambda d, depts, force, jobs: seen.append(("tiles_alt_dept", d)) or shp,
    )
    monkeypatch.setattr(
        weather_grid,
        "build",
        lambda depts, species: seen.append(("weather_grid", depts, species)) or shp,
    )
    return seen


def test_all_runs_the_five_stages_in_order(calls, capsys):
    """README › `all` enchaîne download, grid, terrain, tiles, weather-grid sur un département."""
    assert cli.main(["all", "--dept", "25", "--species", "a", "--species", "b"]) == 0
    names = [c[0] for c in calls]
    assert names == [
        "bdforet",
        "soilgrids",
        "dem",  # download
        "bdforet",
        "host_cover",
        "soilgrids",
        "ph",
        "dem",
        "dem_products",  # grid
        "terrain",
        "terrain",
        "tiles",
        "tiles",
        "tiles_alt",
        "weather_grid",
    ]
    assert ("terrain", "25", "a", False) in calls and ("tiles", "b", ["25"], None) in calls
    assert ("weather_grid", ["25"], ["a", "b"]) in calls
    out, err = capsys.readouterr()
    assert out == ""
    lines = err.splitlines()
    assert lines[0] == "all --dept 25 · 2 espèce(s)"
    assert lines[1] == "[1/8] download 25"
    assert (
        "BD Forêt FORMATION_VEGETALE.shp · SoilGrids · DEM 1 dalle(s) · grille 3812x4620 px"
        in lines[2]
    )
    assert "[8/8] weather-grid" in err and lines[-1].startswith("25 prêt en ")


def test_download_grid_and_terrain_commands(calls, capsys):
    assert cli.main(["download", "--dept", "39"]) == 0
    assert [c[0] for c in calls] == ["bdforet", "soilgrids", "dem"]
    calls.clear()
    assert cli.main(["grid", "--dept", "39", "--force"]) == 0
    assert ("host_cover", "39", True) in calls and ("dem_products", "39", True) in calls
    calls.clear()
    assert cli.main(["terrain", "--dept", "39", "--species", "x"]) == 0
    assert calls == [("terrain", "39", "x", False)]
    err = capsys.readouterr().err
    assert "[1/1] terrain x" in err and "ok" in err


def test_tiles_per_dept_counts_steps(calls, capsys):
    """`tiles --per-dept` : un jeu complet puis, s'il y a plusieurs départements, un jeu par département."""
    assert (
        cli.main(["tiles", "--depts", "25", "39", "--species", "x", "--per-dept", "--jobs", "2"])
        == 0
    )
    names = [c[0] for c in calls]
    assert names == [
        "tiles",
        "tiles_alt",
        "tiles_dept",
        "tiles_alt_dept",
        "tiles_dept",
        "tiles_alt_dept",
    ]
    err = capsys.readouterr().err
    assert (
        "[1/4] tiles T x" in err
        and "[3/4] tiles département 25" in err
        and "[4/4] tiles département 39" in err
    )
    calls.clear()
    # un seul département : son jeu est déjà le jeu complet, pas d'extraction
    assert cli.main(["tiles", "--depts", "25", "--species", "x", "--per-dept"]) == 0
    assert [c[0] for c in calls] == ["tiles", "tiles_alt"]


def test_summary_and_explain_print_data_on_stdout(monkeypatch, capsys):
    from champis_pipeline import explain

    monkeypatch.setattr(terrain, "summary", lambda p: {"forest_ha": 10.0, "excellent_ha": 9.0})
    assert cli.main(["summary", "--dept", "25", "--species", "boletus_edulis"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("boletus_edulis {")
    assert json.loads(out.removeprefix("boletus_edulis ")) == {
        "forest_ha": 10.0,
        "excellent_ha": 9.0,
    }

    monkeypatch.setattr(
        explain,
        "explain",
        lambda dept, sid: {
            "excellent_share": 0.9,
            "forest_ha": 247_000.0,
            "factors": {"ph": {"mean": 0.42}},
        },
    )
    assert cli.main(["explain", "--dept", "25", "--species", "a"]) == 0
    out, err = capsys.readouterr()
    assert (
        out == ""
        and "[1/1] explain a" in err
        and "90% excellent sur 247,000 ha" in err
        and "ph 0.42" in err
    )


def test_soil_clim_parses_years(monkeypatch, capsys, tmp_path: Path):
    from champis_pipeline import soil_clim

    seen = {}

    def build(grid, years, out):
        seen.update(grid=grid, years=years, out=out)
        return 265

    monkeypatch.setattr(soil_clim, "build", build)
    assert cli.main(["soil-clim", "--years", "2016-2018", "--out", str(tmp_path / "s.db")]) == 0
    assert seen["years"] == [2016, 2017, 2018] and seen["out"] == tmp_path / "s.db"
    assert "265 cellules" in capsys.readouterr().err
    assert cli.main(["soil-clim"]) == 0 and seen["years"] is None


def test_publish_and_status_without_bucket(monkeypatch, capsys):
    for name in (
        "AWS_ENDPOINT_URL",
        "AWS_S3_BUCKET_NAME",
        "CHAMPIS_S3_ENDPOINT",
        "CHAMPIS_S3_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["publish", "--depts", "25"]) == 1
    err = capsys.readouterr().err
    assert "ECHEC bucket non configuré" in err and ".env.example" in err


def test_batch_prints_one_line_per_department_and_fails_on_errors(
    monkeypatch, capsys, tmp_path: Path
):
    """`batch` : ligne par département à l'arrivée (compteur), résumé, code 1 et conseil de reprise."""
    results = [
        batch.DeptResult("25", True, "grille 3812x4620 px", 252.0, "2026-10-31T10:00:00+00:00"),
        batch.DeptResult(
            "39",
            False,
            "HTTPError: 503 SoilGrids\n  trace ligne 1",
            41.0,
            "2026-10-31T10:01:00+00:00",
        ),
    ]

    def fake_batch(depts, species, jobs, force, report):
        for r in results:
            report(r)
        return {r.dept: {"ok": r.ok} for r in results}

    monkeypatch.setattr(batch, "batch", fake_batch)
    monkeypatch.setattr(batch, "load_status", lambda path=None: {})
    assert cli.main(["batch", "--depts", "25", "39", "--jobs", "2"]) == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines[0] == "batch · 2 département(s), 0 déjà ok, 2 à faire, 2 en parallèle"
    assert lines[1].split() == [
        "25",
        "ok",
        "4",
        "min",
        "12",
        "s",
        "grille",
        "3812x4620",
        "px",
        "[1/2]",
    ]
    assert (
        lines[2].split()[:2] == ["39", "ECHEC"]
        and "503 SoilGrids" in lines[2]
        and "[2/2]" in lines[2]
    )
    assert lines[3].startswith("      - ") and "trace ligne 1" in lines[3]
    assert any(line.startswith("1/2 départements ok") for line in lines)
    assert "ECHEC 1 échec(s) : 39" in err and "relance la même commande" in err

    results[1] = batch.DeptResult("39", True, "ok", 1.0, "x")
    assert cli.main(["batch", "--depts", "25", "39"]) == 0


def test_run_dept_captures_failures_as_messages(monkeypatch, calls):
    """Skill geo-pipeline : un département en échec n'arrête pas le lot, l'erreur est gardée en message."""
    r = batch.run_dept("25", ["a"], force=False)
    assert r.ok and r.message == "grille 3812×4620 px" and r.dept == "25" and r.seconds >= 0
    assert ("terrain", "25", "a", False) in calls

    def boom(dept):
        raise ConnectionError("Géoplateforme injoignable")

    monkeypatch.setattr(download, "bdforet", boom)
    r = batch.run_dept("39", None, force=True)
    assert not r.ok and r.message.startswith("ConnectionError: Géoplateforme injoignable")
    assert "Traceback" in r.message and r.finished.endswith("+00:00")


def test_parse_depts_all_and_padding():
    """`--depts all` = les 96 départements de BD Forêt ; `1` → `01`, `2a` → `2A` ; inconnu = erreur."""
    assert len(batch.parse_depts(["all"])) == 96 and batch.parse_depts(["all"])[0] == "01"
    assert batch.parse_depts(["1", "2a", "2B", "95"]) == ["01", "2A", "2B", "95"]
    with pytest.raises(SystemExit, match="département inconnu : 20"):
        batch.parse_depts(["20"])  # la Corse est 2A/2B
    with pytest.raises(SystemExit):
        batch.parse_depts(["971"])  # pas d'outre-mer dans BD Forêt V2
