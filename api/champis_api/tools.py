"""Catalogue des outils que le backoffice peut lancer (spec › Backoffice › Tâches, lot BO3).

Chaque outil est une commande `champis-api` ou `champis-pipeline` dont les options sont bornées
ici : la page n'envoie que des champs nommés, validés et renvoyés sous une forme normalisée, et
c'est ce module qui construit la ligne de commande. Rien d'autre ne peut être exécuté depuis le
backoffice. Le service d'exécution (`worker.py`) lance la commande en sous-processus avec
`CHAMPIS_JOB_ID`, et la commande s'inscrit elle-même dans le journal (`jobs.register`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from champis_pipeline.batch import parse_depts
from champis_pipeline.species import list_species

from . import jobs

RENDER_ZOOMS = (9, 10)
ALL = "all"
PIPELINE = "pipeline"  # module : `champis-api pipeline <sorte> …` (journal des tâches compris)


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # bool | number | choice | depts (codes ou `all`) | species (ids, vide = toutes)
    label: str
    default: Any = None
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    flag: str | None = None  # option de la ligne de commande ; None = `--<name>`

    def option(self) -> str:
        return self.flag or f"--{self.name.replace('_', '-')}"


@dataclass(frozen=True)
class Tool:
    kind: str
    module: str  # champis_api.cli | champis_pipeline.cli
    duration: str
    needs: str
    fields: tuple[Field, ...] = ()
    exclusive: bool = False  # jamais deux à la fois, cron compris
    min_free_gb: float = 0.0  # le worker refuse de démarrer sous ce seuil sur son volume
    confirm: str = ""  # la page demande confirmation avant de lancer, avec ce texte

    @property
    def label(self) -> str:
        return jobs.KINDS[self.kind]


def _render_species() -> tuple[str, ...]:
    return (ALL, *list_species())


TOOLS: dict[str, Tool] = {
    "warm": Tool(
        "warm",
        "champis_api.cli",
        "≈ 40 min pour la France",
        "bucket, clé Open-Meteo",
        (
            Field("pull", "bool", "reprendre d'abord le cache publié", True),
            Field("publish", "bool", "publier l'instantané à la fin", True),
            Field(
                "interval", "number", "secondes entre deux requêtes", 2.0, minimum=0.0, maximum=10.0
            ),
        ),
        exclusive=True,
    ),
    "notify": Tool(
        "notify",
        "champis_api.cli",
        "≈ 1 min",
        "Postgres, APNs",
        (Field("pull", "bool", "reprendre d'abord le cache publié", True),),
        exclusive=True,
    ),
    "publish-cache": Tool(
        "publish-cache", "champis_api.cli", "quelques minutes", "bucket ; le cache local du worker"
    ),
    "publish-seed": Tool(
        "publish-seed",
        "champis_api.cli",
        "quelques minutes",
        "bucket ; data/weather_seed.db du worker",
    ),
    "seed": Tool(
        "seed",
        "champis_api.cli",
        "heures à jours selon le quota ; reprise automatique",
        "clé Open-Meteo (archive : plan Professional)",
        (
            Field(
                "climatology", "bool", "aussi la climatologie des cellules qui n'en ont pas", False
            ),
            Field(
                "interval", "number", "secondes entre deux requêtes", 1.0, minimum=0.0, maximum=10.0
            ),
        ),
        exclusive=True,
    ),
    "soil-clim": Tool(
        "soil-clim",
        PIPELINE,
        "heures (Climate Data Store)",
        "compte CDS (CDSAPI_URL, CDSAPI_KEY)",
        exclusive=True,
    ),
    "render": Tool(
        "render",
        "champis_api.cli",
        "1 à 5 min par espèce",
        "bucket (PMTiles T du jeu France publiés)",
        (
            Field("species", "choice", "espèce", ALL, choices=None),
            Field("zoom", "choice", "niveau", "9", choices=tuple(str(z) for z in RENDER_ZOOMS)),
            Field("force", "bool", "refaire même si la carte existe", False),
        ),
    ),
    # lot BO4 : le pipeline terrain sur le volume du service d'exécution
    "status": Tool(
        "status",
        PIPELINE,
        "secondes",
        "lecture seule : disque du worker et bucket",
        (
            Field("depts", "depts", "départements (codes ou all)", ALL),
            Field("groups", "bool", "compter aussi les G", True),
        ),
    ),
    "batch": Tool(
        "batch",
        PIPELINE,
        "≈ 15 min par département, reprise automatique",
        "≈ 2 Go de RAM par processus, ≈ 5 Go d'entrées brutes par département (effacées à la fin de chacun)",
        (
            Field("depts", "depts", "départements (codes ou all)", ALL),
            Field(
                "species", "species", "espèces (ids séparés par des espaces ; vide = toutes)", ""
            ),
            Field("jobs", "number", "départements en parallèle", 2, minimum=1, maximum=4),
            Field(
                "clean_raw",
                "bool",
                "effacer les entrées brutes d'un département une fois ses T écrits",
                True,
            ),
            Field("force", "bool", "refaire même si le résultat existe", False),
        ),
        exclusive=True,
        min_free_gb=10.0,
    ),
    "tiles": Tool(
        "tiles",
        PIPELINE,
        "heures pour la France",
        "les T de tous les départements sur le volume ; niveau 15 en parallèle",
        (
            Field("depts", "depts", "départements (codes ou all)", ALL),
            Field(
                "species",
                "species",
                "espèces (ids ; vide = toutes ; un G demande tout son groupe)",
                "",
            ),
            Field("jobs", "number", "processus pour le niveau 15", 2, minimum=1, maximum=8),
            Field("per_dept", "bool", "aussi un jeu par département (hors ligne de l'app)", True),
            Field("groups", "bool", "aussi un PMTiles G par groupe", True),
            Field("force", "bool", "refaire même si le résultat existe", False),
        ),
        exclusive=True,
        min_free_gb=10.0,
    ),
    "publish": Tool(
        "publish",
        PIPELINE,
        "minutes à une heure (téléversement)",
        "bucket ; les PMTiles dans data/tiles du worker",
        (
            Field("depts", "depts", "départements (codes ou all)", ALL),
            Field("species", "species", "espèces (ids ; vide = toutes)", ""),
            Field("per_dept", "bool", "publier aussi les jeux par département", True),
            Field("groups", "bool", "publier aussi les G", True),
            Field("prune", "bool", "effacer du bucket les autres jeux de départements", False),
            Field(
                "dry_run", "bool", "montrer seulement ce qui serait fait (rien n'est écrit)", True
            ),
        ),
        exclusive=True,
        confirm="Publier avec --prune efface du bucket les PMTiles des autres jeux : la carte n'affichera plus que ce jeu. Lance d'abord avec « montrer seulement » coché et lis le journal. Continuer ?",
    ),
    "weather-grid": Tool(
        "weather-grid",
        PIPELINE,
        "minutes",
        "les T sur le volume ; le fichier est versionné : déposé dans le bucket (artifacts/) à committer",
        (
            Field("depts", "depts", "départements (codes ou all)", ALL),
            Field(
                "to_bucket", "bool", "déposer weather_grid.json dans le bucket (artifacts/)", True
            ),
        ),
        exclusive=True,
    ),
}


class ToolError(ValueError):
    """Outil inconnu ou option refusée : la page le dit, rien n'est lancé."""


