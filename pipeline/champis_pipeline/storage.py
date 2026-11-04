"""Bucket S3-compatible (Railway Storage Bucket) : téléversement des PMTiles, manifeste, URL présignées.

Spec › Points ouverts › hébergement des PMTiles (18 oct. 2026). Le bucket est privé : la carte
reçoit des URL présignées via `GET /tiles` (API), ou passe par le relais `GET /tiles/{name}`.
Configuration par variables d'environnement, lues ici et par l'API :

    AWS_ENDPOINT_URL        https://storage.railway.app — ou CHAMPIS_S3_ENDPOINT
    AWS_S3_BUCKET_NAME      nom du bucket                 — ou CHAMPIS_S3_BUCKET
    AWS_DEFAULT_REGION      défaut « auto »               — ou CHAMPIS_S3_REGION
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   identifiants du bucket

Les noms AWS_* sont ceux du préréglage « AWS SDK (Generic) » de Railway (Connect Service to
Bucket) ; les CHAMPIS_S3_* priment s'ils sont posés.

`Bucket` n'importe boto3 qu'à la construction : les tests passent un objet au même contrat.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

TILES_PREFIX = "tiles/"
MANIFEST_KEY = TILES_PREFIX + "manifest.json"
MANIFEST_VERSION = 1

_T_NAME = re.compile(
    r"^T-(?P<species>[a-z_]+)-(?P<hash>[0-9a-f]{8})-p(?P<version>[\d.]+)-d(?P<dh>[0-9a-f]{6})\.pmtiles$"
)
_A_NAME = re.compile(r"^A-(?P<class_m>\d+)m-d(?P<dh>[0-9a-f]{6})\.pmtiles$")
_G_NAME = re.compile(
    r"^G-(?P<group>[a-z_]+)-(?P<hash>[0-9a-f]{8})-p(?P<version>[\d.]+)-d(?P<dh>[0-9a-f]{6})"
    r"(?:-(?P<part>[2-9]|[1-9]\d+))?\.pmtiles$"
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    region: str = "auto"
    access_key: str = ""
    secret_key: str = ""

    @classmethod
    def from_env(cls) -> S3Config | None:
        """None si le bucket n'est pas configuré (développement local, tests)."""
        env = os.environ.get
        endpoint = env("CHAMPIS_S3_ENDPOINT") or env("AWS_ENDPOINT_URL")
        bucket = env("CHAMPIS_S3_BUCKET") or env("AWS_S3_BUCKET_NAME")
        if not endpoint or not bucket:
            return None
        return cls(
            endpoint=endpoint,
            bucket=bucket,
            region=env("CHAMPIS_S3_REGION") or env("AWS_DEFAULT_REGION") or "auto",
            access_key=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        )


class BucketLike(Protocol):
    def list(self, prefix: str) -> dict[str, int]: ...
    def upload(self, path: Path, key: str, progress: Callable[[int], None] | None = None) -> None:
        """`progress(n)` est appelé à chaque tranche de n octets transférés."""
        ...

    def put_json(self, key: str, obj: Any) -> None: ...
    def delete(self, keys: list[str]) -> None: ...
    def download(self, key: str, path: Path) -> None: ...
    def get_json(self, key: str) -> Any | None: ...
    def presign(self, key: str, expires_s: int) -> str: ...


class Bucket:
    """Contrat minimal au-dessus de boto3 (voir `BucketLike`)."""

    def __init__(self, cfg: S3Config):
        import boto3
        from botocore.config import Config

        self.name = cfg.bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            region_name=cfg.region,
            aws_access_key_id=cfg.access_key or None,
            aws_secret_access_key=cfg.secret_key or None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    def list(self, prefix: str) -> dict[str, int]:
        out: dict[str, int] = {}
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.name, Prefix=prefix):
            for obj in page.get("Contents", []):
                out[obj["Key"]] = int(obj["Size"])
        return out

    def upload(self, path: Path, key: str, progress: Callable[[int], None] | None = None) -> None:
        self._s3.upload_file(str(path), self.name, key, Callback=progress)

    def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.name, key, str(path))

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):  # limite S3 par requête
            objs = [{"Key": k} for k in keys[i : i + 1000]]
            self._s3.delete_objects(Bucket=self.name, Delete={"Objects": objs, "Quiet": True})

    def put_json(self, key: str, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self._s3.put_object(Bucket=self.name, Key=key, Body=body, ContentType="application/json")

    def get_json(self, key: str) -> Any | None:
        try:
            r = self._s3.get_object(Bucket=self.name, Key=key)
        except self._s3.exceptions.NoSuchKey:
            return None
        return json.loads(r["Body"].read().decode("utf-8"))

    def presign(self, key: str, expires_s: int) -> str:
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.name, "Key": key}, ExpiresIn=expires_s
        )


def parse_tile_name(name: str) -> dict[str, Any] | None:
    """`T-<espèce>-<hash>-p<version>-d<dh>.pmtiles`, `A-<classe>m-d<dh>.pmtiles` ou
    `G-<groupe>-<hash>-p<version>-d<dh>[-<partie>].pmtiles` → champs (`part` seulement s'il y en a
    une ; les espèces d'un G, dans l'ordre des canaux, viennent de `build_manifest`)."""
    m = _T_NAME.match(name)
    if m:
        return {
            "kind": "T",
            "species": m["species"],
            "hash": m["hash"],
            "scoring_version": m["version"],
            "depts_hash": m["dh"],
        }
    m = _A_NAME.match(name)
    if m:
        return {"kind": "A", "class_m": int(m["class_m"]), "depts_hash": m["dh"]}
    m = _G_NAME.match(name)
    if m:
        info = {
            "kind": "G",
            "group": m["group"],
            "hash": m["hash"],
            "scoring_version": m["version"],
            "depts_hash": m["dh"],
        }
        if m["part"]:
            info["part"] = int(m["part"])
        return info
    return None


def group_key(group: str, hash_: str) -> str:
    """Clé de `group_species` : un G est identifié par son groupe et l'empreinte de ses canaux."""
    return f"{group}-{hash_}"


def build_manifest(
    objects: dict[str, int],
    depts_by_hash: dict[str, list[str]],
    generated: str,
    group_species: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Manifeste à partir du contenu du bucket (tous les PMTiles sous `tiles/`), trié par nom.

    `group_species` : `group_key(groupe, hash)` → identifiants d'espèces dans l'ordre des canaux,
    recopiés dans chaque entrée G (`species`) ; un G dont on ne sait plus les espèces (fichier d'un
    modèle qui n'est plus dans `species/` ni dans l'ancien manifeste) est écarté, avec un
    avertissement : un client ne doit jamais deviner l'ordre des canaux."""
    tiles = []
    for key, size in sorted(objects.items()):
        name = key.removeprefix(TILES_PREFIX)
        info = parse_tile_name(name)
        if info is None:
            continue
        if info["kind"] == "G":
            species = (group_species or {}).get(group_key(info["group"], info["hash"]))
            if species is None:
                log.warning("%s : espèces des canaux inconnues, hors du manifeste", name)
                continue
            info["species"] = list(species)
        entry = {**info, "name": name, "key": key, "bytes": size}
        members = depts_by_hash.get(info["depts_hash"])
        if members and len(members) == 1:
            entry["dept"] = members[0]  # jeu d'un seul département : hors ligne de l'app
        tiles.append(entry)
    return {
        "version": MANIFEST_VERSION,
        "generated": generated,
        "depts_by_hash": depts_by_hash,
        "tiles": tiles,
    }
