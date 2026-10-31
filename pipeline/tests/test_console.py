"""Console des CLI : étapes, statuts, journal fichier, couleurs, erreurs (mode non-TTY)."""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path

import pytest
from champis_pipeline import console


def test_steps_statuses_and_log_file(tmp_path: Path):
    buf = io.StringIO()
    log = tmp_path / "logs" / "all-x.log"
    c = console.Console(stream=buf, color=False, log_path=log)
    steps = c.steps(3, "all --dept 25")
    with steps.step("download") as st:
        st.note("BD Forêt présent")
    with steps.step("grid") as st:
        st.skip("déjà là")
    with pytest.raises(FileNotFoundError), steps.step("terrain"):
        raise FileNotFoundError("rasters grid manquants pour 25 : ['host']\nsuite ignorée")
    c.summary("terminé")
    c.close()

    out = buf.getvalue()
    lines = out.splitlines()
    assert lines[0] == "all --dept 25"
    assert lines[1] == "[1/3] download"
    assert lines[2].split() == ["ok", "0.0", "s", "BD", "Forêt", "présent"]
    assert lines[3] == "[2/3] grid"
    assert lines[4].split()[:2] == ["deja", "fait"] and lines[4].endswith("déjà là")
    assert lines[5] == "[3/3] terrain"
    assert "ECHEC" in lines[6] and lines[6].endswith(
        "FileNotFoundError: rasters grid manquants pour 25 : ['host']"
    )
    assert lines[7].startswith("terminé en ") and str(log) in lines[7]
    assert "\033[" not in out
    assert log.read_text(encoding="utf-8") == out  # même contenu, sans couleur


def test_written_tells_new_file_from_existing(tmp_path: Path):
    buf = io.StringIO()
    c = console.Console(stream=buf, color=False)
    old = tmp_path / "old.tif"
    old.write_text("x")
    past = time.time() - 3600
    import os

    os.utime(old, (past, past))
    steps = c.steps(3)
    with steps.step("a") as st:
        st.written(old)
    with steps.step("b") as st:
        new = tmp_path / "new.tif"
        new.write_text("y")
        st.written(new)
    with steps.step("c") as st:
        st.written(None)  # fonction factice en test : pas de résultat, pas d'erreur
    lines = buf.getvalue().splitlines()
    assert lines[1].split()[:2] == ["deja", "fait"] and lines[1].endswith(str(old))
    assert lines[3].split()[0] == "ok" and lines[3].endswith(str(new))
    assert lines[5].split() == ["ok", "0.0", "s"]


def test_colors_only_when_asked_and_logging_restyled():
    buf = io.StringIO()
    c = console.Console(stream=buf, color=True, verbose=False)
    assert c.style("ok", console.GREEN) == "\033[32mok\033[0m"
    handler = c.logging_handler()
    logger = logging.getLogger("champis_pipeline.test_console")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.info("présent : x.7z")
    logger.warning("md5 différent")
    logger.error("boum")
    logger.debug("invisible sans -v")
    lines = buf.getvalue().splitlines()
    assert lines == [
        "\033[2m      - présent : x.7z\033[0m",
        "\033[33m      ! md5 différent\033[0m",
        "\033[31m      x boum\033[0m",
    ]

    plain = io.StringIO()
    v = console.Console(stream=plain, color=False, verbose=True)
    logger.handlers[:] = [v.logging_handler()]
    logger.info("brut")
    logger.debug("debug visible")
    assert plain.getvalue().splitlines() == [
        "INFO champis_pipeline.test_console: brut",
        "DEBUG champis_pipeline.test_console: debug visible",
    ]


def test_quiet_hides_info_but_keeps_it_in_log(tmp_path: Path):
    buf = io.StringIO()
    log = tmp_path / "q.log"
    c = console.Console(stream=buf, color=False, log_path=log, quiet=True)
    c.info("détail")
    c.warn("attention")
    c.close()
    assert buf.getvalue().splitlines() == ["      ! attention"]
    assert log.read_text(encoding="utf-8").splitlines() == ["      - détail", "      ! attention"]


def test_wants_color_respects_no_color_and_tty(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CHAMPIS_QUIET", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert console.wants_color(Tty())
    assert not console.wants_color(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not console.wants_color(Tty())
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not console.wants_color(Tty())


def test_run_turns_exceptions_into_failure_lines():
    buf = io.StringIO()
    c = console.Console(stream=buf, color=False)

    def boom():
        raise ValueError("mauvaise valeur")

    assert console.run(boom, console=c) == 1
    assert buf.getvalue().splitlines() == [
        "ECHEC ValueError: mauvaise valeur",
        "      relance avec -v pour la trace complète",
    ]

    buf = io.StringIO()
    c = console.Console(stream=buf, color=False, verbose=True)
    assert console.run(boom, console=c) == 1
    assert "Traceback" in buf.getvalue() and "mauvaise valeur" in buf.getvalue()

    buf = io.StringIO()
    c = console.Console(stream=buf, color=False)

    def missing():
        raise SystemExit("bucket non configuré")

    assert console.run(missing, console=c) == 1
    assert buf.getvalue().splitlines() == ["ECHEC bucket non configuré"]

    buf = io.StringIO()
    c = console.Console(stream=buf, color=False)

    def in_step():
        with c.steps(2).step("terrain"):
            raise FileNotFoundError("rasters manquants")

    assert console.run(in_step, console=c) == 1
    assert buf.getvalue().splitlines()[-2:] == [
        "ECHEC arrêt à l'étape [1/2] terrain",
        "      relance avec -v pour la trace complète",
    ]
    assert console.run(lambda: 0, console=c) == 0
    assert console.run(lambda: None, console=c) == 0


def test_setup_writes_dated_log_and_rotates(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for i in range(console.KEEP_LOGS + 5):
        p = log_dir / f"old-{i:03d}.log"
        p.write_text("x")
        t = 1_000_000 + i
        import os

        os.utime(p, (t, t))
    monkeypatch.setenv("CHAMPIS_LOG_DIR", str(log_dir))
    c = console.setup("tiles", verbose=False, quiet=False)
    try:
        logging.getLogger("champis_pipeline.x").info("une ligne")
        assert c.log_path is not None and c.log_path.parent == log_dir
        assert c.log_path.name.startswith("tiles-") and c.log_path.suffix == ".log"
        logs = sorted(log_dir.glob("*.log"))
        assert len(logs) == console.KEEP_LOGS
        assert not (log_dir / "old-000.log").exists() and (log_dir / "old-010.log").exists()
        text = c.log_path.read_text(encoding="utf-8")
        assert text.startswith("# tiles · ") and "      - une ligne" in text
    finally:
        c.close()

    monkeypatch.setenv("CHAMPIS_LOG_DIR", "")
    c = console.setup("grid")
    assert c.log_path is None
    c.close()


def test_duration_and_size():
    assert console.duration(3.24) == "3.2 s"
    assert console.duration(42) == "42 s"
    assert console.duration(252) == "4 min 12 s"
    assert console.duration(7300) == "2 h 01 min"
    assert console.size(123) == "123 o"
    assert console.size(4_200_000) == "4,2 Mo"
    assert console.size(1.1e9) == "1,1 Go"
    assert console.size(250_000_000) == "250 Mo"
