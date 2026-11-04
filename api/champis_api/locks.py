"""Verrous de fichier entre les workers uvicorn d'un même service (décision du 4 nov. 2026 :
plusieurs workers, la pré-chauffe dans un service cron).

`flock` sur un fichier du volume : le verrou meurt avec le processus qui le tient, aucun nettoyage
à prévoir après un redémarrage. Deux usages :

- `file_lock(path)` : section critique bloquante, pour l'import de l'amorçage au démarrage (un
  worker importe, les autres attendent puis constatent qu'il n'y a plus rien à faire) ;
- `try_lock(path)` : élection d'un meneur, sans attente ; le worker qui l'obtient garde le
  descripteur ouvert (donc le verrou) tant qu'il vit et lance seul les tâches de fond.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


def _open(path: Path) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a")


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Attend le verrou exclusif sur `path`, le rend à la sortie."""
    f = _open(path)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        f.close()  # fermer le descripteur libère le verrou


def try_lock(path: Path) -> IO[str] | None:
    """Verrou exclusif sans attente : le fichier ouvert (à garder tant qu'on veut le verrou),
    ou None si un autre processus le tient."""
    f = _open(path)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f
