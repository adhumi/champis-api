"""Accès au backoffice : une clé unique, une session révocable, un `Bearer` pour les scripts.

`CHAMPIS_ADMIN_KEY` (variable secrète Railway, au moins 32 caractères : `MIN_KEY_LENGTH`) est
comparée en temps constant ; plus courte, le backoffice reste fermé (404) et le démarrage comme
`champis-api doctor` le disent (`key_problem`).

**Session** (14 nov. 2026). La connexion crée un identifiant de session aléatoire ; le cookie
`champis_admin` le porte, signé (HMAC par une clé dérivée de la clé d'admin), et le serveur le
connaît : table `admin_sessions` de la base des observations, qui n'en garde que le SHA-256 (la
base seule ne permet pas de refaire un cookie). Une session vaut donc pour tous les workers et
survit à un redéploiement, la déconnexion la supprime, « déconnecter toutes les sessions » les
supprime toutes, et changer la clé les invalide toutes (signature). Sans base (développement,
tests), les sessions vivent dans la mémoire du processus. Jusque-là le cookie était
`<expiration>.<signature>`, le même pour toutes les sessions, valable 30 jours quoi qu'il arrive.
HttpOnly, Secure derrière HTTPS, SameSite=Strict, limité à `/admin`, 30 jours.

Les routes refusent toute requête dont l'`Origin` n'est pas celle de l'API : une page tierce ne
peut ni lire ni agir, et `Authorization` n'entre pas dans les en-têtes CORS autorisés. Les actions
(`POST`) exigent en plus `X-Champis-Admin: 1`.

**Essais** : une clé refusée, dans le formulaire de connexion comme dans un `Authorization:
Bearer`, prend un jeton du même seau par adresse (`FAILURES_PER_MINUTE`) ; seau vide, la réponse
est 429 avant toute comparaison, même pour la bonne clé.

Pages rendues par le serveur (lot BO6, `pages.py`) : un `<form>` ne peut pas poser d'en-tête, ses
`POST` portent donc un champ caché `csrf`, HMAC du cookie de session par la même clé dérivée
(`csrf_token`) : il est propre à la session et meurt avec elle ; l'API JSON garde son en-tête. Les
messages d'une redirection (`flash`) voyagent dans un cookie signé de la même façon, lu une fois.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Request

from ..ratelimit import TokenBuckets, client_ip, scope_headers

log = logging.getLogger(__name__)

COOKIE = "champis_admin"
TTL_S = 30 * 24 * 3600
ACTION_HEADER = "x-champis-admin"
# `openssl rand -base64 32` en donne 44 ; une clé plus courte est refusée : le backoffice reste
# fermé (404), et le démarrage de l'API comme `champis-api doctor` disent pourquoi
MIN_KEY_LENGTH = 32
FAILURES_PER_MINUTE = 5  # clés refusées par minute et par adresse, formulaire et Bearer ensemble
SIGNATURE_CHARS = 40


def key_problem(key: str) -> str | None:
    """Pourquoi une clé configurée n'ouvre rien, ou None (clé absente ou assez longue)."""
    if not key or len(key) >= MIN_KEY_LENGTH:
        return None
    return (
        f"CHAMPIS_ADMIN_KEY fait {len(key)} caractères, il en faut au moins {MIN_KEY_LENGTH} "
        "(`openssl rand -base64 32`) : le backoffice reste fermé (404)"
    )


def _derived(key: str) -> bytes:
    return hmac.new(key.encode("utf-8"), b"champis-admin-cookie", hashlib.sha256).digest()


def _mac(key: str, purpose: bytes, text: str, length: int = SIGNATURE_CHARS) -> str:
    return hmac.new(_derived(key), purpose + text.encode("utf-8"), hashlib.sha256).hexdigest()[
        :length
    ]


def key_matches(key: str, given: str | None) -> bool:
    """Comparaison en temps constant ; une clé trop courte n'ouvre jamais rien."""
    if not key or len(key) < MIN_KEY_LENGTH or not given:
        return False
    return hmac.compare_digest(key.encode("utf-8"), given.encode("utf-8"))


# ------------------------------------------------------------------ sessions


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_cookie(key: str, session_id: str) -> str:
    """`<identifiant>.<signature>` : la signature évite une lecture en base pour un cookie forgé."""
    return f"{session_id}.{_mac(key, b'session:', session_id)}"


def session_id_of(key: str, cookie: str | None) -> str | None:
    """L'identifiant d'un cookie que nous avons signé avec cette clé, sinon None."""
    if not key or not cookie or "." not in cookie:
        return None
    session_id, signature = cookie.rsplit(".", 1)
    expected = _mac(key, b"session:", session_id)
    return session_id if session_id and hmac.compare_digest(expected, signature) else None


