"""« Mes coins » : score du jour de chaque favori et notification récapitulative.

Spec › App iOS › Mes coins et › Backend (30 oct. 2026) : les coins sont synchronisés vers le
backend avec le jeton APNs ; après la pré-chauffe du matin, le backend calcule le score de chaque
coin et envoie **une seule** notification par appareil, récapitulative, **seulement si au moins un
coin est bon ou excellent**. L'app ne fait aucun calcul en arrière-plan. La notification porte
`coin_id` (le coin le mieux noté) et `GET /coins` renvoie les onze jours de chaque coin (refonte UX,
lot 1, 1er nov. 2026).

Score d'un coin = max sur ses espèces de `T_e × M_eff_e × Φ_e` (règle 9), avec `T_e` lu par l'app
dans les tuiles au moment où le coin est créé, et `M_eff`, `Φ` de la cellule météo la plus proche
pour la classe d'altitude du coin. Les seuils (« bon » = 0,55…) viennent de la palette, jamais du code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from champis_pipeline.species import load_palette, load_species

from .apns import ApnsClient
from .grid import Cell, WeatherGrid
from .scoring import palette_class
from .store import Coin, Store
from .weather_client import OpenMeteoClient
from .weather_service import HistoryTooShortError, SeriesGapError, cell_weather, days_around

log = logging.getLogger(__name__)

DAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


@dataclass(frozen=True)
class CoinDay:
    """Un des onze jours J−3 → J+7 d'un coin : `None` quand la météo manque ce jour-là."""

    date: date
    value: float | None
    leading: str | None


@dataclass(frozen=True)
class CoinScore:
    coin: Coin
    today: float | None  # None : pas de météo pour la cellule
    best_day: date | None  # meilleur jour de J à J+7 (strictement meilleur que J), sinon None
    best: float | None
    leading: str | None  # espèce qui porte le score du jour
    # les onze jours (refonte UX, lot 1, 1er nov. 2026) : la fiche d'un coin et sa tendance
    days: tuple[CoinDay, ...] = ()
    # phrases du jour pour l'espèce qui porte le score, à la classe du coin (étape B2, 6 nov. 2026)
    summary: tuple[str, ...] = ()


def nearest_alt_class(cell: Cell, alt_m: float) -> str:
    """Classe d'altitude de la cellule la plus proche de celle du coin, en clé de `by_alt`."""
    best = min(cell.alt_classes, key=lambda a: abs(a - alt_m))
    return str(int(best))


def score_coins(
    coins: list[Coin], grid: WeatherGrid, weather: dict[str, dict], today: date
) -> list[CoinScore]:
    """`weather` : cellule → réponse de `cell_weather` ; avec `detail=True`, les phrases du jour
    (`summary`) accompagnent le score. Pur et testable."""
    out = []
    all_days = days_around(today)
    for coin in coins:
        cell = grid.nearest(coin.lat, coin.lon)
        w = weather.get(cell.id) if cell else None
        if w is None:
            out.append(CoinScore(coin, None, None, None, None, _empty_days(all_days)))
            continue
        alt = nearest_alt_class(cell, coin.alt_class)
        per_day: dict[date, tuple[float, str]] = {}
        for d in all_days:
            best_v, best_sid = None, None
            for sid, t in coin.t.items():
                rows = w["species"].get(sid)
                if not rows:
                    continue
                row = next((r for r in rows if r["date"] == d.isoformat()), None)
                if row is None or alt not in row["by_alt"]:
                    continue
                v = t * row["by_alt"][alt]["M_eff"] * row["by_alt"][alt]["phi"]
                if best_v is None or v > best_v or (v == best_v and sid < best_sid):
                    best_v, best_sid = v, sid
            if best_v is not None:
                per_day[d] = (best_v, best_sid)
        days = tuple(
            CoinDay(d, per_day[d][0], per_day[d][1]) if d in per_day else CoinDay(d, None, None)
            for d in all_days
        )
        if today not in per_day:
            out.append(CoinScore(coin, None, None, None, None, days))
            continue
        t_val, lead = per_day[today]
        today_row = next(r for r in w["species"][lead] if r["date"] == today.isoformat())
        phrases = tuple(today_row["by_alt"][alt].get("summary", ()))
        later = [(v, d) for d, (v, _) in per_day.items() if d > today]
        best = max(later, default=None)
        if best and best[0] > t_val:
            out.append(CoinScore(coin, t_val, best[1], best[0], lead, days, phrases))
        else:
            out.append(CoinScore(coin, t_val, None, None, lead, days, phrases))
    return out


def _empty_days(all_days: list[date]) -> tuple[CoinDay, ...]:
    return tuple(CoinDay(d, None, None) for d in all_days)


def good_threshold() -> float:
    """Seuil « bon » de la palette : en dessous, on ne dérange personne."""
    return next(t["min"] for t in load_palette()["thresholds"] if t["label"] == "bon")


