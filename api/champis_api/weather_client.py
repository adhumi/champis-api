"""Client Open-Meteo : le seul module qui parle à Open-Meteo (skill open-meteo).

Décision du 17 oct. 2026, « amorcer, puis accumuler » : en service, l'API n'appelle **que la
prévision** (plan payant, clé API, hôte `customer-api`). Chaque réponse apporte ~60 jours de
passé, rangés dans l'historique SQLite (`cache.history`) ; au-delà, l'historique a été amorcé une
fois par `champis-api seed` depuis l'archive ERA5-Land gratuite (hors Railway), qui fournit aussi
la climatologie du sol (10 ans → 11 quantiles par mois). L'archive n'est donc jamais appelée en
service : l'API historique n'est pas dans le plan Standard, et l'IP de sortie Railway est bridée
sur l'API gratuite.

- `forecast` : `past_days=60&forecast_days=8` (J inclus → J+7), séries journalières + humidité du
  sol (moyenne journalière pondérée des couches horaires 0–1, 1–3 et 3–9 cm, poids 1/2/6 ≈ 0–9 cm,
  l'équivalent prévision du 0–7 cm ERA5-Land). Seau de 12 h.
- `archive` et `soil_clim` : amorçage uniquement (`fetch_seed`), toujours sur l'API gratuite.

Quotas : Open-Meteo pondère chaque requête par le nombre de points et la longueur de la période.
D'où : lots bornés, un intervalle minimal entre requêtes, un 429 traité comme une indisponibilité
(cache périmé ou 503 avec `Retry-After`), un seul réessai sur 429 court.

Si Open-Meteo est injoignable, on sert la dernière réponse connue avec `stale=True` ; sans
aucune réponse connue, `WeatherUnavailableError`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import numpy as np

from .cache import HISTORY_VARS, WeatherCache, bucket_now
from .grid import Cell

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Plan payant Standard (clé API, décision du 17 oct. 2026) : prévision seule, quota propre à la clé
CUSTOMER_FORECAST_URL = "https://customer-api.open-meteo.com/v1/forecast"
TIMEZONE = "Europe/Paris"
PAST_DAYS = 60  # le modèle de prévision ne remonte pas plus loin ; l'archive prend le relais
FORECAST_DAYS = 8  # J inclus : J+1 … J+7
ARCHIVE_START_OFFSET = 200  # J−200 : couvre mémoire (90) + pluie (28) + persistance (12) + J−3
CLIMATOLOGY_YEARS = 10
QUANTILES = [i / 10 for i in range(11)]
BATCH_POINTS = 100
MIN_REQUEST_INTERVAL_S = 0.25  # lissage : jamais de rafale vers Open-Meteo
RETRY_MAX_WAIT_S = 10  # un seul réessai, et seulement si Open-Meteo demande une courte attente
SOIL_DAILY = "soil_moisture_0_to_7cm_mean"
SOIL_LAYER_WEIGHTS = {
    "soil_moisture_0_to_1cm": 1,
    "soil_moisture_1_to_3cm": 2,
    "soil_moisture_3_to_9cm": 6,
}

DAILY_VARS = [
    "temperature_2m_mean",
    "temperature_2m_min",
    "temperature_2m_max",
    "rain_sum",
    "snowfall_sum",
    "et0_fao_evapotranspiration",
]


class WeatherUnavailableError(Exception):
    """Open-Meteo injoignable et aucune réponse en cache pour cette cellule."""

    def __init__(self, message: str, retry_after_s: int | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class FetchResult:
    raws: dict[str, CellRaw]  # cellules complètes
    unavailable: dict[str, str]  # cellule → raison (Open-Meteo injoignable, rien en cache)
    retry_after_s: int | None  # délai conseillé par Open-Meteo sur un 429, s'il y en a eu un


@dataclass
class CellRaw:
    forecast: dict[str, Any]  # dates, t_mean, t_min, t_max, rain, snow, et0, soil (None possible)
    history: dict[str, dict[str, float]]  # jour → variables, jours passés accumulés + amorcés
    soil_clim: dict[str, Any]  # {"months": {"1": [11 quantiles], …}}
    stale: bool


class OpenMeteoClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        cache: WeatherCache,
        now: Callable[[], datetime] | None = None,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        api_key: str | None = None,
    ):
        self.http = http
        self.cache = cache
        self.now = now or (lambda: datetime.now().astimezone())
        self.min_interval_s = min_interval_s
        self.api_key = api_key or None
        self.forecast_url = CUSTOMER_FORECAST_URL if self.api_key else FORECAST_URL
        self.archive_url = ARCHIVE_URL  # amorçage seulement, API gratuite (hors plan Standard)
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    def today(self) -> date:
        return self.now().date()

    # ------------------------------------------------------------------ public

    async def fetch_cells(self, cells: Iterable[Cell]) -> dict[str, CellRaw]:
        """Toutes les cellules ou rien : lève `WeatherUnavailableError` à la première indisponible."""
        res = await self.fetch_cells_partial(cells)
        if res.unavailable:
            _cid, reason = next(iter(res.unavailable.items()))
            raise WeatherUnavailableError(reason, res.retry_after_s)
        return res.raws

    async def fetch_cells_partial(self, cells: Iterable[Cell]) -> FetchResult:
        """Ce qu'on peut : les cellules complètes d'un côté, les indisponibles de l'autre.

        Une cellule est complète quand elle a une prévision (fraîche ou périmée) et une
        climatologie amorcée. Les jours passés de chaque prévision fraîche sont rangés dans
        l'historique au passage.
        """
        cells = list(cells)
        now = self.now()
        today = now.date()
        bucket = bucket_now(now)
        forecasts: dict[str, dict[str, Any]] = {}
        stale: set[str] = set()
        unavailable: dict[str, str] = {}
        retry_after: int | None = None
        missing: list[Cell] = []
        for c in cells:
            cached = self.cache.get(c.id, "forecast", bucket)
            if cached is None:
                missing.append(c)
            else:
                forecasts[c.id] = cached
        for i in range(0, len(missing), BATCH_POINTS):
            batch = missing[i : i + BATCH_POINTS]
            try:
                payloads = await self._fetch_with_one_retry("forecast", batch, today)
            except (httpx.HTTPError, ValueError) as e:
                ra = _retry_after(e)
                retry_after = max(retry_after or 0, ra) if ra is not None else retry_after
                log.warning("Open-Meteo forecast injoignable (%s) : cache périmé", _redact(e))
                for c in batch:
                    last = self.cache.latest(c.id, "forecast")
                    if last is None:
                        unavailable[c.id] = f"prévision indisponible pour {c.id}"
                    else:
                        forecasts[c.id] = last[0]
                        stale.add(c.id)
                continue
            for c, p in zip(batch, payloads, strict=True):
                self.cache.put(c.id, "forecast", bucket, p, now)
                self.cache.put_history(c.id, _past_rows(p, today), "forecast")
                forecasts[c.id] = p
        raws: dict[str, CellRaw] = {}
        for c in cells:
            if c.id not in forecasts:
                continue
            clim = self.cache.soil_clim(c.id)
            if clim is None:
                unavailable[c.id] = (
                    f"climatologie du sol manquante pour {c.id} : amorçage requis "
                    "(champis-api seed)"
                )
                continue
            history = self.cache.history(
                c.id, today - timedelta(days=ARCHIVE_START_OFFSET), today - timedelta(days=1)
            )
            raws[c.id] = CellRaw(forecasts[c.id], history, clim, c.id in stale)
        return FetchResult(raws, unavailable, retry_after)

    async def fetch_seed(self, cell: Cell, today: date) -> tuple[dict[str, dict], dict[str, Any]]:
        """Amorçage d'une cellule depuis l'archive gratuite : (jours J−200 → J−1, climatologie)."""
        archive = (await self._fetch("archive", [cell], today))[0]
        clim = (await self._fetch("soil_clim", [cell], today))[0]
        rows = {d: {v: archive[v][i] for v in HISTORY_VARS} for i, d in enumerate(archive["dates"])}
        return rows, clim

    async def _fetch_with_one_retry(
        self, kind: str, batch: list[Cell], today: date
    ) -> list[dict[str, Any]]:
        """Sur un 429 à courte attente (limite par minute), attendre et réessayer une fois."""
        try:
            return await self._fetch(kind, batch, today)
        except httpx.HTTPStatusError as e:
            ra = _retry_after(e)
            if ra is None or ra > RETRY_MAX_WAIT_S:
                raise
            log.info("Open-Meteo 429, réessai dans %d s", ra)
            await asyncio.sleep(ra)
            return await self._fetch(kind, batch, today)

    # ------------------------------------------------------------------ requêtes

    async def _fetch(self, kind: str, batch: list[Cell], today: date) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "latitude": ",".join(f"{c.lat:.4f}" for c in batch),
            "longitude": ",".join(f"{c.lon:.4f}" for c in batch),
            "timezone": TIMEZONE,
        }
        if self.api_key and kind == "forecast":
            params["apikey"] = self.api_key
        if kind == "forecast":
            params |= {
                "daily": ",".join(DAILY_VARS),
                "hourly": ",".join(SOIL_LAYER_WEIGHTS),
                "past_days": PAST_DAYS,
                "forecast_days": FORECAST_DAYS,
            }
            url, norm = self.forecast_url, _normalize_forecast
        elif kind == "archive":
            params |= {
                "daily": ",".join([*DAILY_VARS, SOIL_DAILY]),
                "start_date": (today - timedelta(days=ARCHIVE_START_OFFSET)).isoformat(),
                "end_date": (today - timedelta(days=1)).isoformat(),
            }
            url, norm = self.archive_url, _normalize_archive
        else:
            params |= {
                "daily": SOIL_DAILY,
                "start_date": f"{today.year - CLIMATOLOGY_YEARS}-01-01",
                "end_date": f"{today.year - 1}-12-31",
            }
            url, norm = self.archive_url, _normalize_soil_climatology
        async with self._lock:  # une requête à la fois, espacées : pas de rafale
            wait = self._last_request + self.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            r = await self.http.get(url, params=params, timeout=60.0)
        r.raise_for_status()
        body = r.json()
        items = body if isinstance(body, list) else [body]
        if len(items) != len(batch):
            raise ValueError(f"{len(items)} réponses pour {len(batch)} points")
        return [norm(it) for it in items]


# ---------------------------------------------------------------------- normalisation


def _normalize_forecast(body: dict[str, Any]) -> dict[str, Any]:
    """Prévision : journalier + sol = moyenne journalière pondérée des couches horaires."""
    h = body["hourly"]
    w_total = sum(SOIL_LAYER_WEIGHTS.values())
    soil_by_day: dict[str, list[float]] = {}
    for i, ts in enumerate(h["time"]):
        vals = [h[k][i] for k in SOIL_LAYER_WEIGHTS]
        if any(v is None for v in vals):
            continue
        v = sum(SOIL_LAYER_WEIGHTS[k] * h[k][i] for k in SOIL_LAYER_WEIGHTS) / w_total
        soil_by_day.setdefault(ts[:10], []).append(v)
    d = body["daily"]
    soil = [
        (sum(soil_by_day[day]) / len(soil_by_day[day])) if soil_by_day.get(day) else None
        for day in d["time"]
    ]
    return _daily(body, soil)


def _normalize_archive(body: dict[str, Any]) -> dict[str, Any]:
    """Archive ERA5-Land : tout en journalier, sol 0–7 cm moyen fourni tel quel."""
    return _daily(body, body["daily"][SOIL_DAILY])


def _daily(body: dict[str, Any], soil: list[float | None]) -> dict[str, Any]:
    """Les `null` d'Open-Meteo sont conservés : `weather_service` décide quelle source comble quoi."""
    d = body["daily"]
    return {
        "elevation": float(body["elevation"]),
        "dates": d["time"],
        "t_mean": d["temperature_2m_mean"],
        "t_min": d["temperature_2m_min"],
        "t_max": d["temperature_2m_max"],
        "rain": d["rain_sum"],
        "snow": d["snowfall_sum"],
        "et0": d["et0_fao_evapotranspiration"],
        "soil": soil,
    }


