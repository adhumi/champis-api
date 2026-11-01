"""Contrats de la spec partagés par tous les composants (fichiers, palette, noms, versions).

Chaque test cite la section de la spec ou du schéma qu'il protège. Ces fichiers sont lus tels
quels par le pipeline, l'API, le site et l'app : une dérive ici casse les clients sans erreur.
"""

from __future__ import annotations

import ast
import json
import re
import runpy
from pathlib import Path

import pytest
from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import (
    ASPECT_CLASSES,
    COVER_CLASSES,
    ROOT,
    all_species_hash,
    fingerprint_bytes,
    host_vocabulary,
    list_species,
    load_mapping,
    load_palette,
    load_species,
    species_hash,
)
from champis_pipeline.storage import MANIFEST_VERSION, TILES_PREFIX, build_manifest, parse_tile_name

# ------------------------------------------------------------------ Scoring › restitution


def test_palette_matches_the_spec_thresholds():
    """Spec › Scoring › Restitution : ≥ 0,75 excellent · 0,55 bon · 0,35 moyen · 0,15 faible · sinon très faible.

    Règle 10 du modèle : ne pas en inventer d'autres ; la palette est la seule source des clients.
    """
    palette = load_palette()
    assert palette["version"] == 1
    mins = [t["min"] for t in palette["thresholds"]]
    assert mins == [0.75, 0.55, 0.35, 0.15, 0.0]
    assert [t["label"] for t in palette["thresholds"]] == [
        "excellent",
        "bon",
        "moyen",
        "faible",
        "tres_faible",
    ]
    for t in palette["thresholds"]:
        assert re.fullmatch(r"#[0-9A-F]{6}", t["hex"]), t
        assert t["fr"]
    # transparence totale pour le nodata : le pixel sans donnée ne porte jamais une valeur inventée
    assert palette["nodata_hex"] == "#00000000"


def test_palette_tile_encodings_match_the_pipeline():
    """Skill geo-pipeline : gris = T × 254, 255 = nodata ; couche A par classes de 300 m."""
    from champis_pipeline.scoring import NODATA_U8
    from champis_pipeline.tiles import ALT_CLASS_M, NODATA
    from champis_pipeline.weather_grid import ALT_CLASS_M as GRID_ALT_CLASS_M

    palette = load_palette()
    assert palette["tile_encoding"] == {"channel": "luminance", "scale": 254, "nodata": 255}
    assert palette["alt_encoding"] == {"channel": "luminance", "class_m": 300, "nodata": 255}
    assert NODATA == NODATA_U8 == palette["tile_encoding"]["nodata"]
    assert ALT_CLASS_M == GRID_ALT_CLASS_M == palette["alt_encoding"]["class_m"]


# ------------------------------------------------------------------ Modèle d'espèce


def test_species_files_pass_their_validator(capsys):
    """Spec › Modèle d'espèce : `species/validate.py` est le contrat (structure, poids, trapèzes, refs)."""
    result = runpy.run_path(str(ROOT / "species" / "validate.py"), run_name="not_main")
    assert result["main"]() == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "ok  " in out and "32 codes TFV" in out


def test_every_species_declares_group_names_and_weights_summing_to_one():
    """SCHEMA.md : `group` (règle 9), noms fr/la, poids terrain et météo de somme 1, trapèzes ordonnés."""
    ids = list_species()
    assert len(ids) >= 7 and ids == sorted(ids)
    groups = set()
    for sid in ids:
        sp = load_species(sid)
        assert sp["id"] == sid and sp["schema_version"] == 1
        assert sp["group"]["id"] and sp["group"]["fr"]
        groups.add(sp["group"]["id"])
        assert sp["names"]["fr"] and sp["names"]["la"]
        assert sum(sp["terrain"]["weights"].values()) == pytest.approx(1.0)
        assert set(sp["terrain"]["weights"]) == {"cover", "ph", "altitude", "aspect", "slope"}
        assert sum(sp["weather"]["weights"].values()) == pytest.approx(1.0)
        assert set(sp["weather"]["weights"]) == {"temp", "rain", "trigger", "soil_moisture"}
        for key in ("ph", "altitude_m", "slope_deg"):
            t = sp["terrain"][key]
            assert t["min"] <= t["opt_low"] <= t["opt_high"] <= t["max"], (sid, key)
        assert set(sp["terrain"]["aspect"]) <= set(ASPECT_CLASSES)
        assert set(sp["terrain"]["cover"]) == set(COVER_CLASSES)
        assert 0 < sp["weather"]["memory"]["floor"] < 1  # module, n'annule jamais
        assert 0 < sp["weather"]["frost"]["factor"] < 1
    assert {"cepes", "girolles"} <= groups  # spec › Objectif : cèpes et girolles au lancement


