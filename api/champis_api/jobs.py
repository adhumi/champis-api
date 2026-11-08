"""Journal des tâches (spec › Backoffice, 7 nov. 2026 ; lot BO1).

Chaque exécution d'une commande `champis-api` qui fait quelque chose (pré-chauffe, notification,
amorçage, publications) s'inscrit dans la table `jobs` de la base des observations, avec l'étape
en cours, sa progression et ses événements (`job_events`) : les crons Railway comme les outils
lancés depuis le backoffice (lot BO3) ou la CLI sur un poste. Le backoffice lit ces tables ; une
passe absente ou en échec se voit.

Qui écrit : `JobSink`, branché sur la console (`champis_pipeline.console.Sink`) par `register`
quand `DATABASE_URL` est fournie. Il ne lève jamais : une base injoignable débranche le puits
(`Console.emit`) et la commande continue. L'identifiant de la tâche vient de `CHAMPIS_JOB_ID`
quand le service d'exécution l'a créée d'avance (lot BO3), sinon il est tiré ici ; l'origine vient
de `CHAMPIS_JOB_ORIGIN` (`cron` sur les services Railway, `admin` depuis le backoffice), `cli`
par défaut.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from champis_pipeline import console

from .store import Store

log = logging.getLogger(__name__)

KEEP_DAYS = 400  # 13 mois, comme les compteurs de la mesure minimale
LOG_PREFIX = "jobs/"  # journal complet dans le bucket : jobs/<id>.log

# commandes qui s'inscrivent, et leur libellé dans le backoffice ; les lectures (doctor, stats) non
KINDS: dict[str, str] = {
    "warm": "pré-chauffe",
    "notify": "notification « mes coins »",
    "seed": "amorçage météo",
    "publish-seed": "publication de l'amorçage",
    "publish-cache": "publication du cache",
}

# ce que les crons Railway sont censés faire : au-delà, la page Santé alerte
EXPECTED_EVERY_H: dict[str, float] = {"warm": 14.0, "notify": 26.0}


class JobSink:
    """Écrit les événements de la console dans `jobs` et `job_events`.

    La progression est lissée : au plus une écriture toutes les `min_interval_s` secondes ou tous
    les `min_fraction` du total, et toujours la dernière valeur.
    """

    def __init__(
        self,
        store: Store,
        job_id: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = 2.0,
        min_fraction: float = 0.01,
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.now = now
        self.clock = clock
        self.min_interval_s = min_interval_s
        self.min_fraction = min_fraction
        self._last_progress_at = -1e9
        self._last_progress_done = -1
        self.summary_text: str | None = None

    def step_begin(self, index: int, total: int, name: str) -> None:
        ts = self.now()
        self.store.update_job(
            self.job_id,
            step_index=index,
            step_total=total,
            step_name=name[:160],
            progress_done=None,
            progress_total=None,
            progress_unit=None,
            heartbeat_at=ts,
        )
        self.store.add_event(self.job_id, "step", f"[{index}/{total}] {name}", ts)
        self._last_progress_at = -1e9
        self._last_progress_done = -1

    def step_end(
        self, index: int, total: int, name: str, status: str, detail: str, seconds: float
    ) -> None:
        ts = self.now()
        level = "error" if status == "ECHEC" else "step"
        text = f"[{index}/{total}] {name} : {status} en {console.duration(seconds)}"
        if detail:
            text += f" · {detail}"
        self.store.add_event(self.job_id, level, text, ts)
        fields: dict[str, Any] = {"heartbeat_at": ts}
        if detail:
            fields["message"] = detail[:2000]
        self.store.update_job(self.job_id, **fields)

    def event(self, level: str, text: str) -> None:
        self.store.add_event(self.job_id, level, text, self.now())

    def progress(self, done: int, total: int | None, unit: str, detail: str) -> None:
        t = self.clock()
        step = max(1, int((total or 0) * self.min_fraction))
        due = (
            t - self._last_progress_at >= self.min_interval_s
            or done - self._last_progress_done >= step
            or (total is not None and done >= total)
        )
        if not due:
            return
        self._last_progress_at = t
        self._last_progress_done = done
        fields: dict[str, Any] = {
            "progress_done": done,
            "progress_total": total,
            "progress_unit": unit[:24],
            "heartbeat_at": self.now(),
        }
        if detail:
            fields["message"] = detail[:2000]
        self.store.update_job(self.job_id, **fields)

    def summary(self, text: str) -> None:
        self.summary_text = text
        self.store.update_job(self.job_id, summary=text[:2000], heartbeat_at=self.now())


class Registration:
    """Une exécution inscrite : à clore avec `close(code)` une fois la commande terminée."""

    def __init__(self, store: Store, job_id: str, sink: JobSink) -> None:
        self.store = store
        self.job_id = job_id
        self.sink = sink

    def close(self, code: int, log_path: Path | None, bucket: Any | None) -> None:
        now = datetime.now(UTC)
        try:
            status = "ok" if code == 0 else ("cancelled" if code == 130 else "failed")
            self.store.finish_job(self.job_id, status, code, now, self.sink.summary_text)
            if bucket is not None and log_path is not None and log_path.exists():
                key = f"{LOG_PREFIX}{self.job_id}.log"
                bucket.upload(log_path, key)
                self.store.update_job(self.job_id, log_key=key)
        except Exception:
            log.exception("journal des tâches : clôture non enregistrée")
        finally:
            self.store.close()


def register(
    kind: str, args: dict[str, Any], database_url: str, c: console.Console | None = None
) -> Registration | None:
    """Inscrit l'exécution qui commence et branche le puits sur la console ; None si la commande
    ne s'inscrit pas ou si la base est injoignable (la commande tourne quand même)."""
    if kind not in KINDS or not database_url:
        return None
    c = c or console.get()
    try:
        store = Store(database_url)
        now = datetime.now(UTC)
        job_id = os.environ.get("CHAMPIS_JOB_ID") or None
        origin = os.environ.get("CHAMPIS_JOB_ORIGIN") or "cli"
        if job_id and store.job(job_id) is not None:
            pass  # créée d'avance par le service d'exécution (lot BO3)
        else:
            job_id = store.create_job(kind, args, origin, now, job_id=job_id)
        store.start_job(job_id, socket.gethostname()[:80], now)
        store.purge_jobs(now - timedelta(days=KEEP_DAYS))
    except Exception:
        log.exception("journal des tâches : inscription impossible, la commande continue")
        return None
    sink = JobSink(store, job_id)
    c.sink = sink
    return Registration(store, job_id, sink)


def age_hours(iso: str | None, now: datetime | None = None) -> float | None:
    """Heures écoulées depuis un horodatage ISO (UTC), None sans horodatage."""
    if not iso:
        return None
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - then).total_seconds() / 3600