def _normalize_soil_climatology(body: dict[str, Any]) -> dict[str, Any]:
    """11 quantiles (0, 10, … 100 %) de l'humidité 0–7 cm journalière par mois civil, sur 10 ans."""
    d = body["daily"]
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for ts, v in zip(d["time"], d[SOIL_DAILY], strict=True):
        if v is not None:
            by_month[int(ts[5:7])].append(v)
    months = {}
    for m, vals in by_month.items():
        if len(vals) < 28 * 5:  # moins de 5 mois sur 10 ans : climatologie inexploitable
            raise ValueError(f"climatologie du sol trop courte pour le mois {m}")
        months[str(m)] = [float(q) for q in np.quantile(vals, QUANTILES)]
    return {"months": months}


def _past_rows(forecast: dict[str, Any], today: date) -> dict[str, dict[str, float | None]]:
    """Les jours strictement passés d'une prévision, prêts pour `cache.put_history`."""
    return {
        d: {v: forecast[v][i] for v in HISTORY_VARS}
        for i, d in enumerate(forecast["dates"])
        if date.fromisoformat(d) < today
    }


def _redact(e: Exception) -> str:
    """Message d'erreur sans la clé API (httpx met l'URL complète dans le message)."""
    return re.sub(r"apikey=[^&'\s]+", "apikey=***", str(e))


def _retry_after(e: Exception) -> int | None:
    """Délai conseillé par Open-Meteo sur un 429, sinon None."""
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
        try:
            return int(e.response.headers.get("Retry-After", "60"))
        except ValueError:
            return 60
    return None


def soil_percentile(value: float | None, clim: dict[str, Any], day: date) -> float | None:
    """Percentile 0–1 de `value` dans la climatologie du mois de `day` (interpolation linéaire)."""
    if value is None:
        return None
    q = clim["months"][str(day.month)]
    if value <= q[0]:
        return 0.0
    if value >= q[-1]:
        return 1.0
    for i in range(1, len(q)):
        if value <= q[i]:
            lo, hi = q[i - 1], q[i]
            frac = 0.0 if hi == lo else (value - lo) / (hi - lo)
            return QUANTILES[i - 1] + frac * (QUANTILES[i] - QUANTILES[i - 1])
    return 1.0