def session_digest(session_id: str) -> str:
    """Ce que le serveur garde d'une session : son SHA-256, pas de quoi refaire le cookie."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class Sessions(Protocol):
    """Ce que `Store` offre (table `admin_sessions`) et, sans base, `MemorySessions`."""

    def create_admin_session(self, digest: str, now: datetime, expires: datetime) -> None: ...

    def admin_session_valid(self, digest: str, now: datetime) -> bool: ...

    def delete_admin_session(self, digest: str) -> None: ...

    def delete_admin_sessions(self) -> int: ...


class MemorySessions:
    """Sans base (développement, tests) : les sessions du processus, perdues à son arrêt et
    inconnues des autres workers. En production la base est obligatoire (`app.require_database`)."""

    def __init__(self) -> None:
        self.sessions: dict[str, datetime] = {}

    def create_admin_session(self, digest: str, now: datetime, expires: datetime) -> None:
        self.sessions = {d: e for d, e in self.sessions.items() if e > now}
        self.sessions[digest] = expires

    def admin_session_valid(self, digest: str, now: datetime) -> bool:
        expires = self.sessions.get(digest)
        return expires is not None and expires > now

    def delete_admin_session(self, digest: str) -> None:
        self.sessions.pop(digest, None)

    def delete_admin_sessions(self) -> int:
        n = len(self.sessions)
        self.sessions = {}
        return n


def sessions(request: Request) -> Sessions:
    store = getattr(getattr(request.app.state, "champis", None), "store", None)
    if store is not None:
        return store
    memory = getattr(request.app.state, "admin_memory_sessions", None)
    if memory is None:
        memory = request.app.state.admin_memory_sessions = MemorySessions()
    return memory


def open_session(request: Request, key: str, now: datetime | None = None) -> str:
    """Crée une session et rend la valeur de son cookie (synchrone : à lancer dans un fil)."""
    now = now or datetime.now(UTC)
    session_id = new_session_id()
    sessions(request).create_admin_session(
        session_digest(session_id), now, now + timedelta(seconds=TTL_S)
    )
    return session_cookie(key, session_id)


def close_session(request: Request, key: str) -> None:
    """Supprime la session du cookie présenté, s'il est des nôtres (synchrone)."""
    session_id = session_id_of(key, request.cookies.get(COOKIE))
    if session_id is not None:
        sessions(request).delete_admin_session(session_digest(session_id))


def session_valid(request: Request, key: str, now: datetime | None = None) -> bool:
    """Le cookie porte-t-il une session que le serveur connaît encore ? (synchrone) Une base
    injoignable ne valide rien : la page renvoie à la connexion, les scripts gardent leur Bearer."""
    session_id = session_id_of(key, request.cookies.get(COOKIE))
    if session_id is None:
        return False
    try:
        return sessions(request).admin_session_valid(
            session_digest(session_id), now or datetime.now(UTC)
        )
    except Exception:
        log.exception("backoffice : session illisible (base injoignable ?)")
        return False


# ------------------------------------------------------------------ essais, Bearer


def failures(request: Request) -> TokenBuckets:
    """Clés refusées par adresse, formulaire de connexion et `Bearer` ensemble ; `TokenBuckets`
    oublie seul les seaux redevenus pleins et borne leur nombre."""
    buckets = getattr(request.app.state, "admin_login_buckets", None)
    if buckets is None:
        buckets = request.app.state.admin_login_buckets = TokenBuckets(FAILURES_PER_MINUTE)
    return buckets


def address(request: Request) -> str:
    return client_ip(scope_headers(request.scope), request.client)


def too_many(wait: float) -> HTTPException:
    return HTTPException(429, "trop d'essais", headers={"Retry-After": str(int(wait + 1))})


def _bearer(request: Request, key: str) -> bool:
    """`Authorization: Bearer <clé>` : vrai si la clé est la bonne. Une clé refusée prend un jeton
    du seau des essais de son adresse ; seau vide, 429 sans rien comparer (la bonne clé aussi :
    sinon la réponse dirait encore à un essai s'il a réussi)."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return False
    buckets, ip = failures(request), address(request)
    wait = buckets.wait(ip)
    if wait > 0:
        raise too_many(wait)
    if key_matches(key, header[7:].strip()):
        return True
    buckets.take(ip)
    log.warning("backoffice : clé refusée (Bearer)")
    return False


async def logged_in(request: Request, key: str) -> bool:
    """`Bearer` valide ou session connue : l'accès d'une page comme d'un appel JSON. Le résultat
    est gardé pour la requête (une seule lecture en base, un seul jeton pris)."""
    known = getattr(request.state, "admin_logged", None)
    if known is None:
        known = _bearer(request, key) or await asyncio.to_thread(session_valid, request, key)
        request.state.admin_logged = known
    return known


def csrf_token(key: str, session: str | None) -> str:
    """Jeton des formulaires : lié au cookie de session, donc à la session (vide avec un
    `Bearer`), jamais stocké."""
    return _mac(key, b"csrf:", session or "")


def csrf_valid(key: str, session: str | None, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(csrf_token(key, session), given or "")


def sign(key: str, text: str) -> str:
    """`<signature>.<texte>` pour le cookie des messages : rien d'autre que nous ne l'écrit."""
    return f"{_mac(key, b'flash:', text, 24)}.{text}"


def unsign(key: str, value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    sig, text = value.split(".", 1)
    return text if hmac.compare_digest(sign(key, text).split(".", 1)[0], sig) else None


def https(request: Request) -> bool:
    """Derrière Railway, TLS est terminé en amont : le protocole d'origine est dans l'en-tête."""
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def enabled(request: Request) -> str:
    """La clé configurée, ou 404 : sans clé (ou avec une clé trop courte) le backoffice n'existe
    pas."""
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


async def authenticated(request: Request, key: Annotated[str, Depends(enabled)]) -> None:
    same_origin(request)
    if not await logged_in(request, key):
        raise HTTPException(401, "clé requise")


def action(request: Request, _: Annotated[None, Depends(authenticated)]) -> None:
    """Une action (`POST`) : l'en-tête que seul le script de la page ajoute."""
    if request.headers.get(ACTION_HEADER) != "1":
        raise HTTPException(403, f"en-tête {ACTION_HEADER}: 1 requis")


Enabled = Annotated[str, Depends(enabled)]
Authenticated = Annotated[None, Depends(authenticated)]
Action = Annotated[None, Depends(action)]
