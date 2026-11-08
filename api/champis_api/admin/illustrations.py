"""Planche d'une espèce et le prompt qui la produit, pour l'onglet Espèces du backoffice.

Spec › Produit › Décisions prises › Illustrations des espèces (8 nov. 2026) : une aquarelle
`shared/illustrations/<id>.jpg` par espèce, générée (Midjourney ou Gemini) d'après
`shared/illustrations/PROMPTS.md` du référentiel. La planche est requise pour qu'une espèce soit
complète ; la fiche du backoffice la montre et affiche le prompt assemblé (style + composition +
sujet, puis les paramètres Midjourney), prêt à copier.

Le fichier PROMPTS.md est lu tel quel : ses blocs sont les citations (`> …`) qui suivent
« **Style** », « **Composition** » et chaque titre `### <id> — …` ; les paramètres Midjourney sont
la fin (`--ar …`) de l'exemple assemblé. Rien n'est recopié en dur ici : changer un prompt, c'est
changer le référentiel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from champis_pipeline.species import PROMPTS_FILE, REFERENTIAL, illustration_path

_SUBJECT_HEADING = re.compile(r"^###\s+([a-z_]+)\s+—")
_PARAMETERS = re.compile(r"(--ar\b.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Prompts:
    style: str
    composition: str
    subjects: dict[str, str]
    parameters: str | None  # paramètres Midjourney (`--ar 1:1 …`), None s'ils manquent

    def for_species(self, species_id: str) -> dict[str, Any] | None:
        subject = self.subjects.get(species_id)
        if not subject:
            return None
        full = " ".join(part for part in (self.style, self.composition, subject) if part)
        return {
            "style": self.style,
            "composition": self.composition,
            "subject": subject,
            "full": full,
            "midjourney": f"{full} {self.parameters}" if self.parameters else full,
        }


def _quote_blocks(lines: list[str]) -> list[str]:
    """Les citations (`> …`) d'une suite de lignes, chacune repliée en un paragraphe."""
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith(">"):
            current.append(line[1:].strip())
        elif current:
            blocks.append(" ".join(w for w in current if w))
            current = []
    if current:
        blocks.append(" ".join(w for w in current if w))
    return blocks


def _first_quote_after(text: str, marker: str) -> str:
    idx = text.find(marker)
    if idx < 0:
        return ""
    blocks = _quote_blocks(text[idx:].splitlines())
    return blocks[0] if blocks else ""


def parse_prompts(text: str) -> Prompts:
    style = _first_quote_after(text, "**Style**")
    composition = _first_quote_after(text, "**Composition**")
    subjects: dict[str, str] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _SUBJECT_HEADING.match(line)
        if not m:
            continue
        end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("#")), len(lines))
        blocks = _quote_blocks(lines[i + 1 : end])
        if blocks:
            subjects[m.group(1)] = blocks[0]
    params = _PARAMETERS.search(text)
    return Prompts(style, composition, subjects, params.group(1).strip() if params else None)


@lru_cache(maxsize=4)
def _load(path: Path, mtime: float) -> Prompts:
    return parse_prompts(path.read_text(encoding="utf-8"))


def load_prompts(path: Path = PROMPTS_FILE) -> Prompts | None:
    try:
        return _load(path, path.stat().st_mtime)
    except OSError:
        return None


def illustration_info(species_id: str) -> dict[str, Any]:
    """Présence de la planche, son chemin dans le référentiel, et le prompt pour l'obtenir."""
    path = illustration_path(species_id)
    prompts = load_prompts()
    return {
        "present": path is not None,
        "file": str(path.relative_to(REFERENTIAL)) if path else None,
        "bytes": path.stat().st_size if path else 0,
        "prompt": prompts.for_species(species_id) if prompts else None,
    }
