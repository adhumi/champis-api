"""Masquage des secrets dans ce qui est journalisé (correctif du 13 nov. 2026).

Plusieurs clients HTTP écrivent l'adresse complète de chaque requête dans leurs journaux : httpx
dès le niveau INFO (`HTTP Request: GET https://…?apikey=…`), httpcore, hpack, urllib3 et botocore
au niveau DEBUG. Or trois de nos adresses portent un secret :

- Open-Meteo : la clé du plan payant, en paramètre `apikey` ;
- le bucket : la signature d'une adresse présignée (`X-Amz-Signature`, `X-Amz-Credential`,
  `X-Amz-Security-Token`), valable plusieurs heures ;
- APNs : le jeton de l'appareil dans le chemin (`/3/device/<jeton>`), et le JWT du fournisseur
  dans l'en-tête `authorization: bearer …` que hpack recopie au niveau DEBUG.

Deux protections, toutes deux en place : ces journaux restent sous WARNING en service comme dans
les CLI (`QUIET_LOGGERS`), et tout ce qui est écrit passe par `redact`, par la console des CLI
(écran, fichier journal, journal des tâches du backoffice, `-v` compris) et par un filtre posé sur
les handlers `logging` de l'API (`install`). Le niveau seul ne suffirait pas : `-v` le rétablit, et
le message d'une exception httpx cite aussi l'adresse.

Le journal d'accès de l'API, lui, perd la query string de chaque requête (`AccessQueryFilter`,
14 nov. 2026) : elle porte la position demandée (`lat`, `lon`, emprise), qui n'a rien à faire
dans un journal.

Bibliothèque standard seulement, aucune E/S.
"""

from __future__ import annotations

import logging
import re
import traceback

MASK = "***"

# Bibliothèques qui journalisent des adresses ou des en-têtes signés : rien sous WARNING.
QUIET_LOGGERS = ("httpx", "httpcore", "hpack", "h2", "botocore", "boto3", "s3transfer", "urllib3")

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # paramètres d'adresse et champs `nom=valeur` : clé Open-Meteo, signature et identifiants S3
    (
        re.compile(
            r"(?i)\b(apikey|api_key|access_token|x-amz-signature|x-amz-credential|"
            r"x-amz-security-token|awsaccesskeyid|signature|credential)(=|%3D)"
            r"[^&\s\"'<>,)\]]+"
        ),
        rf"\1\2{MASK}",
    ),
    # botocore, niveau DEBUG : « Signature:\n<hex> » (assez pour refaire une adresse présignée)
    (re.compile(r"(?i)\b(signature:\s*)[0-9a-f]{32,}"), rf"\1{MASK}"),
    # jeton d'appareil dans le chemin APNs
    (re.compile(r"(/3/device/)[0-9A-Za-z]+"), rf"\1{MASK}"),
    # JWT du fournisseur APNs, clé du backoffice : tout `Bearer <jeton>`
    (re.compile(r"(?i)\b(bearer)([\s:=]+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1\2{MASK}"),
    # mot de passe d'une adresse `schéma://utilisateur:motdepasse@hôte` (base de données)
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), rf"\1{MASK}\2"),
)


def redact(text: str) -> str:
    """`text` sans les secrets connus (remplacés par `***`) ; inchangé s'il n'en porte pas."""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    """Filtre de handler : masque le message (arguments compris) et la trace de l'exception.

    Posé sur un handler et non sur un logger : le filtre d'un logger ne voit pas ce que ses
    descendants (`httpcore.http11`, `botocore.auth`…) émettent.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # message mal formé : logging s'en plaindra lui-même
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            # `Formatter.format` reprend `exc_text` s'il est déjà là : la trace sort masquée
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class AccessQueryFilter(logging.Filter):
    """Journal d'accès d'uvicorn sans la query string (14 nov. 2026) : `GET /weather?lat=…&lon=…`
    écrivait la position demandée à chaque requête. Restent la méthode, le chemin et le code.

    Posé sur le logger `uvicorn.access`, dont l'enregistrement porte ses cinq arguments (adresse
    de la connexion, méthode, chemin complet, version HTTP, code) : seul le chemin est réécrit,
    le formateur d'uvicorn retrouve ses arguments. Un enregistrement d'une autre forme perd tout
    ce qui suit un `?` dans son message."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], strip_query(args[2]), *args[3:])
            return True
        try:
            message = record.getMessage()
        except Exception:  # message mal formé : logging s'en plaindra lui-même
            return True
        clean = _QUERY.sub("", message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


_QUERY = re.compile(r"\?[^\s\"']*")


def strip_query(path: str) -> str:
    """Le chemin d'une requête sans sa query string."""
    return path.split("?", 1)[0]


def install_access_filter(logger: logging.Logger) -> None:
    """Pose `AccessQueryFilter` sur ce logger (une seule fois)."""
    if not any(isinstance(f, AccessQueryFilter) for f in logger.filters):
        logger.addFilter(AccessQueryFilter())


def quiet_libraries(level: int = logging.WARNING) -> None:
    """Met les journaux des clients HTTP et S3 à `level` (WARNING : aucune adresse écrite)."""
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(level)


def install(*loggers: logging.Logger) -> None:
    """Pose le filtre de masquage sur les handlers de ces loggers (une seule fois par handler)."""
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(f, RedactFilter) for f in handler.filters):
                handler.addFilter(RedactFilter())
