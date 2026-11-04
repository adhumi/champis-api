"""Cache disque des plages lues par le relais `GET /tiles/{name}` (spec › Points ouverts › Tuiles hors
du processus Python, décision du 4 nov. 2026).

Les PMTiles sont immuables (le nom porte le hash de l'espèce, la version du scoring et celui des
départements) et les clients lisent par requêtes `Range: bytes=a-b` exactes : en-tête (0–127),
répertoires racine et feuilles, puis une tuile par requête. Chaque plage relayée est gardée sur le
volume sous `<dir>/<nom>/<a>-<b>` avec ses en-têtes (ETag, Content-Range, type), et servie de là aux
lectures suivantes sans repasser par le bucket.

Sûr avec plusieurs processus : écriture dans un fichier temporaire du même dossier puis `os.replace`
(un lecteur voit l'entrée entière ou rien), lecture sans verrou, aucun état en mémoire nécessaire
(les compteurs ne sont que des indices pour espacer les nettoyages). Éviction LRU approximative :
le mtime est rafraîchi à la lecture au plus une fois par heure, et au-delà de la borne les entrées
les plus anciennes sont supprimées, hors du chemin de la requête et sous un budget de temps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import Settings

log = logging.getLogger(__name__)

MAX_ENTRY_BYTES = 4 * 1024 * 1024  # au-delà, on relaie sans garder (une feuille fait ≤ ~60 Ko)
SMALL_READ_BYTES = 256 * 1024  # en deçà, lecture directe ; au-delà, dans un thread
TOUCH_INTERVAL_S = 3600  # mtime rafraîchi à la lecture au plus une fois par heure
TRIM_BUDGET_S = 5.0  # durée maximale d'un nettoyage
TRIM_TARGET = 0.9  # après nettoyage, le cache redescend à 90 % de la borne
TMP_MAX_AGE_S = 600  # fichier temporaire orphelin (processus tué en pleine écriture) à supprimer
_TMP_SUFFIX = ".tmp"
_RANGE = re.compile(r"^bytes=(\d+)-(\d+)$")
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


def parse_exact_range(header: str | None) -> tuple[int, int] | None:
    """`bytes=a-b` avec deux bornes et a ≤ b, sinon None (suffixe, ouvert, multiple : relais seul)."""
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if m is None:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if a <= b else None


def parse_content_range(header: str | None) -> tuple[int, int, int | None] | None:
    """`bytes a-b/total` → (a, b, total) ; total None si `*`."""
    m = _CONTENT_RANGE.match(header or "")
    if m is None:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


@dataclass(frozen=True)
class Entry:
    """Une plage mise en cache : son corps et ce qu'il faut pour rejouer la réponse 206."""

    body: bytes
    start: int  # bornes réelles renvoyées par le bucket (peuvent être plus courtes que demandé)
    end: int
    total: int | None
    etag: str | None = None
    content_type: str = "application/octet-stream"

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total if self.total is not None else '*'}"

    def headers(self) -> dict[str, str]:
        out = {
            "content-type": self.content_type,
            "content-length": str(len(self.body)),
            "content-range": self.content_range,
            "accept-ranges": "bytes",
        }
        if self.etag:
            out["etag"] = self.etag
        return out


@dataclass
class Stats:
    entries: int
    bytes: int
    removed: int = 0  # entrées supprimées par le dernier nettoyage
    complete: bool = True  # False : parcours interrompu par le budget de temps