def test_mapping_covers_the_32_tfv_codes_with_known_hosts_and_covers():
    """Skill geo-pipeline : 32 codes TFV → vocabulaire d'hôtes ; un code hors table est une erreur, pas un défaut."""
    mapping = load_mapping()
    codes = mapping["bdforet_v2"]
    assert len(codes) == 32
    vocab = set(host_vocabulary())
    for code, entry in codes.items():
        assert code == code.strip(), code
        assert entry["host"] in vocab, (code, entry["host"])
        assert entry["cover"] in COVER_CLASSES, (code, entry["cover"])
    assert "FF1-00" in codes and "FF1-00-00" in codes  # distincts : comparaison exacte
    assert codes["FF1-00"]["host"] != codes["FF1-00-00"]["host"]
    # repli Corine hors BD Forêt : toute clé renvoie un hôte du vocabulaire (herbacée comprise)
    for code, entry in mapping["clc_2018_fallback"].items():
        if isinstance(entry, dict):  # les autres clés sont de la documentation
            assert entry["host"] in vocab, code
    # chaque espèce connaît chaque hôte du vocabulaire, ou a un `default`
    for sid in list_species():
        hosts = load_species(sid)["terrain"]["hosts"]
        missing = vocab - set(hosts)
        assert not missing or "default" in hosts, (sid, missing)


def test_no_species_value_lives_in_the_code():
    """Skill champis-model : « si tu écris `if species == "boletus_edulis"`, arrête-toi ».

    Aucun identifiant d'espèce dans les chaînes du code du pipeline et de l'API (hors docstrings et
    hors `vectors.py`, qui fixe les cas de parité). Les tests, eux, ont le droit.
    """
    ids = set(list_species())
    offenders = []
    for folder in ("pipeline/champis_pipeline", "api/champis_api"):
        for path in (ROOT / folder).rglob("*.py"):
            if path.name == "vectors.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(
                    node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                ):
                    body = node.body
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                    ):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ) and any(sid in node.value for sid in ids):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.value!r}")
    assert offenders == []


# ------------------------------------------------------------------ Pipeline › tuiles et manifeste


