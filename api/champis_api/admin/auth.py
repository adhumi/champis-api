"""Accès au backoffice : une clé unique, un cookie signé, un `Bearer` pour les scripts.

`CHAMPIS_ADMIN_KEY` (variable secrète Railway, ≥ 32 caractères) est comparée en temps constant.
Le cookie `champis_admin` porte une date d'expiration et sa signature HMAC par une clé dérivée de la
clé d'admin : rien n'est stocké côté serveur, changer la clé déconnecte tout. HttpOnly, Secure,
SameSite=Strict, limité à `/admin`, 30 jours. Les routes refusent toute requête dont l'`Origin`
n'est pas celle de l'API : une page tierce ne peut ni lire ni agir, et `Authorization` n'entre pas
dans les en-têtes CORS autorisés. Les actions (`POST`) exigent en plus `X-Champis-Admin: 1`.

Pages rendues par le serveur (lot BO6, `pages.py`) : un `<form>` ne peut pas poser d'en-tête, ses
`POST` portent donc un champ caché `csrf`, HMAC du cookie de session par la même clé dérivée
(`csrf_token`) ; l'API JSON garde son en-tête. Les messages d'une redirection (`flash`) voyagent
dans un cookie signé de la même façon, lu une fois.
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


def csrf_token(key: str, session: str | None) -> str:
    """Jeton des formulaires : lié au cookie de session (vide avec un `Bearer`), jamais stocké."""
    msg = b"csrf:" + (session or "").encode("utf-8")
    return hmac.new(_derived(key), msg, hashlib.sha256).hexdigest()[:40]


def csrf_valid(key: str, session: str | None, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(csrf_token(key, session), given or "")


def sign(key: str, text: str) -> str:
    """`<signature>.<texte>` pour le cookie des messages : rien d'autre que nous ne l'écrit."""
    sig = hmac.new(_derived(key), b"flash:" + text.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{sig}.{text}"


def unsign(key: str, value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    sig, text = value.split(".", 1)
    return text if hmac.compare_digest(sign(key, text).split(".", 1)[0], sig) else None


def logged_in(request: Request, key: str) -> bool:
    """Cookie valide ou `Bearer` : la session d'une page comme d'un appel JSON."""
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and key_matches(key, header[7:].strip()):
        return True
    return token_valid(key, request.cookies.get(COOKIE))


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
    if not logged_in(request, key):
        raise HTTPException(401, "clé requise")


def action(request: Request, _: Annotated[None, Depends(authenticated)]) -> None:
    """Une action (`POST`) : l'en-tête que seul le script de la page ajoute."""
    if request.headers.get(ACTION_HEADER) != "1":
        raise HTTPException(403, f"en-tête {ACTION_HEADER}: 1 requis")


Enabled = Annotated[str, Depends(enabled)]
Authenticated = Annotated[None, Depends(authenticated)]
Action = Annotated[None, Depends(action)]
