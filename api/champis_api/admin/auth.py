"""Accès au backoffice : une clé unique, un cookie signé, un `Bearer` pour les scripts.

`CHAMPIS_ADMIN_KEY` (variable secrète Railway, ≥ 32 caractères) est comparée en temps constant.
Le cookie `champis_admin` porte une date d'expiration et sa signature HMAC par une clé dérivée de la
clé d'admin : rien n'est stocké côté serveur, changer la clé déconnecte tout. HttpOnly, Secure,
SameSite=Strict, limité à `/admin`, 30 jours. Les routes refusent toute requête dont l'`Origin`
n'est pas celle de l'API : une page tierce ne peut ni lire ni agir, et `Authorization` n'entre pas
dans les en-têtes CORS autorisés. Les actions (`POST`) exigent en plus `X-Champis-Admin: 1`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request

COOKIE = "champis_admin"
TTL_S = 30 * 24 * 3600
ACTION_HEADER = "x-champis-admin"
MIN_KEY_LENGTH = 16  # une clé plus courte est refusée au démarrage : le backoffice reste fermé


def _derived(key: str) -> bytes:
    return hmac.new(key.encode("utf-8"), b"champis-admin-cookie", hashlib.sha256).digest()


def make_token(key: str, now: float | None = None, ttl_s: int = TTL_S) -> str:
    expires = int((now if now is not None else time.time()) + ttl_s)
    sig = hmac.new(_derived(key), str(expires).encode(), hashlib.sha256).hexdigest()[:40]
    return f"{expires}.{sig}"


def token_valid(key: str, token: str | None, now: float | None = None) -> bool:
    if not key or not token or "." not in token:
        return False
    expires_s, sig = token.split(".", 1)
    if not expires_s.isdigit():
        return False
    expected = hmac.new(_derived(key), expires_s.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(expected, sig):
        return False
    return int(expires_s) > (now if now is not None else time.time())


def key_matches(key: str, given: str | None) -> bool:
    """Comparaison en temps constant ; une clé trop courte n'ouvre jamais rien."""
    if not key or len(key) < MIN_KEY_LENGTH or not given:
        return False
    return hmac.compare_digest(key.encode("utf-8"), given.encode("utf-8"))


def enabled(request: Request) -> str:
    """La clé configurée, ou 404 : sans clé le backoffice n'existe pas."""
    key = getattr(request.app.state, "settings", None)
    key = key.admin_key if key is not None else ""
    if not key or len(key) < MIN_KEY_LENGTH:
        raise HTTPException(404)
    return key


def same_origin(request: Request) -> None:
    """Un `Origin` étranger est refusé (la page vit sur l'API elle-même) ; absent = requête
    directe (curl, navigation), acceptée."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if origin.rstrip("/").split("://", 1)[-1] != host:
        raise HTTPException(403, "origine refusée")


def authenticated(request: Request, key: Annotated[str, Depends(enabled)]) -> None:
    same_origin(request)
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and key_matches(key, auth[7:].strip()):
        return
    if token_valid(key, request.cookies.get(COOKIE)):
        return
    raise HTTPException(401, "clé requise")


def action(request: Request, _: Annotated[None, Depends(authenticated)]) -> None:
    """Une action (`POST`) : l'en-tête que seul le script de la page ajoute."""
    if request.headers.get(ACTION_HEADER) != "1":
        raise HTTPException(403, f"en-tête {ACTION_HEADER}: 1 requis")


Enabled = Annotated[str, Depends(enabled)]
Authenticated = Annotated[None, Depends(authenticated)]
Action = Annotated[None, Depends(action)]
