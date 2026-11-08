"""Page Santé du backoffice : `GET /admin/api/health` (spec › Backoffice › Santé de l'API).

Réutilise les contrôles de `champis-api doctor` (`doctor.run_checks`), complétés de ce que seul le
service en ligne connaît : état du cache météo en mémoire, volume, cache des tuiles, base, journal
des tâches, bucket (manifeste des tuiles, cache et amorçage publiés), trafic (429, 5xx). Chaque
alerte dit quoi et depuis quand ; rien n'est stocké pour cette page hormis les deux compteurs de
trafic, écrits par le middleware de l'API. Lecture seule, réseau pour le bucket seulement.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from champis_pipeline.storage import MANIFEST_KEY, BucketLike

from .. import counts, doctor, jobs
from ..seed import SEED_META_KEY
from ..settings import Settings
from ..snapshot import META_CACHE_PUBLISHED, published_cache
from ..store import Store

CACHE_MAX_AGE_H = 14.0  # deux passes par jour : au-delà, la pré-chauffe a manqué
MIN_FREE_BYTES = 5e9
TRAFFIC_DAYS = 7
TRAFFIC_COUNTERS = ("http_429", "http_5xx")


def _alert(alerts: list[dict[str, str]], level: str, text: str) -> None:
    alerts.append({"level": level, "text": text})


def cache_section(state: Any, alerts: list[dict[str, str]]) -> dict[str, Any]:
    cache = state.cache
    meta = cache.dept_index_meta()
    published = cache.meta_get(META_CACHE_PUBLISHED)
    age = jobs.age_hours(published)
    cells, seeded = len(state.grid), cache.seeded_cells()
    if published is None:
        _alert(alerts, "warn", "aucun cache publié par le cron n'a encore été importé")
    elif age is not None and age > CACHE_MAX_AGE_H:
        _alert(alerts, "warn", f"le cache publié a {age:.0f} h (deux passes par jour attendues)")
    if seeded < cells:
        _alert(alerts, "warn", f"{cells - seeded} cellules sur {cells} sans amorçage")
    return {
        "cells": cells,
        "seeded": seeded,
        "dept_index_at": meta[0] if meta else None,
        "today": meta[1] if meta else None,
        "cache_published": published,
        "cache_published_age_h": round(age, 1) if age is not None else None,
        "cell_weather": cache.cell_weather_count(),
        "cell_field": cache.cell_field_count(),
    }


def disk_section(settings: Settings, alerts: list[dict[str, str]]) -> dict[str, Any]:
    root = settings.cache_db.parent
    probe = root if root.exists() else root.parent
    usage = shutil.disk_usage(probe)
    if usage.free < MIN_FREE_BYTES:
        _alert(alerts, "error", f"moins de 5 Go libres sur {root}")
    return {
        "path": str(root),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "cache_db_bytes": settings.cache_db.stat().st_size if settings.cache_db.exists() else 0,
    }


def tile_cache_section(state: Any, settings: Settings) -> dict[str, Any] | None:
    cache = state.tile_cache
    if cache is None:
        return None
    _, st = cache.scan(budget_s=2.0)
    return {
        "root": str(cache.root),
        "entries": st.entries,
        "bytes": st.bytes,
        "max_bytes": settings.tile_cache_mb * 1_000_000,
        "complete": st.complete,
    }


def database_section(store: Store | None, alerts: list[dict[str, str]]) -> dict[str, Any] | None:
    if store is None:
        _alert(alerts, "error", "pas de base : observations, coins et journal des tâches en 503")
        return None
    try:
        return {"dialect": store.dialect, "tables": store.table_counts()}
    except Exception as e:
        _alert(alerts, "error", f"base injoignable : {type(e).__name__}")
        return {"dialect": store.dialect, "error": type(e).__name__}


def jobs_section(
    store: Store | None, alerts: list[dict[str, str]], now: datetime
) -> dict[str, Any]:
    if store is None:
        return {}
    try:
        last = store.last_jobs_by_kind()
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for kind, label in jobs.KINDS.items():
        job = last.get(kind)
        expected = jobs.EXPECTED_EVERY_H.get(kind)
        entry: dict[str, Any] = {"label": label, "expected_every_h": expected, "last": job}
        if job is not None:
            entry["age_h"] = jobs.age_hours(job["created_at"], now)
            if job["status"] == "failed":
                _alert(alerts, "error", f"{label} : dernière exécution en échec")
            elif expected and entry["age_h"] is not None and entry["age_h"] > expected:
                _alert(alerts, "warn", f"{label} : aucune exécution depuis {entry['age_h']:.0f} h")
        elif expected:
            _alert(alerts, "info", f"{label} : aucune exécution inscrite (cron pas encore passé)")
        out[kind] = entry
    return out


WORKER_MAX_SILENCE_MIN = 2.0


def workers_section(
    store: Store | None, alerts: list[dict[str, str]], now: datetime
) -> list[dict[str, Any]]:
    """Le service d'exécution champis-jobs (lot BO3) : dernier battement, disque, tâche en cours."""
    if store is None:
        return []
    try:
        rows = store.list_workers()
    except Exception:
        return []
    for w in rows:
        age = jobs.age_hours(w["heartbeat_at"], now)
        w["silent_min"] = round(age * 60, 1) if age is not None else None
        if age is not None and age * 60 > WORKER_MAX_SILENCE_MIN:
            _alert(
                alerts, "warn", f"service d'exécution {w['name']} muet depuis {age * 60:.0f} min"
            )
        if w.get("disk_free_mb") is not None and w["disk_free_mb"] < 5000:
            _alert(alerts, "error", f"moins de 5 Go libres sur le volume de {w['name']}")
    return rows


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Les jeux publiés : par empreinte de départements, le nombre de fichiers par sorte."""
    sets: dict[str, dict[str, Any]] = {}
    total = 0
    for t in manifest.get("tiles", []):
        h = t.get("depts_hash", "?")
        entry = sets.setdefault(
            h,
            {
                "hash": h,
                "depts": manifest.get("depts_by_hash", {}).get(h, []),
                "kinds": {},
                "bytes": 0,
            },
        )
        kind = t.get("kind", "T")
        entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
        entry["bytes"] += int(t.get("bytes", 0))
        total += int(t.get("bytes", 0))
    ordered = sorted(sets.values(), key=lambda s: -len(s["depts"]))
    return {
        "generated": manifest.get("generated"),
        "tiles": len(manifest.get("tiles", [])),
        "bytes": total,
        "sets": ordered[:12],
        "per_dept_sets": sum(1 for s in ordered if len(s["depts"]) == 1),
    }


def bucket_section(bucket: BucketLike | None, alerts: list[dict[str, str]]) -> dict[str, Any]:
    if bucket is None:
        _alert(alerts, "error", "bucket non configuré : /tiles vide")
        return {"configured": False}
    try:
        manifest = bucket.get_json(MANIFEST_KEY)
        cache_meta = published_cache(bucket)
        seed_meta = bucket.get_json(SEED_META_KEY)
    except Exception as e:
        _alert(alerts, "error", f"bucket injoignable : {type(e).__name__}")
        return {"configured": True, "error": type(e).__name__}
    if not manifest:
        _alert(alerts, "warn", "aucun manifeste de tuiles dans le bucket")
    return {
        "configured": True,
        "manifest": manifest_summary(manifest) if manifest else None,
        "cache": cache_meta,
        "seed": seed_meta,
    }


def traffic_section(
    store: Store | None, alerts: list[dict[str, str]], today
) -> dict[str, dict[str, int]]:
    if store is None:
        return {}
    try:
        data = store.counts_since(today - timedelta(days=TRAFFIC_DAYS))
    except Exception:
        return {}
    out = {
        d.isoformat(): {k: v for k, v in values.items() if k in TRAFFIC_COUNTERS}
        for d, values in sorted(data.items())
    }
    yesterday = (today - timedelta(days=1)).isoformat()
    if out.get(yesterday, {}).get("http_5xx"):
        _alert(alerts, "warn", f"{out[yesterday]['http_5xx']} réponses 5xx hier")
    return out


def gather(
    settings: Settings, state: Any, started_at: datetime, now: datetime | None = None
) -> dict[str, Any]:
    """Tout ce que la page Santé affiche, alertes en tête (synchrone : à lancer dans un fil)."""
    now = now or datetime.now(UTC)
    alerts: list[dict[str, str]] = []
    store = state.store
    out: dict[str, Any] = {
        "generated": now.isoformat(timespec="seconds"),
        "api": {
            "build": settings.build,
            "started_at": started_at.astimezone(UTC).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "workers": os.environ.get("CHAMPIS_WORKERS", "2"),
            "tiles_mode": settings.tiles_mode,
        },
        "cache": cache_section(state, alerts),
        "disk": disk_section(settings, alerts),
        "tile_cache": tile_cache_section(state, settings),
        "database": database_section(store, alerts),
        "jobs": jobs_section(store, alerts, now),
        "workers": workers_section(store, alerts, now),
        "bucket": bucket_section(state.bucket, alerts),
        "traffic": traffic_section(store, alerts, counts.today()),
    }
    checks = doctor.run_checks(settings, offline=True)  # le bucket est déjà interrogé ci-dessus
    for k in checks:
        if k.status == doctor.MISSING:
            _alert(alerts, "warn", f"{k.name} : {k.detail}" + (f" ({k.hint})" if k.hint else ""))
    out["checks"] = [asdict(k) for k in checks]
    order = {"error": 0, "warn": 1, "info": 2}
    out["alerts"] = sorted(alerts, key=lambda a: order.get(a["level"], 3))
    return out