class TileCache:
    """Cache clé = (nom de fichier, début, fin) → fichier `<root>/<nom>/<début>-<fin>`.

    Format d'une entrée : une ligne JSON (bornes, total, ETag, type) puis le corps. `max_bytes` borne
    la taille totale (approximativement : le nettoyage est différé et sous budget).
    """

    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        # Octets écrits depuis le dernier nettoyage par ce processus ; `inf` force un nettoyage à la
        # première écriture d'un processus (pas d'état durable : chaque worker vérifie une fois).
        self._written_since_trim = float("inf")

    @classmethod
    def from_settings(cls, settings: Settings) -> TileCache | None:
        """None quand `CHAMPIS_TILE_CACHE_MB=0` : le relais ne garde rien."""
        if settings.tile_cache_mb <= 0:
            return None
        return cls(settings.tile_cache_path, settings.tile_cache_mb * 1_000_000)

    # ------------------------------------------------------------------ chemins

    def path(self, name: str, start: int, end: int) -> Path:
        return self.root / name / f"{start}-{end}"

    @property
    def trim_threshold(self) -> int:
        """Octets à écrire entre deux nettoyages : 5 % de la borne, au moins 8 Mo."""
        return max(self.max_bytes // 20, 8 * 1_000_000)

    # ------------------------------------------------------------------ lecture

    async def get(self, name: str, start: int, end: int) -> Entry | None:
        """Entrée pour la plage demandée, ou None ; lecture dans un thread si elle est grosse."""
        path = self.path(name, start, end)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > SMALL_READ_BYTES:
            return await asyncio.to_thread(self._read, path)
        return self._read(path)

    def _read(self, path: Path) -> Entry | None:
        try:
            with path.open("rb") as f:
                data = f.read()
            entry = decode(data)
        except OSError:
            return None  # supprimée entre-temps par un nettoyage
        if entry is None:  # fichier corrompu (ne devrait pas arriver : écriture atomique)
            log.warning("cache tuiles : entrée illisible supprimée, %s", path)
            _unlink(path)
            return None
        self._touch(path)
        return entry

    @staticmethod
    def _touch(path: Path) -> None:
        """LRU approximatif : le mtime vieillit d'au plus une heure sans être rafraîchi."""
        try:
            if time.time() - path.stat().st_mtime > TOUCH_INTERVAL_S:
                os.utime(path)
        except OSError:
            pass

    # ------------------------------------------------------------------ écriture

    def put(self, name: str, start: int, end: int, entry: Entry, expected_len: int) -> bool:
        """Écrit l'entrée si le corps est complet (un client parti en cours de route laisse un corps
        tronqué : rien n'est gardé). Atomique : temporaire puis `os.replace`. Synchrone, à appeler
        hors de la boucle d'événements (tâche de fond Starlette ou `to_thread`)."""
        if len(entry.body) != expected_len or expected_len > MAX_ENTRY_BYTES:
            return False
        path = self.path(name, start, end)
        if path.exists():
            return True  # un autre worker l'a déjà écrite
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}{_TMP_SUFFIX}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as f:
                f.write(encode(entry))
                f.flush()
            os.replace(tmp, path)
        except OSError:
            log.warning("cache tuiles : écriture impossible, %s", path, exc_info=True)
            _unlink(tmp)
            return False
        self._written_since_trim += len(entry.body)
        if self._written_since_trim >= self.trim_threshold:
            self._written_since_trim = 0
            self.trim()
        return True

    # ------------------------------------------------------------------ nettoyage et état

    def scan(self, budget_s: float | None = None) -> tuple[list[tuple[float, int, Path]], Stats]:
        """(mtime, taille, chemin) de chaque entrée ; les temporaires orphelins sont supprimés."""
        deadline = time.monotonic() + budget_s if budget_s is not None else None
        entries: list[tuple[float, int, Path]] = []
        total = 0
        complete = True
        now = time.time()
        try:
            dirs = [d for d in os.scandir(self.root) if d.is_dir(follow_symlinks=False)]
        except OSError:
            return [], Stats(0, 0)
        for d in dirs:
            try:
                with os.scandir(d.path) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if e.name.endswith(_TMP_SUFFIX):
                            if now - st.st_mtime > TMP_MAX_AGE_S:
                                _unlink(Path(e.path))
                            continue
                        entries.append((st.st_mtime, st.st_size, Path(e.path)))
                        total += st.st_size
            except OSError:
                continue
            if deadline is not None and time.monotonic() > deadline:
                complete = False
                break
        return entries, Stats(len(entries), total, complete=complete)

    def trim(self, budget_s: float = TRIM_BUDGET_S) -> Stats:
        """Supprime les entrées les plus anciennes jusqu'à `TRIM_TARGET × max_bytes`, sous budget."""
        started = time.monotonic()
        entries, stats = self.scan(budget_s)
        if not stats.complete:
            log.warning(
                "cache tuiles : nettoyage interrompu après %d entrées (budget %.0f s)",
                stats.entries,
                budget_s,
            )
            return stats
        if stats.bytes <= self.max_bytes:
            return stats
        target = int(self.max_bytes * TRIM_TARGET)
        entries.sort()  # mtime croissant : les moins récemment lues d'abord
        deadline = started + budget_s
        for _mtime, size, path in entries:
            if stats.bytes <= target or time.monotonic() > deadline:
                break
            if _unlink(path):
                stats.bytes -= size
                stats.entries -= 1
                stats.removed += 1
        log.info(
            "cache tuiles : %d entrées supprimées, %d restantes (%.0f Mo sur %.0f Mo)",
            stats.removed,
            stats.entries,
            stats.bytes / 1e6,
            self.max_bytes / 1e6,
        )
        return stats

    def stats(self) -> Stats:
        return self.scan()[1]


def encode(entry: Entry) -> bytes:
    meta = {
        "start": entry.start,
        "end": entry.end,
        "total": entry.total,
        "etag": entry.etag,
        "content_type": entry.content_type,
        "length": len(entry.body),
    }
    return json.dumps(meta, separators=(",", ":")).encode() + b"\n" + entry.body


def decode(data: bytes) -> Entry | None:
    """None si le fichier ne respecte pas le format ou si le corps n'a pas la longueur annoncée."""
    nl = data.find(b"\n")
    if nl < 0:
        return None
    try:
        meta = json.loads(data[:nl])
        body = data[nl + 1 :]
        if len(body) != meta["length"]:
            return None
        return Entry(
            body=body,
            start=int(meta["start"]),
            end=int(meta["end"]),
            total=meta["total"],
            etag=meta.get("etag"),
            content_type=meta.get("content_type") or "application/octet-stream",
        )
    except (ValueError, KeyError, TypeError):
        return None


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False
