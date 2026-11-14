"""Masquage des secrets dans les journaux (`champis_pipeline.redact`, correctif du 13 nov. 2026) :
clé Open-Meteo, signature des adresses présignées, jeton d'appareil et JWT d'APNs ne sont écrits
ni par la console des CLI (`-v` compris), ni dans son fichier journal, ni dans le puits."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import httpx
import pytest
from champis_pipeline import console
from champis_pipeline.redact import QUIET_LOGGERS, RedactFilter, install, redact

KEY = "CLE-OPEN-METEO-1234"
SIG = "0123456789abcdef" * 4
TOKEN = "a1b2c3d4e5f6" * 5
URL = f"https://customer-api.open-meteo.com/v1/forecast?latitude=47.1&apikey={KEY}&past_days=1"
PRESIGNED = (
    "https://bucket.example/tiles/T-x.pmtiles?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential=AKIAEXAMPLE%2F20261113%2Fauto%2Fs3%2Faws4_request&X-Amz-Signature={SIG}"
)


@pytest.fixture
def restore_logging():
    """Niveaux et handlers de la racine tels qu'avant le test (`console.setup` les remplace)."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {name: logging.getLogger(name).level for name in (*QUIET_LOGGERS, "rasterio", "fiona")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in levels.items():
        logging.getLogger(name).setLevel(lvl)


def test_redact_masks_known_secrets_and_nothing_else():
    assert redact(URL) == URL.replace(KEY, "***")
    out = redact(PRESIGNED)
    assert SIG not in out and "AKIAEXAMPLE" not in out
    assert "X-Amz-Signature=***" in out and "X-Amz-Algorithm=AWS4-HMAC-SHA256" in out
    assert redact(f"u=apikey%3D{KEY}") == "u=apikey%3D***"  # adresse encodée dans une autre
    assert redact(f"Signature:\n{SIG}") == "Signature:\n***"  # botocore, niveau DEBUG
    assert redact(f"POST https://api.push.apple.com/3/device/{TOKEN} 200").endswith(
        "/3/device/*** 200"
    )
    assert redact("(b'authorization', b'bearer eyJhbGciOiJFUzI1NiJ9.e30.sig')") == (
        "(b'authorization', b'bearer ***')"
    )
    assert redact("postgresql://champis:motdepasse@db.internal:5432/x") == (
        "postgresql://champis:***@db.internal:5432/x"
    )
    plain = "[2/5] terrain boletus_edulis · data/T/boletus_edulis/25-af34c16e.tif à 14:02:11"
    assert redact(plain) == plain


def test_filter_masks_message_arguments_and_exception_text():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger = logging.Logger("essai")
    logger.addHandler(handler)
    install(logger)
    install(logger)  # une seule fois par handler
    assert sum(isinstance(f, RedactFilter) for f in handler.filters) == 1
    logger.warning("HTTP Request: GET %s %d", URL, 200)
    try:
        raise RuntimeError(f"échec pour {PRESIGNED}")
    except RuntimeError:
        logger.exception("relais des tuiles")
    logger.info("rien à masquer : %d cellules", 3)
    out = buf.getvalue()
    assert KEY not in out and SIG not in out
    assert "apikey=***" in out and "X-Amz-Signature=***" in out and "RuntimeError" in out
    assert "rien à masquer : 3 cellules" in out


def _open_meteo_call() -> None:
    """Un appel httpx comme celui du client Open-Meteo, sans réseau (httpx journalise l'adresse
    complète au niveau INFO, httpcore le détail au niveau DEBUG)."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as http:
        http.get(URL.split("?")[0], params={"latitude": "47.1", "apikey": KEY})


def test_cli_logs_never_show_the_key_even_verbose(tmp_path: Path, monkeypatch, restore_logging):
    """`-v` rétablit les journaux bruts des bibliothèques : la ligne de httpx sort, clé masquée,
    à l'écran, dans le fichier journal et dans le puits ; sans `-v`, elle ne sort pas du tout."""
    monkeypatch.setenv("CHAMPIS_LOG_DIR", str(tmp_path))

    class Sink:
        def __init__(self):
            self.events: list[str] = []

        def event(self, level: str, text: str) -> None:
            self.events.append(text)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", buf)
    c = console.setup("warm", verbose=True)
    c.stream, c.sink = buf, Sink()
    _open_meteo_call()
    c.warn(f"Open-Meteo forecast injoignable (403 pour {URL})")
    assert console.run(lambda: (_ for _ in ()).throw(RuntimeError(f"échec {PRESIGNED}")), console=c)
    screen, journal = buf.getvalue(), next(tmp_path.glob("warm-*.log")).read_text(encoding="utf-8")
    for text in (screen, journal, "\n".join(c.sink.events)):
        assert KEY not in text and SIG not in text
    assert "INFO httpx: HTTP Request: GET" in journal and "apikey=***" in journal
    assert "X-Amz-Signature=***" in journal and "apikey=***" in "\n".join(c.sink.events)

    buf2 = io.StringIO()
    c = console.setup("warm", verbose=False)
    c.stream = buf2
    _open_meteo_call()
    c.close()
    assert "HTTP Request" not in buf2.getvalue() and KEY not in buf2.getvalue()
    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_access_filter_drops_the_query_string_whatever_the_record_shape():
    """Journal d'accès sans query string (14 nov. 2026) : l'enregistrement à cinq arguments
    d'uvicorn garde ses arguments, chemin réécrit ; une autre forme perd ce qui suit le `?`."""
    from champis_pipeline.redact import AccessQueryFilter, install_access_filter, strip_query

    assert strip_query("/weather?lat=47.1&lon=6.3") == "/weather" and strip_query("/x") == "/x"

    def record(msg, *args):
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)

    f = AccessQueryFilter()
    r = record(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.1:1", "GET", "/weather?lat=47.1&lon=6.3", "1.1", 200
    )
    assert f.filter(r) and r.args == ("10.0.0.1:1", "GET", "/weather", "1.1", 200)
    assert r.getMessage() == '10.0.0.1:1 - "GET /weather HTTP/1.1" 200'
    r = record("GET %s terminé en %d ms", "/weather/bbox?min_lon=6.2&min_lat=47.0", 12)
    assert f.filter(r) and r.getMessage() == "GET /weather/bbox terminé en 12 ms"
    r = record("rien à retirer : %s", "/health")
    assert f.filter(r) and r.args == ("/health",)
    assert f.filter(record("mal formé %d", "x"))  # logging s'en plaindra lui-même
    logger = logging.getLogger("champis.test.access")
    install_access_filter(logger)
    install_access_filter(logger)
    assert len(logger.filters) == 1
