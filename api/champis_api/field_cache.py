"""Cache en mémoire du champ compact servi par `GET /weather/field` (13 nov. 2026).

Mesure sur la grille réelle (22 672 cellules) : sans lui, une requête France entière lit et
assemble tout le champ à chaque fois, ≈ 0,3 s de CPU pour une espèce, ≈ 0,5 s pour cinq, ≈ 2 s
pour les 27 (sans compter la compression), et rien n'était gardé d'une requête à l'autre. Or le
champ ne change que lorsque le précalcul change, deux fois par jour.

Une entrée = le champ de **toute la grille** pour un jeu d'espèces et un jour, déjà encodé en
JSON cellule par cellule ; une requête n'a plus qu'à choisir les cellules de son emprise et à
coller leurs fragments. La clé ne porte donc pas l'emprise, que chaque écran choisit à sa façon
(carte du site, app) : toutes les cartes qui affichent la même couche le même jour partagent
l'entrée. Les entrées sont jetées dès que le précalcul change (`version`, voir
`routers.weather`), soit au plus un seau de 12 h, et bornées en octets (la moins récemment servie
part). Une seule construction à la fois par worker : dix requêtes identiques qui arrivent
ensemble n'en déclenchent qu'une.

Construire la grille entière coûte plus que lire une petite emprise : l'entrée n'est donc
construite que si la requête couvre déjà une grande part de la grille (`eager`), ou à la
**deuxième** demande du même jeu d'espèces et du même jour. Une requête isolée est servie comme
avant, par une lecture de sa seule emprise, et ne coûte jamais plus qu'avant ; une requête
répétée (couche très regardée, ou rejouée en boucle) ne coûte plus que le collage.

Par processus, comme le limiteur de débit : chaque worker uvicorn a le sien.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass

MB = 1024 * 1024
ENTRY_OVERHEAD = 200  # octets par cellule en plus du fragment (dictionnaire, tuple, en-têtes)


MAX_SEEN = 4096  # clés demandées une fois, gardées jusqu'au prochain changement de version


@dataclass(frozen=True)
class FieldEntry:
    """Par cellule : (fragment JSON de la cellule, `computed_at` du précalcul)."""

    items: dict[str, tuple[bytes, str]]
    size: int

    @classmethod
    def of(cls, items: dict[str, tuple[bytes, str]]) -> FieldEntry:
        payload = sum(len(frag) for frag, _ in items.values())
        return cls(items, payload + ENTRY_OVERHEAD * len(items))


class FieldCache:
    def __init__(self, max_bytes: int = 64 * MB) -> None:
        self.max_bytes = max_bytes
        self.bytes = 0
        self.builds = 0  # constructions faites (tests, diagnostic)
        self._version: Hashable = None
        self._entries: OrderedDict[Hashable, FieldEntry] = OrderedDict()
        self._seen: set[Hashable] = set()
        self._lock = threading.Lock()
        self._building = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def _lookup(
        self, version: Hashable, key: Hashable, eager: bool
    ) -> tuple[FieldEntry | None, bool]:
        """(l'entrée si elle est là, faut-il la construire sinon)."""
        with self._lock:
            if version != self._version:  # le précalcul a changé : tout ce qu'on garde est périmé
                self._entries.clear()
                self._seen.clear()
                self.bytes = 0
                self._version = version
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry, False
            if eager or key in self._seen:
                return None, True
            if len(self._seen) >= MAX_SEEN:
                self._seen.clear()
            self._seen.add(key)
            return None, False

    def get(
        self, version: Hashable, key: Hashable, build: Callable[[], FieldEntry], eager: bool = False
    ) -> FieldEntry | None:
        """L'entrée `key` de la version courante ; construite par `build` si elle manque et que
        la requête est `eager` ou que la clé a déjà été demandée ; sinon None : à l'appelant de
        servir sa seule emprise."""
        if self.max_bytes <= 0:
            return None
        entry, wanted = self._lookup(version, key, eager)
        if entry is not None or not wanted:
            return entry
        with self._building:
            entry, _ = self._lookup(version, key, True)  # construite pendant l'attente ?
            if entry is not None:
                return entry
            entry = build()
            self.builds += 1
            with self._lock:
                if version == self._version and entry.size <= self.max_bytes:
                    self._entries[key] = entry
                    self.bytes += entry.size
                    while self.bytes > self.max_bytes:
                        _, old = self._entries.popitem(last=False)
                        self.bytes -= old.size
            return entry
