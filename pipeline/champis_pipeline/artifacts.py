"""Sorties du pipeline qui doivent revenir dans git quand il tourne sur le service d'exécution
(spec › Backoffice › Tâches, lot BO4) : `data/weather_grid.json` est versionné, `batch` et
`weather-grid` tournant sur le worker ne peuvent pas le committer. Elles sont déposées dans le
bucket sous `artifacts/<nom>` avec une fiche `artifacts/<nom>.meta.json` (`generated`, `bytes`,
`sha256`, `command`, détails) ; le backoffice les liste et les donne à télécharger, Adrien les
commet depuis son poste.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import BucketLike

PREFIX = "artifacts/"
META_SUFFIX = ".meta.json"


def meta_key(name: str) -> str:
    return f"{PREFIX}{name}{META_SUFFIX}"


def publish_artifact(
    bucket: BucketLike, path: Path, name: str, command: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Dépose `path` sous `artifacts/<name>` et sa fiche ; renvoie la fiche."""
    data = path.read_bytes()
    meta: dict[str, Any] = {
        "name": name,
        "key": PREFIX + name,
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "command": command,
        **(extra or {}),
    }
    bucket.upload(path, PREFIX + name)
    bucket.put_json(meta_key(name), meta)
    return meta


def list_artifacts(bucket: BucketLike) -> list[dict[str, Any]]:
    """Les artefacts présents, fiche comprise quand elle existe, les plus récents d'abord."""
    present = bucket.list(PREFIX)
    out = []
    for key, size in present.items():
        if key.endswith(META_SUFFIX) or key == PREFIX:
            continue
        name = key[len(PREFIX) :]
        meta = bucket.get_json(meta_key(name)) or {}
        out.append({"name": name, "key": key, "bytes": size, **meta})
    return sorted(out, key=lambda a: a.get("generated", ""), reverse=True)
