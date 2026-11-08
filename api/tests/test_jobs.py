"""Journal des tâches (spec › Backoffice, lot BO1) : les tables `jobs` et `job_events`, le puits
branché sur la console, l'inscription d'une commande et sa clôture (journal déposé dans le bucket).
Une base qui manque ou refuse ne fait jamais échouer la commande."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from champis_api import jobs
from champis_api.store import Store
from champis_pipeline import console
from conftest import MemoryBucket

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)


def test_store_keeps_jobs_and_their_events():
    store = Store("sqlite://")
    jid = store.create_job("warm", {"pull": "True"}, "cron", NOW)
    assert store.job(jid)["status"] == "queued" and store.job(jid)["args"] == {"pull": "True"}
    store.start_job(jid, "worker-1", NOW)
    store.update_job(jid, step_index=1, step_total=3, step_name="pré-chauffe")
    store.add_event(jid, "step", "[1/3] pré-chauffe", NOW)
    store.add_event(jid, "warn", "c_1: amorçage requis", NOW + timedelta(seconds=5))
    store.finish_job(jid, "ok", 0, NOW + timedelta(minutes=40), summary="pré-chauffe terminée")
    job = store.job(jid)
    assert job["status"] == "ok" and job["exit_code"] == 0 and job["worker"] == "worker-1"
    assert job["started_at"] == "2026-11-08T09:00:00+00:00"
    assert job["finished_at"] == "2026-11-08T09:40:00+00:00"
    assert job["step_name"] == "pré-chauffe" and job["summary"] == "pré-chauffe terminée"
    events = store.events_of(jid)
    assert [e["level"] for e in events] == ["step", "warn"]
    assert events[1]["at"] == "2026-11-08T09:00:05+00:00"

    other = store.create_job("notify", {}, "cli", NOW + timedelta(hours=1))
    assert [j["id"] for j in store.list_jobs()] == [other, jid]  # la plus récente d'abord
    assert [j["id"] for j in store.list_jobs(kind="warm")] == [jid]
    assert set(store.last_jobs_by_kind()) == {"warm", "notify"}
    assert store.table_counts()["jobs"] == 2 and store.table_counts()["job_events"] == 2
    assert store.job("nope") is None

    # purge : les tâches anciennes et leurs événements, pas les récentes
    old = store.create_job("seed", {}, "cli", NOW - timedelta(days=500))
    store.add_event(old, "info", "vieux", NOW - timedelta(days=500))
    assert store.purge_jobs(NOW - timedelta(days=400)) == 1
    assert store.job(old) is None and store.events_of(old) == []
    assert store.job(jid) is not None
    store.close()


def test_sink_writes_steps_progress_and_summary_through_the_console():
    store = Store("sqlite://")
    jid = store.create_job("warm", {}, "cron", NOW, status="running")
    clock = [0.0]
    sink = jobs.JobSink(store, jid, now=lambda: NOW, clock=lambda: clock[0], min_interval_s=2.0)
    c = console.Console(stream=io.StringIO(), color=False)
    c.sink = sink
    steps = c.steps(2, "warm · test")
    with steps.step("pré-chauffe") as step:
        for done in range(1, 101):
            clock[0] += 0.01  # 1 s en tout : seuls 1 % du total et la fin déclenchent une écriture
            step.progress(done, 100, "cellules", f"{done} faites")
        job = store.job(jid)
        assert job["step_index"] == 1 and job["step_total"] == 2
        assert job["step_name"] == "pré-chauffe"
        assert (job["progress_done"], job["progress_total"], job["progress_unit"]) == (
            100,
            100,
            "cellules",
        )
        assert job["message"] == "100 faites"
        c.warn("c_1: amorçage requis")
        step.note("2 cellules")
    with steps.step("indice") as step:
        step.skip("déjà là")
    c.summary("pré-chauffe terminée")
    job = store.job(jid)
    assert job["summary"] == "pré-chauffe terminée" and job["message"] == "déjà là"
    assert job["progress_done"] is None  # remis à zéro à l'étape suivante
    texts = [(e["level"], e["text"]) for e in store.events_of(jid)]
    assert texts[0] == ("step", "[1/2] pré-chauffe")
    assert ("warn", "c_1: amorçage requis") in texts
    assert texts[2][0] == "step" and texts[2][1].startswith("[1/2] pré-chauffe : ok en ")
    assert texts[2][1].endswith(" · 2 cellules")
    assert texts[-1][1].startswith("[2/2] indice : deja fait")
    store.close()


def test_a_failing_sink_is_unplugged_and_the_command_goes_on(tmp_path: Path):
    class Broken:
        def step_begin(self, *a):
            raise RuntimeError("base partie")

    out = io.StringIO()
    c = console.Console(stream=out, color=False, log_path=tmp_path / "x.log")
    c.sink = Broken()
    with c.steps(1).step("quelque chose") as step:
        step.note("fait")
    c.close()
    assert "ok" in out.getvalue()
    assert (
        "puits débranché (step_begin) : RuntimeError: base partie"
        in (tmp_path / "x.log").read_text()
    )


def test_failed_step_is_an_error_event():
    store = Store("sqlite://")
    jid = store.create_job("notify", {}, "cron", NOW, status="running")
    c = console.Console(stream=io.StringIO(), color=False)
    c.sink = jobs.JobSink(store, jid, now=lambda: NOW)
    try:
        with c.steps(1).step("notification"):
            raise ValueError("APNs 403")
    except ValueError:
        pass
    events = store.events_of(jid)
    assert events[-1]["level"] == "error" and "ECHEC" in events[-1]["text"]
    assert "ValueError: APNs 403" in events[-1]["text"]
    store.close()


def test_register_then_close_records_the_run_and_uploads_the_log(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMPIS_JOB_ORIGIN", "cron")
    monkeypatch.delenv("CHAMPIS_JOB_ID", raising=False)
    url = f"sqlite:///{tmp_path / 'champis.db'}"
    log = tmp_path / "api-warm.log"
    c = console.Console(stream=io.StringIO(), color=False, log_path=log)
    reg = jobs.register("warm", {"pull": "True"}, url, c)
    assert reg is not None and c.sink is reg.sink
    with c.steps(1).step("pré-chauffe") as step:
        step.note("2 cellules")
    c.summary("pré-chauffe terminée")
    c.close()
    bucket = MemoryBucket()
    reg.close(0, log, bucket)
    store = Store(url)
    (job,) = store.list_jobs()
    assert job["origin"] == "cron" and job["status"] == "ok" and job["exit_code"] == 0
    assert job["summary"] == "pré-chauffe terminée" and job["worker"]
    assert job["log_key"] == f"jobs/{job['id']}.log"
    assert bucket.objects[job["log_key"]] == log.read_bytes()
    store.close()

    # un identifiant créé d'avance (service d'exécution, lot BO3) est repris, pas dupliqué
    store = Store(url)
    pre = store.create_job("notify", {}, "admin", NOW)
    store.close()
    monkeypatch.setenv("CHAMPIS_JOB_ID", pre)
    reg = jobs.register("notify", {}, url, console.Console(stream=io.StringIO(), color=False))
    assert reg is not None and reg.job_id == pre
    reg.close(1, None, None)
    store = Store(url)
    assert store.job(pre)["status"] == "failed" and store.job(pre)["origin"] == "admin"
    assert len(store.list_jobs()) == 2
    store.close()


def test_register_skips_read_only_commands_and_survives_a_missing_database(tmp_path: Path):
    c = console.Console(stream=io.StringIO(), color=False)
    assert jobs.register("doctor", {}, "sqlite://", c) is None
    assert jobs.register("warm", {}, "", c) is None
    # base injoignable : la commande tourne sans journal
    assert jobs.register("warm", {}, "postgresql+psycopg://u:p@127.0.0.1:1/x", c) is None
    assert c.sink is None


def test_age_hours():
    assert jobs.age_hours(None) is None
    assert jobs.age_hours("2026-11-08T07:00:00+00:00", NOW) == 2.0
    assert jobs.age_hours("2026-11-08T07:00:00", NOW) == 2.0  # naïf = UTC (SQLite)
