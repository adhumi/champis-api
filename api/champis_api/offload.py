"""Calcul lourd des routes publiques hors de la boucle d'événements (13 nov. 2026).

Calculer les scores d'une cellule (≈ 14 ms par espèce), lire le précalcul d'une emprise ou encoder
une réponse de plusieurs Mo sont des opérations synchrones : faites dans un `async def`, elles
gelaient toutes les autres requêtes du worker (mesuré : 7 s pour `species=x` répété 500 fois,
2 s pour un champ France entière). `run` les confie à un petit groupe de fils réservé à cela.

Un groupe à part, et non celui d'`asyncio.to_thread` : ce dernier sert aussi la mesure minimale
et les compteurs après chaque réponse, qu'une rafale de requêtes lourdes ne doit pas faire
attendre. `THREADS` borne du même coup le calcul simultané d'un worker : les requêtes lourdes en
trop attendent leur tour sans rien coûter, au lieu de se disputer le processeur (l'interpréteur
n'exécute qu'un fil Python à la fois ; quatre fils laissent une requête courte passer à côté
d'une longue).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

THREADS = 4

_pool = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="champis-calcul")


async def run[**P, R](fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """`fn(*args, **kwargs)` dans un fil du groupe ; une exception remonte telle quelle."""
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_pool, call)
