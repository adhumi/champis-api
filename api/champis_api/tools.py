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

from champis_pipeline.species import list_species

from . import jobs

RENDER_ZOOMS = (9, 10)
ALL = "all"


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # bool | number | choice
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
        "champis_pipeline.cli",
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
    argv = [sys.executable, "-m", tool.module, "-q", kind]
    for f in tool.fields:
        value = normalized[f.name]
        if f.kind == "bool":
            if value == "True":
                argv.append(f.option())
        else:
            argv += [f.option(), value]
    return argv