def test_scoring_version_and_species_hash_formats():
    """Noms de tuiles immuables : `p<version semver>` et hash espèce sur 8 hexadécimaux ; un octet
    changé dans un fichier espèce change son hash (et donc le nom du PMTiles)."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", SCORING_VERSION)
    for sid in list_species():
        assert re.fullmatch(r"[0-9a-f]{8}", species_hash(sid))
    assert re.fullmatch(r"[0-9a-f]{8}", all_species_hash())
    assert len({species_hash(s) for s in list_species()}) == len(list_species())


def test_tile_names_parse_and_manifest_marks_single_department_sets():
    """`T-<espèce>-<hash>-p<version>-d<hash départements>.pmtiles`, `A-<classe>m-d<hash>.pmtiles` ;
    manifeste `GET /tiles` (api/README) : `version`, `generated`, `depts_by_hash`, `tiles[]` avec
    `dept` pour un jeu d'un seul département (décision du 31 oct. 2026, hors ligne de l'app)."""
    assert parse_tile_name("T-boletus_edulis-af34c16e-p1.0.0-da1b2c3.pmtiles") == {
        "kind": "T",
        "species": "boletus_edulis",
        "hash": "af34c16e",
        "scoring_version": "1.0.0",
        "depts_hash": "a1b2c3",
    }
    assert parse_tile_name("A-300m-da1b2c3.pmtiles") == {
        "kind": "A",
        "class_m": 300,
        "depts_hash": "a1b2c3",
    }
    for bad in (
        "T-boletus_edulis-af34c16e-da1b2c3.pmtiles",
        "manifest.json",
        "T-x-1234-p1-d1.pmtiles",
    ):
        assert parse_tile_name(bad) is None, bad

    objects = {
        TILES_PREFIX + "T-boletus_edulis-af34c16e-p1.0.0-da1b2c3.pmtiles": 10,
        TILES_PREFIX + "T-boletus_edulis-af34c16e-p1.0.0-d222222.pmtiles": 3,
        TILES_PREFIX + "A-300m-d222222.pmtiles": 1,
        TILES_PREFIX + "manifest.json": 99,
        TILES_PREFIX + "autre.txt": 1,
    }
    manifest = build_manifest(
        objects, {"a1b2c3": ["25", "39"], "222222": ["25"]}, "2026-10-31T03:00:00+00:00"
    )
    assert manifest["version"] == MANIFEST_VERSION and manifest["generated"].startswith(
        "2026-10-31"
    )
    names = [t["name"] for t in manifest["tiles"]]
    assert names == sorted(names) and "manifest.json" not in names and "autre.txt" not in names
    by_name = {t["name"]: t for t in manifest["tiles"]}
    assert by_name["A-300m-d222222.pmtiles"]["dept"] == "25"
    assert by_name["T-boletus_edulis-af34c16e-p1.0.0-d222222.pmtiles"]["dept"] == "25"
    assert "dept" not in by_name["T-boletus_edulis-af34c16e-p1.0.0-da1b2c3.pmtiles"]
    assert by_name["T-boletus_edulis-af34c16e-p1.0.0-da1b2c3.pmtiles"]["bytes"] == 10
    assert by_name["A-300m-d222222.pmtiles"]["key"] == TILES_PREFIX + "A-300m-d222222.pmtiles"


def test_committed_weather_grid_has_the_documented_shape():
    """Spec › Sources : `{version, cell_m: 5000, cells: [{id, lat, lon, alt_classes, elevation_m}]}`,
    `id` = coin SO en km (`c_0930_6680`), `alt_classes` = centres des classes de 300 m."""
    raw = json.loads((ROOT / "data" / "weather_grid.json").read_text(encoding="utf-8"))
    assert raw["version"] in (1, 2) and raw["cell_m"] == 5000
    assert raw["cells"], "grille vide"
    for cell in raw["cells"][:50]:
        assert re.fullmatch(r"c_\d{4}_\d{4}", cell["id"]), cell["id"]
        assert 41 <= cell["lat"] <= 51.5 and -5.5 <= cell["lon"] <= 10
        assert cell["alt_classes"] == sorted(cell["alt_classes"])
        for center in cell["alt_classes"]:
            assert center % 300 == 150, center  # centre de [300k, 300k + 300[
        assert cell["elevation_m"] >= 0
    if raw["version"] == 2:
        assert raw["species_hash"] and raw["alt_class_m"] == 300
        assert all("t_sum" in c for c in raw["cells"])


def test_repo_layout_matches_claude_md():
    """CLAUDE.md › Structure : les dossiers que les skills et le Makefile supposent existent."""
    for name in (
        "species",
        "mapping",
        "shared/vectors",
        "shared/palette.json",
        "pipeline",
        "api",
        "web",
        "Makefile",
    ):
        assert (ROOT / name).exists(), name
    assert (ROOT / ".env.example").exists()
    assert not (Path(ROOT) / ".env").exists() or ".env" in (ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )


def test_shared_texts_index_and_species_about():
    """Spec › App iOS (lot 4) : `shared/texts/index.json` liste les articles de Comprendre et chaque
    fichier existe avec un titre de niveau 1 ; chaque espèce porte un bloc `about` sourcé."""
    texts = ROOT / "shared" / "texts"
    index = json.loads((texts / "index.json").read_text(encoding="utf-8"))
    assert index["version"] == 1
    ids = [a["id"] for a in index["articles"]]
    assert ids == [
        "comment-ca-marche",
        "donnees-et-sources",
        "especes",
        "cueillir-sans-nuire",
        "confidentialite",
    ]
    for article in index["articles"]:
        body = (texts / article["file"]).read_text(encoding="utf-8")
        assert body.startswith("# " + article["title"] + "\n"), article["id"]
    assert any(a.get("species") for a in index["articles"])
    assert "pharmacien" in (texts / "especes.md").read_text(encoding="utf-8")

    for path in sorted((ROOT / "species").glob("*.json")):
        sp = json.loads(path.read_text(encoding="utf-8"))
        about = sp["about"]
        assert about["confusions"], path.stem
        known = set(sp["references"]) | set(about["references"])
        for confusion in about["confusions"]:
            assert confusion["refs"] and all(r in known for r in confusion["refs"]), path.stem
        assert about["photo"] is None or {"url", "page", "author", "license"} <= set(about["photo"])


def test_species_fingerprint_ignores_about():
    """Spec › Modèle d'espèce : `about` est exclu de l'empreinte, relire une fiche ne renomme rien."""
    raw = (ROOT / "species" / "boletus_edulis.json").read_bytes()
    data = json.loads(raw)
    about = data.pop("about")
    bare = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert fingerprint_bytes(raw) == bare == fingerprint_bytes(bare)
    data["about"] = {**about, "summary": "relecture d'Adrien"}
    reread = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert fingerprint_bytes(reread) == bare
    # l'empreinte publiée (vecteurs, tuiles) est bien celle du modèle seul
    vectors = json.loads(
        (ROOT / "shared" / "vectors" / "trapezoid.json").read_text(encoding="utf-8")
    )
    assert all_species_hash() == vectors["generated_by"]["species_hash"]
