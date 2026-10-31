"""`storage.Bucket` : le contrat `BucketLike` au-dessus de boto3, sans réseau (botocore Stubber).

Spec › Points ouverts › stockage des tuiles : bucket privé S3-compatible (Railway), URL présignées
24 h en mode presign, suppressions par lots de 1 000 (limite S3), manifeste JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from botocore.stub import Stubber
from champis_pipeline.storage import Bucket, S3Config


@pytest.fixture
def bucket():
    b = Bucket(
        S3Config(
            endpoint="https://storage.railway.app", bucket="champis", access_key="k", secret_key="s"
        )
    )
    with Stubber(b._s3) as stub:
        yield b, stub
        stub.assert_no_pending_responses()


def test_list_follows_pagination_and_returns_sizes(bucket):
    b, stub = bucket
    stub.add_response(
        "list_objects_v2",
        {
            "Contents": [{"Key": "tiles/a.pmtiles", "Size": 10}],
            "IsTruncated": True,
            "NextContinuationToken": "t1",
        },
        {"Bucket": "champis", "Prefix": "tiles/"},
    )
    stub.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "tiles/b.pmtiles", "Size": 20}], "IsTruncated": False},
        {"Bucket": "champis", "Prefix": "tiles/", "ContinuationToken": "t1"},
    )
    assert b.list("tiles/") == {"tiles/a.pmtiles": 10, "tiles/b.pmtiles": 20}
    stub.add_response(
        "list_objects_v2", {"IsTruncated": False}, {"Bucket": "champis", "Prefix": "vide/"}
    )
    assert b.list("vide/") == {}


def test_json_roundtrip_and_missing_key(bucket):
    b, stub = bucket
    manifest = {"version": 1, "tiles": [], "é": "accents gardés"}
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": "champis",
            "Key": "tiles/manifest.json",
            "Body": json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"),
            "ContentType": "application/json",
        },
    )
    b.put_json("tiles/manifest.json", manifest)

    import io

    from botocore.response import StreamingBody

    body = json.dumps(manifest).encode()
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(body), len(body))},
        {"Bucket": "champis", "Key": "tiles/manifest.json"},
    )
    assert b.get_json("tiles/manifest.json") == manifest
    stub.add_client_error(
        "get_object", "NoSuchKey", expected_params={"Bucket": "champis", "Key": "absent.json"}
    )
    assert b.get_json("absent.json") is None


def test_delete_batches_by_thousand(bucket):
    b, stub = bucket
    keys = [f"tiles/{i}.pmtiles" for i in range(1500)]
    for chunk in (keys[:1000], keys[1000:]):
        stub.add_response(
            "delete_objects",
            {},
            {
                "Bucket": "champis",
                "Delete": {"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            },
        )
    b.delete(keys)
    b.delete([])  # rien à faire, aucun appel


def test_presign_is_a_time_limited_virtual_host_url(bucket):
    b, _ = bucket
    url = b.presign("tiles/T.pmtiles", 24 * 3600)
    assert url.startswith("https://champis.storage.railway.app/tiles/T.pmtiles?")
    assert "X-Amz-Expires=86400" in url and "X-Amz-Signature=" in url


def test_upload_and_download_use_managed_transfers(bucket, tmp_path: Path, monkeypatch):
    """`upload_file`/`download_file` (transferts gérés, Range et multipart) ; `progress` reçoit les octets."""
    b, _ = bucket
    seen = {}
    monkeypatch.setattr(
        b._s3,
        "upload_file",
        lambda path, name, key, **kw: seen.update(up=(path, name, key, kw.get("Callback"))),
    )
    monkeypatch.setattr(
        b._s3, "download_file", lambda name, key, path: Path(path).write_bytes(b"data")
    )
    src = tmp_path / "x.pmtiles"
    src.write_bytes(b"xyz")
    ticks = []
    b.upload(src, "tiles/x.pmtiles", progress=ticks.append)
    assert seen["up"] == (str(src), "champis", "tiles/x.pmtiles", ticks.append)
    dest = tmp_path / "deep" / "dir" / "seed.db"
    b.download("weather/weather_seed.db", dest)
    assert dest.read_bytes() == b"data"  # dossier créé au passage