def _choices(tool: Tool, f: Field) -> tuple[str, ...]:
    if f.choices is not None:
        return f.choices
    if tool.kind == "render" and f.name == "species":
        return _render_species()
    return ()


def describe() -> list[dict[str, Any]]:
    """Le catalogue tel que la page l'affiche."""
    out = []
    for tool in TOOLS.values():
        out.append(
            {
                "kind": tool.kind,
                "label": tool.label,
                "duration": tool.duration,
                "needs": tool.needs,
                "exclusive": tool.exclusive,
                "min_free_gb": tool.min_free_gb,
                "confirm": tool.confirm,
                "fields": [
                    {
                        "name": f.name,
                        "kind": f.kind,
                        "label": f.label,
                        "default": f.default,
                        "choices": list(_choices(tool, f)) or None,
                        "min": f.minimum,
                        "max": f.maximum,
                    }
                    for f in tool.fields
                ],
            }
        )
    return out


def validate(kind: str, args: dict[str, Any] | None) -> dict[str, str]:
    """Options normalisées (chaînes) ; `ToolError` pour tout ce qui n'est pas dans le catalogue."""
    tool = TOOLS.get(kind)
    if tool is None:
        raise ToolError(f"outil inconnu : {kind}")
    args = args or {}
    known = {f.name for f in tool.fields}
    extra = set(args) - known
    if extra:
        raise ToolError(f"options refusées : {', '.join(sorted(extra))}")
    out: dict[str, str] = {}
    for f in tool.fields:
        value = args.get(f.name, f.default)
        if f.kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "oui", "on", "yes")
            out[f.name] = "True" if bool(value) else "False"
        elif f.kind == "depts":
            tokens = [t for t in str(value).replace(",", " ").split() if t]
            if not tokens:
                raise ToolError(f"{f.name} : au moins un département, ou all")
            try:
                parsed = parse_depts(tokens if tokens != [ALL] else [ALL])
            except SystemExit as e:
                raise ToolError(f"{f.name} : {e}") from e
            out[f.name] = ALL if tokens == [ALL] else " ".join(parsed)
        elif f.kind == "species":
            ids = [t for t in str(value or "").replace(",", " ").split() if t]
            unknown = [t for t in ids if t not in list_species()]
            if unknown:
                raise ToolError(f"{f.name} : espèce inconnue {', '.join(unknown)}")
            out[f.name] = " ".join(dict.fromkeys(ids))
        elif f.kind == "number":
            try:
                number = float(value)
            except (TypeError, ValueError) as e:
                raise ToolError(f"{f.name} : nombre attendu") from e
            if f.minimum is not None and number < f.minimum:
                raise ToolError(f"{f.name} : au moins {f.minimum:g}")
            if f.maximum is not None and number > f.maximum:
                raise ToolError(f"{f.name} : au plus {f.maximum:g}")
            out[f.name] = f"{number:g}"
        else:
            value = str(value)
            if value not in _choices(tool, f):
                raise ToolError(f"{f.name} : valeur inconnue {value!r}")
            out[f.name] = value
    return out


def command(kind: str, args: dict[str, Any] | None) -> list[str]:
    """La ligne de commande du sous-processus : `python -m <module> -q <commande> [options]`."""
    tool = TOOLS[kind]
    normalized = validate(kind, args)
    if tool.module == PIPELINE:
        argv = [sys.executable, "-m", "champis_api.cli", "-q", "pipeline", kind]
    else:
        argv = [sys.executable, "-m", tool.module, "-q", kind]
    for f in tool.fields:
        value = normalized[f.name]
        if f.kind == "bool":
            if value == "True":
                argv.append(f.option())
        elif f.kind == "depts":
            argv += [f.option(), *value.split()]
        elif f.kind == "species":
            for sid in value.split():  # `--species` se répète (action append)
                argv += [f.option(), sid]
        elif (
            f.kind == "number" and value.replace(".", "", 1).isdigit() and float(value).is_integer()
        ):
            argv += [f.option(), str(int(float(value)))]
        else:
            argv += [f.option(), value]
    return argv
