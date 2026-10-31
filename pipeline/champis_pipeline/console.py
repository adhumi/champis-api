"""Console des CLI (`champis-pipeline`, `champis-api`, `champis-vectors`) : étapes numérotées,
statut coloré, journaux reformatés, copie dans un fichier.

Conventions :

- le statut (étapes, journaux, barres) va sur **stderr**, les données (`summary`, JSON) sur stdout ;
- couleurs ANSI seulement sur un terminal, jamais si `NO_COLOR` ou `CHAMPIS_QUIET` est posé ;
- aucun emoji, des caractères ASCII : `ok`, `ECHEC`, `deja fait`, `[2/5]`, `#`, `-` ;
- tout ce qui s'affiche est aussi écrit, sans couleur, dans `data/logs/<commande>-<date>.log`
  (`CHAMPIS_LOG_DIR` pour changer de dossier, vide pour ne rien écrire) ; les 50 derniers journaux
  sont gardés.

Rien ici ne touche au modèle ni aux données : un module de confort, testé en mode non-TTY.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from tqdm import tqdm as _tqdm

KEEP_LOGS = 50

# séquences ANSI (SGR) : pas de dépendance, pas de détection fine du terminal
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

_STATUS_STYLE = {"ok": GREEN, "deja fait": DIM, "ECHEC": RED + BOLD, "ignore": YELLOW}


def wants_color(stream: IO[str]) -> bool:
    """Un terminal, sans `NO_COLOR` (https://no-color.org) ni `CHAMPIS_QUIET`, et pas `TERM=dumb`."""
    if os.environ.get("NO_COLOR") or os.environ.get("CHAMPIS_QUIET"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def duration(seconds: float) -> str:
    """`3 s`, `4 min 12 s`, `2 h 05 min` : lisible, sans décimales inutiles."""
    s = round(seconds)
    if s < 60:
        return f"{seconds:.1f} s" if seconds < 10 else f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def size(n_bytes: float) -> str:
    """`123 ko`, `4,2 Mo`, `1,1 Go` (décimal, comme les tailles affichées par l'app)."""
    for unit, div in (("Go", 1e9), ("Mo", 1e6), ("ko", 1e3)):
        if n_bytes >= div:
            value = n_bytes / div
            text = f"{value:.1f}".replace(".", ",") if value < 100 else f"{value:.0f}"
            return f"{text} {unit}"
    return f"{int(n_bytes)} o"


class Console:
    """Écrit sur `stream` (couleurs selon `color`) et, si `log_path` est donné, dans ce fichier."""

    def __init__(
        self,
        stream: IO[str] | None = None,
        color: bool | None = None,
        log_path: Path | None = None,
        verbose: bool = False,
        quiet: bool = False,
    ) -> None:
        self.stream = stream or sys.stderr
        self.color = wants_color(self.stream) if color is None else color
        self.verbose = verbose
        self.quiet = quiet
        self.log_path = log_path
        self._log_file: IO[str] | None = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = log_path.open("a", encoding="utf-8")
        self.started = time.monotonic()
        self.failures = 0
        # exception déjà affichée par une étape : `run` ne la répète pas
        self.reported: BaseException | None = None
        self.reported_step: str = ""

    # ------------------------------------------------------------------ sortie brute

    def style(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + RESET

    def write(self, text: str, plain: str | None = None, important: bool = True) -> None:
        """Une ligne sur le terminal (`text`, colorée) et dans le journal (`plain`, sans couleur).

        `important=False` : masquée par `--quiet` à l'écran, gardée dans le journal.
        """
        if self._log_file is not None:
            self._log_file.write((plain if plain is not None else text) + "\n")
            self._log_file.flush()
        if self.quiet and not important:
            return
        # tqdm.write dégage la barre en cours, puis la redessine : rien ne s'entremêle
        _tqdm.write(text, file=self.stream)

    def log_only(self, plain: str) -> None:
        """Dans le journal seulement (entête, trace)."""
        if self._log_file is not None:
            self._log_file.write(plain + "\n")
            self._log_file.flush()

    def close(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------ étapes

    def title(self, text: str) -> None:
        self.write(self.style(text, BOLD), text)

    def info(self, text: str) -> None:
        """Ligne d'information secondaire (grise), indentée sous l'étape en cours."""
        self.write(self.style("      - " + text, DIM), "      - " + text, important=False)

    def warn(self, text: str) -> None:
        self.write(self.style("      ! " + text, YELLOW), "      ! " + text)

    def error(self, text: str, hint: str | None = None) -> None:
        self.failures += 1
        self.write(self.style("ECHEC " + text, RED, BOLD), "ECHEC " + text)
        if hint:
            self.write(self.style("      " + hint, DIM), "      " + hint)

    def steps(self, total: int, title: str | None = None) -> Steps:
        if title:
            self.title(title)
        return Steps(self, total)

    def summary(self, text: str) -> None:
        """Dernière ligne : durée totale et chemin du journal."""
        tail = f" en {duration(time.monotonic() - self.started)}"
        if self.log_path is not None:
            tail += f" · journal : {self.log_path}"
        self.write(self.style(text + tail, BOLD), text + tail)

    # ------------------------------------------------------------------ journaux `logging`

    def logging_handler(self) -> logging.Handler:
        return _ConsoleHandler(self)


class Steps:
    """Compteur `[i/n]` : `with steps.step("download") as st: ...` imprime l'entête, puis la ligne
    de résultat (statut, durée, détail). Une exception marque l'étape ECHEC et remonte."""

    def __init__(self, console: Console, total: int) -> None:
        self.console = console
        self.total = total
        self.index = 0

    @contextmanager
    def step(self, name: str) -> Iterator[Step]:
        self.index += 1
        step = Step(self.console, f"[{self.index}/{self.total}]", name)
        step.begin()
        try:
            yield step
        except BaseException as e:
            step.finish("ECHEC", _one_line(e))
            self.console.reported = e
            self.console.reported_step = f"{step.prefix} {step.name}"
            raise
        step.finish(step.status, step.detail)


class Step:
    def __init__(self, console: Console, prefix: str, name: str) -> None:
        self.console = console
        self.prefix = prefix
        self.name = name
        self.status = "ok"
        self.detail = ""
        self.t0 = time.monotonic()
        # `written()` compare à `st_mtime` : il faut un repère en temps mur, pas monotone
        self.wall0 = time.time()

    def begin(self) -> None:
        plain = f"{self.prefix} {self.name}"
        self.console.write(
            self.console.style(self.prefix, CYAN) + " " + self.console.style(self.name, BOLD), plain
        )

    def note(self, text: str) -> None:
        """Détail affiché sur la ligne de résultat (`3812x4620 px`, chemin écrit…)."""
        self.detail = text

    def skip(self, text: str = "") -> None:
        """L'étape n'avait rien à faire (fichier présent)."""
        self.status = "deja fait"
        if text:
            self.detail = text

    def written(self, path: Path | None) -> None:
        """Résultat selon la date du fichier : écrit pendant l'étape, ou déjà là."""
        if path is None:
            return
        if path.exists() and path.stat().st_mtime >= self.wall0 - 1:
            self.note(str(path))
        else:
            self.skip(str(path))

    def finish(self, status: str, detail: str) -> None:
        took = duration(time.monotonic() - self.t0)
        plain = f"      {status:<9} {took:>10}  {detail}".rstrip()
        text = (
            "      "
            + self.console.style(f"{status:<9}", _STATUS_STYLE.get(status, ""))
            + " "
            + self.console.style(f"{took:>10}", DIM)
            + ("  " + detail if detail else "")
        )
        self.console.write(text, plain)


def _one_line(e: BaseException) -> str:
    text = str(e).strip().splitlines()
    first = text[0] if text else ""
    return f"{type(e).__name__}: {first}" if first else type(e).__name__


class _ConsoleHandler(logging.Handler):
    """Journaux `logging` dans le style de la console : INFO gris, WARNING jaune, ERROR rouge.

    En mode `verbose`, le module et le niveau sont rétablis (format brut, utile pour chercher).
    """

    def __init__(self, console: Console) -> None:
        super().__init__(logging.DEBUG if console.verbose else logging.INFO)
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and self.console.verbose:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            if self.console.verbose:
                plain = f"{record.levelname} {record.name}: {message}"
                self.console.write(
                    self.console.style(plain, DIM),
                    plain,
                    important=record.levelno >= logging.WARNING,
                )
            elif record.levelno >= logging.ERROR:
                self.console.write(
                    self.console.style("      x " + message, RED), "      x " + message
                )
            elif record.levelno >= logging.WARNING:
                self.console.warn(message)
            else:
                self.console.info(message)
        except Exception:  # pragma: no cover - comme logging.Handler.handleError
            self.handleError(record)


# ---------------------------------------------------------------------- installation par les CLI

_current: Console | None = None


def get() -> Console:
    """La console du processus ; une console nue (stderr, sans journal) si `setup` n'a pas été appelé."""
    global _current
    if _current is None:
        _current = Console()
    return _current


def setup(
    command: str, verbose: bool = False, quiet: bool = False, log_dir: Path | None = None
) -> Console:
    """Installe la console et le journal `logging` pour une commande ; renvoie la console.

    `log_dir` : défaut `CHAMPIS_LOG_DIR`, sinon `data/logs` ; `CHAMPIS_LOG_DIR=` (vide) = pas de fichier.
    `quiet` pose `CHAMPIS_QUIET` pour couper les barres tqdm (lu par `progress.tqdm`).
    """
    global _current
    if quiet:
        os.environ["CHAMPIS_QUIET"] = "1"
    log_path = None
    if log_dir is None:
        env = os.environ.get("CHAMPIS_LOG_DIR")
        if env is None:
            from .config import DATA_DIR

            log_dir = DATA_DIR / "logs"
        elif env:
            log_dir = Path(env)
    if log_dir is not None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / f"{command}-{stamp}.log"
        _rotate(log_dir, KEEP_LOGS - 1)
    if _current is not None:
        _current.close()
    _current = Console(log_path=log_path, verbose=verbose, quiet=quiet)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(_current.logging_handler())
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not verbose:  # bibliothèques bavardes : rien sous WARNING
        for name in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "rasterio", "fiona"):
            logging.getLogger(name).setLevel(logging.WARNING)
    if log_path is not None:
        _current.log_only(
            f"# {command} · {datetime.now().isoformat(timespec='seconds')} · {' '.join(sys.argv[1:])}"
        )
    return _current


def _rotate(log_dir: Path, keep: int) -> None:
    if not log_dir.exists():
        return
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    for old in logs[: max(0, len(logs) - keep)]:
        old.unlink(missing_ok=True)


def run(fn: Any, *args: Any, console: Console | None = None) -> int:
    """Exécute `fn(*args)` ; une exception devient une ligne ECHEC (trace complète avec `-v`),
    code de retour 1. `SystemExit` garde son code et son message."""
    c = console or get()
    try:
        result = fn(*args)
        return int(result or 0)
    except KeyboardInterrupt:
        c.error("interrompu")
        return 130
    except SystemExit as e:
        if isinstance(e.code, str):
            c.error(e.code)
            return 1
        return int(e.code or 0)
    except Exception as e:
        hint = None if c.verbose else "relance avec -v pour la trace complète"
        if e is c.reported:  # déjà sur la ligne de l'étape : on dit juste où ça s'est arrêté
            c.error(f"arrêt à l'étape {c.reported_step}", hint=hint)
        else:
            c.error(_one_line(e), hint=hint)
        if c.verbose:
            c.write(traceback.format_exc().rstrip(), important=True)
        return 1
    finally:
        c.close()