def label_fr(value: float) -> str:
    cls = palette_class(value, load_palette())
    return next(t["fr"] for t in load_palette()["thresholds"] if t["label"] == cls["label"])


def day_words(d: date) -> str:
    """`2026-11-08` → `samedi 8` : le jour de la semaine et son numéro, jamais un J+n."""
    return f"{DAY_NAMES[d.weekday()]} {d.day}"


def _species_words(species_id: str | None) -> str:
    """Nom français de l'espèce, en minuscule initiale pour une phrase (« cèpe de Bordeaux »)."""
    if not species_id:
        return ""
    name = load_species(species_id)["names"]["fr"]
    return name[:1].lower() + name[1:]


def compose(scores: list[CoinScore], today: date) -> tuple[str, str, str] | None:
    """(titre, corps, id du meilleur coin) de la notification, ou None s'il n'y a rien à annoncer.

    En mots, sans score ni point décimal (spec › Produit › étape A, 6 nov. 2026) : le libellé de
    la palette, l'espèce qui porte le score, le meilleur jour à venir écrit « samedi 8 ». L'id du
    coin le mieux noté part dans la charge utile (`coin_id`) : l'app ouvre sa fiche.
    """
    threshold = good_threshold()
    ready = sorted(
        (s for s in scores if s.today is not None and s.today >= threshold),
        key=lambda s: -s.today,
    )
    if not ready:
        return None
    labels = [label_fr(s.today).lower() for s in ready]
    if len(ready) == 1:
        title = f"Champis — {ready[0].coin.name} : {labels[0]} aujourd'hui"
    else:
        kinds = sorted(set(labels), key=labels.index)
        words = " ou ".join(f"{k}s" for k in kinds)
        title = f"Champis — {len(ready)} coins {words} aujourd'hui"
    parts = []
    for s, label in zip(ready, labels, strict=True):
        sp = _species_words(s.leading)
        parts.append(f"{s.coin.name} : {label}" + (f" ({sp})" if sp else ""))
    body = ". ".join(parts) + "."
    others = [s for s in scores if s.today is not None and s.today < threshold]
    if len(others) > 1:
        body += f" Les {len(others)} autres restent sous « bon »."
    elif others:
        body += f" {others[0].coin.name} reste sous « bon »."
    peaks = [s for s in scores if s.best_day is not None and s.best >= threshold]
    if peaks:
        p = max(peaks, key=lambda s: s.best)
        body += f" Meilleur jour à venir : {day_words(p.best_day)} pour {p.coin.name}."
    return title, body, ready[0].coin.id


async def weather_for(
    coins: list[Coin], grid: WeatherGrid, client: OpenMeteoClient, today: date
) -> dict[str, dict]:
    """Météo des cellules les plus proches des coins (depuis le cache après la pré-chauffe)."""
    cells: dict[str, Cell] = {}
    for c in coins:
        cell = grid.nearest(c.lat, c.lon)
        if cell:
            cells[cell.id] = cell
    if not cells:
        return {}
    species = sorted({sid for c in coins for sid in c.t})
    res = await client.fetch_cells_partial(list(cells.values()))
    out = {}
    for cid, raw in res.raws.items():
        try:
            # détail pour les phrases du jour de la fiche (étape B2) : quelques coins par appareil
            out[cid] = cell_weather(cells[cid], raw, species, today, detail=True)
        except (HistoryTooShortError, SeriesGapError) as e:
            log.info("coins : cellule %s sans météo (%s)", cid, e)
    return out


@dataclass
class NotifyReport:
    devices: int
    sent: int
    skipped: int  # rien à annoncer
    failed: int
    tokens_cleared: int


async def notify_all(
    store: Store, grid: WeatherGrid, client: OpenMeteoClient, apns: ApnsClient
) -> NotifyReport:
    """Une notification par appareil au plus, seulement si un coin est bon ou excellent."""
    today = client.today()
    report = NotifyReport(0, 0, 0, 0, 0)
    for device, coins in store.devices_with_coins():
        report.devices += 1
        weather = await weather_for(coins, grid, client, today)
        message = compose(score_coins(coins, grid, weather, today), today)
        if message is None:
            report.skipped += 1
            continue
        title, body, coin_id = message
        res = await apns.send(
            device.apns_token, title, body, sandbox=device.sandbox, extra={"coin_id": coin_id}
        )
        if res.ok:
            report.sent += 1
        else:
            report.failed += 1
            if res.token_invalid:
                store.clear_token(device.user_id)
                report.tokens_cleared += 1
    log.info(
        "notifications coins : %d appareils, %d envoyées, %d sans rien à annoncer, %d échecs (%d jetons retirés)",
        report.devices,
        report.sent,
        report.skipped,
        report.failed,
        report.tokens_cleared,
    )
    return report
