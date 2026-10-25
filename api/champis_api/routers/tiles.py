"""`GET /tiles` : manifeste des PMTiles du bucket avec une URL par fichier ; `GET /tiles/{name}` :
relais Range vers le bucket quand le navigateur ne peut pas lire les URL présignées (CORS).

Spec › Points ouverts › hébergement des PMTiles. Sans bucket configuré : `{"configured": false}`
et la carte garde le glisser-déposer.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from champis_pipeline.storage import MANIFEST_KEY, TILES_PREFIX, parse_tile_name
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["tiles"])

MANIFEST_TTL_S = 600
PROXY_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "etag")


def _state(request: Request):
    return request.app.state.champis


async def _manifest(st) -> dict[str, Any]:
    """Manifeste du bucket, mis en cache MANIFEST_TTL_S (une lecture S3 par refresh)."""
    cached = getattr(st, "manifest_cache", None)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    manifest = st.bucket.get_json(MANIFEST_KEY) or {"version": 1, "tiles": [], "depts_by_hash": {}}
    st.manifest_cache = (time.monotonic() + MANIFEST_TTL_S, manifest)
    return manifest


@router.get("/tiles")
async def list_tiles(request: Request) -> dict[str, Any]:
    st = _state(request)
    if st.bucket is None:
        return {"configured": False, "mode": None, "tiles": [], "depts_by_hash": {}}
    manifest = await _manifest(st)
    mode = st.tiles_mode
    tiles = []
    for t in manifest.get("tiles", []):
        url = (
            st.bucket.presign(t["key"], st.tiles_url_hours * 3600)
            if mode == "presign"
            else f"/tiles/{t['name']}"
        )
        tiles.append({**t, "url": url})
    return {
        "configured": True,
        "mode": mode,
        "generated": manifest.get("generated"),
        "depts_by_hash": manifest.get("depts_by_hash", {}),
        "tiles": tiles,
    }


@router.get("/tiles/{name}")
async def proxy_tile(name: str, request: Request) -> StreamingResponse:
    """Relais d'un PMTiles (requêtes Range comprises) ; uniquement en mode proxy."""
    st = _state(request)
    if st.bucket is None or st.tiles_mode != "proxy":
        raise HTTPException(404, "relais désactivé (mode presign)")
    if parse_tile_name(name) is None:
        raise HTTPException(404, "nom de tuiles inconnu")
    url = st.bucket.presign(TILES_PREFIX + name, 3600)
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-none-match")}
    http: httpx.AsyncClient = st.client.http
    req = http.build_request("GET", url, headers=headers)
    upstream = await http.send(req, stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(upstream.status_code, "bucket : fichier indisponible")
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in PROXY_HEADERS}
    out_headers.setdefault("accept-ranges", "bytes")
    out_headers["cache-control"] = "public, max-age=86400, immutable"  # noms de fichiers immuables

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=out_headers)
