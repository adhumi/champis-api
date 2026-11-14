"""Limitation de débit et taille des corps, pour un service exposé à tous (spec › Décisions prises
› API publique, 30 oct. 2026).

Seau à jetons par adresse IP, en mémoire, **par processus** : avec N workers uvicorn (4 nov.
2026) un client peut obtenir jusqu'à N fois la limite ; acceptable, le limiteur protège le CPU et
Open-Meteo, pas l'équité au jeton près. Une réserve pour les lectures,
une bien plus petite pour les écritures (`POST`, `PUT`, `DELETE`), qui ne servent qu'à l'app.
Au-delà : 429 avec `Retry-After`. Un corps plus gros que `max_body` : 413, qu'il soit annoncé
(`Content-Length`) ou non : les octets réellement reçus sont comptés (14 nov. 2026 ; un corps en
`Transfer-Encoding: chunked` n'annonce rien et passait le seul contrôle de l'en-tête).

L'adresse du client est celle que pose le proxy de confiance, jamais une valeur que le client
choisit (`client_ip`, correctif du 13 nov. 2026 : le premier élément de `X-Forwarded-For`, lu
jusque-là, est fourni par le client, qui changeait de seau à chaque requête). Les dictionnaires
sont bornés : un seau intact depuis une minute est de nouveau plein, donc oublié sans rien perdre,
et au-delà de `max_entries` le moins récemment servi part.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field

WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
# Un seau se remplit en une minute quel que soit son débit : passé ce délai sans requête, il vaut
# un seau neuf et peut être oublié.
FULL_AFTER_S = 60.0
# Plafond d'entrées par dictionnaire (≈ 200 octets l'une, soit ≈ 10 Mo par worker au pire) : une
# rafale d'adresses distinctes ne fait pas grossir la mémoire sans fin.
MAX_ENTRIES = 50_000
MAX_KEY_CHARS = 64  # une adresse IPv6 complète en fait 45


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class TokenBuckets:
    per_minute: float
    clock: Callable[[], float] = time.monotonic
    # du moins récemment servi au plus récemment servi (un seau servi repasse en fin)
    buckets: dict[str, _Bucket] = field(default_factory=dict)
    max_entries: int = MAX_ENTRIES

    def take(self, key: str) -> float:
        """0 si un jeton a été pris, sinon le délai (s) avant le prochain."""
        now = self.clock()
        self.prune(now=now)
        b = self.buckets.pop(key, None)
        if b is None:
            # plein de seaux actifs : le moins récemment servi part
            while self.buckets and len(self.buckets) >= self.max_entries:
                del self.buckets[next(iter(self.buckets))]
            b = _Bucket(self.per_minute, now)
        self.buckets[key] = b
        b.tokens = min(self.per_minute, b.tokens + (now - b.updated) * self.per_minute / 60.0)
        b.updated = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return 0.0
        return (1.0 - b.tokens) * 60.0 / self.per_minute

    def prune(self, older_than_s: float = FULL_AFTER_S, now: float | None = None) -> None:
        """Oublie les seaux intacts depuis `older_than_s` : ils sont en tête, le parcours
        s'arrête au premier seau récent (coût amorti constant, à chaque `take`)."""
        now = self.clock() if now is None else now
        while self.buckets:
            key = next(iter(self.buckets))
            if now - self.buckets[key].updated <= older_than_s:
                break
            del self.buckets[key]


class RateLimitMiddleware:
    """Middleware ASGI pur (pas de BaseHTTPMiddleware : il casse le streaming et les tâches)."""

    def __init__(
        self,
        app,
        read_per_minute: float = 120,
        write_per_minute: float = 20,
        max_body: int = 256 * 1024,
        clock: Callable[[], float] = time.monotonic,
        exempt_prefixes: tuple[str, ...] = (),
    ):
        self.app = app
        self.reads = TokenBuckets(read_per_minute, clock)
        self.writes = TokenBuckets(write_per_minute, clock)
        self.max_body = max_body
        self.exempt_prefixes = exempt_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return
        headers = scope_headers(scope)
        ip = client_ip(headers, scope.get("client"))
        method = scope["method"].upper()
        if method in WRITE_METHODS:
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_body:
                await _plain(send, 413, f"corps trop volumineux (maximum {self.max_body} octets)")
                return
            wait = self.writes.take(ip)
        else:
            wait = self.reads.take(ip)
        if wait > 0:
            await _plain(
                send, 429, "trop de requêtes", {"retry-after": str(max(1, int(wait + 0.999)))}
            )
            return
        await self._bounded(scope, receive, send)

    async def _bounded(self, scope, receive, send) -> None:
        """Passe la requête en comptant les octets du corps à mesure qu'ils arrivent. Au-delà de
        `max_body`, la lecture s'arrête (l'application ne reçoit pas la suite) et la réponse est
        413, quoi que l'application ait fait de l'interruption ; si elle avait déjà commencé à
        répondre, sa réponse reste (rien d'autre n'est possible en HTTP)."""
        received = 0
        too_large = started = False

        async def counted_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    too_large = True
                    raise BodyTooLargeError
            return message

        async def guarded_send(message):
            nonlocal started
            if too_large and not started:
                return  # la réponse de l'application à l'interruption : remplacée par le 413
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        with suppress(BodyTooLargeError):
            await self.app(scope, counted_receive, guarded_send)
        if too_large and not started:
            await _plain(send, 413, f"corps trop volumineux (maximum {self.max_body} octets)")


class BodyTooLargeError(Exception):
    """Levée dans `receive` quand le corps dépasse `max_body` : arrête la lecture."""


def scope_headers(scope) -> dict[str, str]:
    """En-têtes d'une requête ASGI, noms en minuscules ; d'un en-tête répété, la dernière ligne
    (celle qu'un proxy ajoute après celles du client)."""
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def client_ip(headers: dict[str, str], client) -> str:
    """Adresse du client telle que le proxy de confiance la donne, clé des seaux.

    L'API n'est jointe que par l'edge Railway (le relais Caddy de champis.app est retiré dans le
    même lot : plus de chaîne à deux proxys). La documentation Railway (Networking › Specs &
    Limits › Technical specifications › Request Headers, docs.railway.com/networking/
    public-networking/specs-and-limits, lue le 13 nov. 2026) ne garantit qu'un en-tête pour
    l'adresse : « `X-Real-IP` for identifying client's remote IP ». Elle ne dit rien de
    `X-Forwarded-For` ni de `X-Envoy-External-Address`. Donc, dans l'ordre :

    1. `X-Real-IP`, posé par l'edge ;
    2. à défaut, le **dernier** élément de `X-Forwarded-For` : un proxy ajoute en fin de liste
       l'adresse qu'il a vue, tout ce qui précède vient du client ;
    3. à défaut, l'adresse de la connexion (développement local, tests).

    Jamais le premier élément de `X-Forwarded-For`. Sans proxy devant (poste de développement),
    ces en-têtes viennent du client : sans conséquence, le service n'y est pas exposé.
    """
    real = headers.get("x-real-ip", "").strip()
    if real:
        return real[:MAX_KEY_CHARS]
    last = headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
    if last:
        return last[:MAX_KEY_CHARS]
    return client[0] if client else "?"


async def _plain(send, status: int, detail: str, extra: dict[str, str] | None = None) -> None:
    body = ('{"detail": "' + detail.replace('"', "'") + '"}').encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    for k, v in (extra or {}).items():
        hdrs.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})
