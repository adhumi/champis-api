"""Contrôle GBIF depuis le backoffice (spec › Backoffice › Espèces, 12 nov. 2026) : l'outil `check`
du catalogue, sa variante lisière validée entière ou pas du tout, la ligne de commande qu'il
assemble, et les rapports du bucket listés dans la fiche de l'espèce."""

from __future__ import annotations

import pytest
from champis_api import jobs, tools
from champis_api.admin import species as view
from conftest import MemoryBucket

CEPE = "boletus_edulis"


def test_check_tool_is_in_the_catalogue_and_builds_its_command():
    assert "check" in jobs.KINDS and "check" in jobs.PIPELINE_KINDS
    described = {t["kind"]: t for t in tools.describe()}
    dept = next(f for f in described["check"]["fields"] if f["name"] == "dept")
    assert "84" in dept["choices"] and "2A" in dept["choices"] and dept["default"] == "25"
    # sans variante : juste le département, l'espèce et le dépôt des rapports
    argv = tools.command("check", {"dept": "84", "species": CEPE})
    assert argv[2:6] == ["champis_api.cli", "-q", "pipeline", "check"]
    assert argv[6:] == ["--dept", "84", "--species", CEPE, "--to-bucket"]
    # avec la variante : `--edge a,b,c,d --edge-weight w`
    argv = tools.command(
        "check",
        {
            "dept": "84",
            "species": CEPE,
            "edge_min": "0",
            "edge_opt_low": "0",
            "edge_opt_high": "100",
            "edge_max": "400",
            "edge_weight": "0.1",
            "to_bucket": False,
            "force": True,
        },
    )
    assert argv[6:] == [
        "--dept",
        "84",
        "--species",
        CEPE,
        "--force",
        "--edge",
        "0,0,100,400",
        "--edge-weight",
        "0.1",
    ]
    normalized = tools.validate("check", {"dept": "84"})
    assert normalized["edge_min"] == "" and normalized["edge_weight"] == ""


def test_check_variant_comes_whole_or_not_at_all():
    with pytest.raises(tools.ToolError, match="quatre bornes et le poids"):
        tools.validate("check", {"dept": "84", "edge_min": "0", "edge_weight": "0.1"})
    with pytest.raises(tools.ToolError, match="croissantes"):
        tools.validate(
            "check",
            {
                "dept": "84",
                "edge_min": "50",
                "edge_opt_low": "0",
                "edge_opt_high": "100",
                "edge_max": "400",
                "edge_weight": "0.1",
            },
        )
    with pytest.raises(tools.ToolError, match=r"au plus 0\.5"):
        tools.validate(
            "check",
            {
                "dept": "84",
                "edge_min": "0",
                "edge_opt_low": "0",
                "edge_opt_high": "100",
                "edge_max": "400",
                "edge_weight": "0.9",
            },
        )
    with pytest.raises(tools.ToolError, match="valeur inconnue"):
        tools.validate("check", {"dept": "20"})


def test_reports_of_the_bucket_are_listed_per_species():
    """Les rapports `reports/<espèce>-<dept>[-variante].html` du bucket, avec les chiffres de leur
    `.json` ; une tâche `check` nommant l'espèce la concerne."""
    b = MemoryBucket()
    b.objects["reports/boletus_edulis-84.html"] = b"<html>"
    b.put_json(
        "reports/boletus_edulis-84.json",
        {"species": CEPE, "dept": "84", "variant": None, "auc": 0.71, "n_in_forest": 80},
    )
    b.objects["reports/boletus_edulis-84-edge-0-0-100-400-w0.1.html"] = b"<html>"
    b.put_json(
        "reports/boletus_edulis-84-edge-0-0-100-400-w0.1.json",
        {
            "species": CEPE,
            "dept": "84",
            "variant": "edge-0-0-100-400-w0.1",
            "auc": 0.74,
            "n_in_forest": 80,
            "baseline": {"auc": 0.71},
            "entry": {"met": True, "evaluated": True, "auc_gain": 0.03},
        },
    )
    b.objects["reports/lactarius_deliciosus-84.html"] = b"<html>"  # sans fiche JSON : lu au nom
    b.objects["reports/inconnu-84.html"] = b"<html>"  # ni espèce connue ni fiche : ignoré
    reports = view.list_reports(b)
    assert [r["name"] for r in reports] == [
        "boletus_edulis-84-edge-0-0-100-400-w0.1.html",
        "boletus_edulis-84.html",
        "lactarius_deliciosus-84.html",
    ]
    variant = reports[0]
    assert variant["species"] == CEPE and variant["dept"] == "84" and variant["auc"] == 0.74
    assert variant["baseline_auc"] == 0.71 and variant["entry"]["met"]
    assert reports[2]["species"] == "lactarius_deliciosus" and reports[2]["dept"] == "84"
    assert view._concerns({"kind": "check", "args": {"species": CEPE}}, CEPE, ["cepes"])
    assert not view._concerns({"kind": "check", "args": {"species": "lepista_nuda"}}, CEPE, [])
