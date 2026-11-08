"""Service d'exécution `champis-jobs` (spec › Backoffice › Tâches, lot BO3) : `champis-api worker`.

Toutes les `poll_s` secondes, prend la plus ancienne tâche `queued` du journal (`jobs`), une seule
à la fois, la lance en sous-processus (`tools.command`) avec `CHAMPIS_JOB_ID` : la commande
s'inscrit alors sur la ligne déjà créée par le backoffice (étapes, progression, journal dans le
bucket, voir `jobs.register`). Le worker bat (`workers.heartbeat_at`, disque de son volume, tâche
en cours) toutes les `heartbeat_s` secondes ; une tâche `queued` dont `cancel_requested` est posé
est annulée, une tâche `running` reçoit SIGTERM puis SIGKILL après `grace_s`. Une sorte exclusive
(pré-chauffe, notification, amorçage) attend qu'aucune autre tâche de la même sorte ne tourne,
cron compris : le cron s'inscrit aussi dans `jobs`.

Même image Docker que l'API, son propre volume (un volume ne se monte que sur un service),
variables du bucket, `DATABASE_URL`, clé Open-Meteo, compte CDS ; pas de port.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from champis_pipeline import console

from . import tools
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)

POLL_S = 5.0
HEARTBEAT_S = 30.0
GRACE_S = 30.0
EXIT_CANCELLED = 130


def inventory() -> dict[str, Any]:
    """Ce qu'il y a sur le volume, en noms seulement (lot BO5) : par espèce, les T à jour et
    périmés ; les PMTiles présents ; l'état du batch. Quelques ko, publiés avec le battement :
    l'API n'a pas le volume et c'est d'ici que la fiche d'une espèce sait où elle en est."""
    from champis_pipeline import batch, config
    from champis_pipeline.species import list_species, species_hash

    t: dict[str, dict[str, int]] = {}
    for sid in list_species():
        folder = config.T_DIR / sid
        current = stale = 0
        if folder.is_dir():
            h = species_hash(sid)
            for f in folder.glob("*.tif"):
                if f.stem.endswith(f"-{h}"):
                    current += 1
                else:
                    stale += 1
        t[sid] = {"current": current, "stale": stale}
    tiles = (
        sorted(p.name for p in config.TILES_DIR.glob("*.pmtiles"))
        if config.TILES_DIR.is_dir()
        else []
    )
    status = batch.load_status()
    finished = [v.get("finished", "") for v in status.values() if v.get("finished")]
    return {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_dir": str(config.DATA_DIR),
        "depts": len(batch.DEPTS_ALL),
        "t": t,
        "tiles": tiles,
        "batch": {
            "ok": sum(1 for v in status.values() if v.get("ok")),
            "failed": sum(1 for v in status.values() if v.get("ok") is False),
            "last": max(finished) if finished else None,
        },
    }


def disk_mb(path: Path) -> tuple[int | None, int | None]:
    probe = path if path.exists() else path.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None, None
    return usage.free // 1_000_000, usage.total // 1_000_000


