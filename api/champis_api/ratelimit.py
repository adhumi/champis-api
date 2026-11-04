"""Limitation de débit et taille des corps, pour un service exposé à tous (spec › Décisions prises
› API publique, 30 oct. 2026).

Seau à jetons par adresse IP, en mémoire, **par processus** : avec N workers uvicorn (4 nov.
2026) un client peut obtenir jusqu'à N fois la limite ; acceptable, le limiteur protège le CPU et
Open-Meteo, pas l'équité au jeton près. Une réserve pour les lectures,
une bien plus petite pour les écritures (`POST`, `PUT`, `DELETE`), qui ne servent qu'à l'app.
Au-delà : 429 avec `Retry-After`. Un corps annoncé plus gros que `max_body` : 413. L'IP est le
premier élément de `X-Forwarded-For` quand il existe (proxy Railway), sinon l'adresse du client.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class TokenBuckets:
    per_minute: float
    clock: Callable[[], float] = time.monotonic
    buckets: dict[str, _Bucket] = field(default_factory=dict)

    def take(self, key: str) -> float:
        """0 si un jeton a été pris, sinon le délai (s) avant le prochain."""
        now = self.clock()
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = _Bucket(self.per_minute, now)
        b.tokens = min(self.per_minute, b.tokens + (now - b.updated) * self.per_minute / 60.0)
        b.updated = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return 0.0
        return (1.0 - b.tokens) * 60.0 / self.per_minute

    def prune(self, older_than_s: float = 600.0) -> None:
        now = self.clock()
        for k in [k for k, b in self.buckets.items() if now - b.updated > older_than_s]:
            del self.buckets[k]


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
        self._requests = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
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
        self._requests += 1
        if self._requests % 1000 == 0:
            self.reads.prune()
            self.writes.prune()
        await self.app(scope, receive, send)


def client_ip(headers: dict[str, str], client) -> str:
    fwd = headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return client[0] if client else "?"


async def _plain(send, status: int, detail: str, extra: dict[str, str] | None = None) -> None:
    body = ('{"detail": "' + detail.replace('"', "'") + '"}').encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    for k, v in (extra or {}).items():
        hdrs.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})
