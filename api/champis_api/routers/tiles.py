"""`GET /tiles` : manifeste des PMTiles du bucket avec une URL par fichier ; `GET /tiles/{name}` :
relais Range vers le bucket quand le navigateur ne peut pas lire les URL présignées (CORS).

Spec › Points ouverts › hébergement des PMTiles. Sans bucket configuré : `{"configured": false}`
et la carte garde le glisser-déposer.

Cache disque du relais (spec › Points ouverts › Tuiles hors du processus Python, 4 nov. 2026) :
une requête `Range: bytes=a-b` exacte et bornée (≤ `MAX_ENTRY_BYTES`) est servie depuis
`CHAMPIS_TILE_CACHE_DIR` si la plage y est, sinon relayée puis écrite après la réponse (tâche de
fond, écriture atomique, voir `tile_cache`). Même réponse dans les deux cas : 206, `content-range`,
`content-length`, `accept-ranges`, `etag` mémorisé avec l'entrée, `cache-control` immutable ;
`x-cache: hit|miss` en plus. Une requête sans Range, ouverte (`bytes=a-`), en suffixe, multiple ou
trop grande reste un relais en streaming, jamais mise en cache ; `If-None-Match` qui correspond
à l'ETag mémorisé rend 304 comme le bucket. `CHAMPIS_TILE_CACHE_MB=0` désactive le cache.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from champis_pipeline.storage import MANIFEST_KEY, TILES_PREFIX, parse_tile_name
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ..tile_cache import MAX_ENTRY_BYTES, Entry, TileCache, parse_content_range, parse_exact_range

router = APIRouter(tags=["tiles"])

MANIFEST_TTL_S = 600
PROXY_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "etag")
CACHE_CONTROL = "public, max-age=86400, immutable"  # noms de fichiers immuables


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
async def proxy_tile(name: str, request: Request) -> Response:
    """Relais d'un PMTiles (requêtes Range comprises) ; uniquement en mode proxy. Une plage exacte
    et bornée passe par le cache disque (voir le module)."""
    st = _state(request)
    if st.bucket is None or st.tiles_mode != "proxy":
        raise HTTPException(404, "relais désactivé (mode presign)")
    if parse_tile_name(name) is None:
        raise HTTPException(404, "nom de tuiles inconnu")
    cache: TileCache | None = getattr(st, "tile_cache", None)
    rng = parse_exact_range(request.headers.get("range"))
    cacheable = cache is not None and rng is not None and rng[1] - rng[0] + 1 <= MAX_ENTRY_BYTES
    if cacheable:
        hit = await cache.get(name, *rng)
        if hit is not None:
            return _cached_response(hit, request.headers.get("if-none-match"))
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
    out_headers["cache-control"] = CACHE_CONTROL
    out_headers["x-cache"] = "miss"

    background = None
    buffer = bytearray()
    served = parse_content_range(upstream.headers.get("content-range"))
    if cacheable and upstream.status_code == 206 and served is not None:
        a, b, total = served
        entry_len = b - a + 1
        if entry_len <= MAX_ENTRY_BYTES:

            def store() -> None:
                entry = Entry(
                    body=bytes(buffer),
                    start=a,
                    end=b,
                    total=total,
                    etag=upstream.headers.get("etag"),
                    content_type=upstream.headers.get("content-type", "application/octet-stream"),
                )
                cache.put(name, rng[0], rng[1], entry, entry_len)

            # Après l'envoi de la réponse ; `put` ne garde rien si le corps est tronqué (client parti)
            background = BackgroundTask(store)

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                if background is not None:
                    buffer.extend(chunk)
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=out_headers, background=background
    )


def _cached_response(entry: Entry, if_none_match: str | None) -> Response:
    """206 depuis le cache avec les en-têtes du relais ; 304 si l'ETag mémorisé est déjà au client."""
    headers = {"cache-control": CACHE_CONTROL, "x-cache": "hit"}
    if entry.etag and if_none_match and entry.etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers={**headers, "etag": entry.etag})
    return Response(content=entry.body, status_code=206, headers={**entry.headers(), **headers})
