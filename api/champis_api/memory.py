"""Mémoire du conteneur et cache de fichiers du système (étude de sobriété, 26 nov. 2026).

La mémoire qu'un hébergeur mesure pour un conteneur est celle de son cgroup : celle des processus
(`anon`) **et** le cache de fichiers du noyau (`file`), c'est-à-dire les pages des fichiers lus ou
écrits récemment. Le noyau ne rend ce cache que sous pression, et un conteneur à la limite haute
n'en subit jamais : après l'import d'un instantané du cache météo, la base entière reste comptée
pendant des heures alors que les requêtes n'en relisent qu'une fraction.

`drop_file_cache` rend ces pages au système pour un fichier (`posix_fadvise`, conseil
`DONTNEED`) : les lectures suivantes repassent par le disque pour les seules pages utiles.
`container_memory` lit ce que le cgroup compte, pour la page Santé du backoffice.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

CGROUP = Path("/sys/fs/cgroup")
STAT_KEYS = ("anon", "file")


def drop_file_cache(path: Path) -> bool:
    """Demande au noyau d'oublier les pages en cache de `path`. Les pages pas encore écrites sur
    le disque ne sont pas concernées par le conseil : on les y pousse d'abord (`fsync`). Sans
    effet, et sans erreur, là où `posix_fadvise` n'existe pas (macOS) ou si le fichier manque."""
    fadvise = getattr(os, "posix_fadvise", None)
    if fadvise is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    except OSError as e:
        log.warning("cache de fichiers de %s non rendu (%s)", path.name, e)
        return False
    finally:
        os.close(fd)


def container_memory(cgroup: Path = CGROUP) -> dict[str, int] | None:
    """`{"current", "anon", "file"}` en octets d'après le cgroup v2 du conteneur ; None hors d'un
    conteneur Linux (poste de développement, cgroup v1)."""
    try:
        out = {"current": int((cgroup / "memory.current").read_text().strip())}
        for line in (cgroup / "memory.stat").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key in STAT_KEYS:
                out[key] = int(value)
    except (OSError, ValueError):
        return None
    return out
