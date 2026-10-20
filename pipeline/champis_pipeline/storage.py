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
import os
import re
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
    def upload(self, path: Path, key: str) -> None: ...
    def put_json(self, key: str, obj: Any) -> None: ...
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

    def upload(self, path: Path, key: str) -> None:
        self._s3.upload_file(str(path), self.name, key)

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
    """`T-<espèce>-<hash>-p<version>-d<dh>.pmtiles` ou `A-<classe>m-d<dh>.pmtiles` → champs."""
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
    return None


def build_manifest(
    objects: dict[str, int], depts_by_hash: dict[str, list[str]], generated: str
) -> dict[str, Any]:
    """Manifeste à partir du contenu du bucket (tous les PMTiles sous `tiles/`), trié par nom."""
    tiles = []
    for key, size in sorted(objects.items()):
        name = key.removeprefix(TILES_PREFIX)
        info = parse_tile_name(name)
        if info is None:
            continue
        tiles.append({**info, "name": name, "key": key, "bytes": size})
    return {
        "version": MANIFEST_VERSION,
        "generated": generated,
        "depts_by_hash": depts_by_hash,
        "tiles": tiles,
    }