class Worker:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        name: str | None = None,
        poll_s: float = POLL_S,
        heartbeat_s: float = HEARTBEAT_S,
        grace_s: float = GRACE_S,
        command: Callable[[str, dict], list[str]] = tools.command,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self.name = name or socket.gethostname()[:80]
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        self.grace_s = grace_s
        self.command = command
        self.clock = clock
        self.sleep = sleep
        self.started = datetime.now(UTC)
        self.stop = False

    # ------------------------------------------------------------------ battement

    def heartbeat(self, current_job: str | None) -> None:
        free, total = disk_mb(self.settings.cache_db.parent)
        try:
            inv = inventory()
        except Exception:  # un inventaire qui échoue ne prive pas du battement
            log.exception("worker : inventaire du volume impossible")
            inv = None
        try:
            self.store.worker_heartbeat(
                self.name,
                datetime.now(UTC),
                current_job,
                free,
                total,
                self.settings.build,
                self.started,
                inventory=inv,
            )
        except Exception:
            log.exception("worker : battement non enregistré")

    # ------------------------------------------------------------------ file

    def next_job(self) -> dict | None:
        """La plus ancienne tâche en file dont la sorte peut tourner maintenant ; les annulations
        demandées en file sont honorées au passage."""
        rows = self.store.jobs_with_status(("queued", "running"))
        running_kinds = {j["kind"] for j in rows if j["status"] == "running"}
        for job in rows:
            if job["status"] != "queued":
                continue
            if job["cancel_requested"]:
                self.store.finish_job(job["id"], "cancelled", EXIT_CANCELLED, datetime.now(UTC))
                continue
            tool = tools.TOOLS.get(job["kind"])
            if tool is None:
                self.store.add_event(
                    job["id"], "error", f"outil inconnu : {job['kind']}", datetime.now(UTC)
                )
                self.store.finish_job(job["id"], "failed", 2, datetime.now(UTC))
                continue
            if tool.exclusive and job["kind"] in running_kinds:
                continue  # une autre passe de la même sorte tourne (cron ou worker) : on attend
            return job
        return None

    # ------------------------------------------------------------------ exécution

    def run_job(self, job: dict) -> int:
        """Lance la commande, bat, honore l'annulation ; renvoie le code de sortie."""
        now = datetime.now(UTC)
        if not self.store.claim_job(job["id"], self.name, now):
            return -1  # prise par un autre worker entre-temps
        try:
            argv = self.command(job["kind"], job["args"])
        except tools.ToolError as e:
            self.store.add_event(job["id"], "error", str(e), datetime.now(UTC))
            self.store.finish_job(job["id"], "failed", 2, datetime.now(UTC))
            return 2
        tool = tools.TOOLS.get(job["kind"])
        free, _ = disk_mb(self.settings.cache_db.parent)
        if tool and tool.min_free_gb and free is not None and free < tool.min_free_gb * 1000:
            text = (
                f"volume trop plein : {free / 1000:.1f} Go libres, {tool.min_free_gb:g} Go requis "
                "pour cet outil (effacer des entrées brutes ou agrandir le volume)"
            )
            self.store.add_event(job["id"], "error", text, datetime.now(UTC))
            self.store.finish_job(job["id"], "failed", 3, datetime.now(UTC), text)
            return 3
        env = {**os.environ, "CHAMPIS_JOB_ID": job["id"], "CHAMPIS_JOB_ORIGIN": job["origin"]}
        self.store.add_event(
            job["id"], "info", f"{self.name} : {' '.join(argv[3:])}", datetime.now(UTC)
        )
        proc = subprocess.Popen(argv, env=env)
        self.heartbeat(job["id"])
        last_beat = self.clock()
        cancelled_at: float | None = None
        while True:
            code = proc.poll()
            if code is not None:
                break
            now_s = self.clock()
            if now_s - last_beat >= self.heartbeat_s:
                self.heartbeat(job["id"])
                last_beat = now_s
            current = self.store.job(job["id"])
            if current and current["cancel_requested"] and cancelled_at is None:
                self.store.add_event(job["id"], "warn", "annulation demandée", datetime.now(UTC))
                proc.send_signal(signal.SIGTERM)
                cancelled_at = now_s
            elif cancelled_at is not None and now_s - cancelled_at >= self.grace_s:
                proc.kill()
                cancelled_at = -1e9  # une seule fois
            self.sleep(1.0)
        final = self.store.job(job["id"])
        if final and final["status"] == "running":  # la commande n'a pas clos sa ligne
            status = "cancelled" if cancelled_at is not None else ("ok" if code == 0 else "failed")
            self.store.finish_job(job["id"], status, code, datetime.now(UTC))
        elif cancelled_at is not None and final and final["status"] != "cancelled":
            self.store.update_job(job["id"], status="cancelled")
        self.heartbeat(None)
        return code

    def run(self, once: bool = False) -> int:
        """Boucle du service ; `once` : traite au plus une tâche puis rend la main (tests)."""
        c = console.get()
        c.info(f"worker {self.name} : file interrogée toutes les {self.poll_s:g} s")
        self.heartbeat(None)
        last_beat = self.clock()
        while not self.stop:
            job = None
            try:
                job = self.next_job()
            except Exception:
                log.exception("worker : file illisible")
            if job is not None:
                c.title(f"{job['kind']} {job['id']}")
                code = self.run_job(job)
                c.info(f"{job['kind']} {job['id']} : code {code}")
                if once:
                    return code
                last_beat = self.clock()
                continue
            if once:
                return 0
            if self.clock() - last_beat >= self.heartbeat_s:
                self.heartbeat(None)
                last_beat = self.clock()
            self.sleep(self.poll_s)
        return 0


def main(settings: Settings, once: bool = False, poll_s: float = POLL_S) -> int:
    if not settings.database_url:
        raise SystemExit("pas de base (DATABASE_URL) : aucune file à servir")
    store = Store(settings.database_url)
    worker = Worker(settings, store, poll_s=poll_s)

    def _stop(*_):
        worker.stop = True

    signal.signal(signal.SIGTERM, _stop)
    try:
        return worker.run(once=once)
    finally:
        store.close()
