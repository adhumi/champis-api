"""`PUT /coins` (liste complète des favoris + jeton APNs) et `GET /coins` (mes coins avec leur
score du jour). Spec › App iOS › Mes coins (30 oct. 2026) : la notification est calculée et
envoyée par le backend après la pré-chauffe du matin (`champis_api.coins.notify_all`)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from champis_pipeline.species import list_species, load_species
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from ..coins import score_coins, weather_for
from ..identity import UserId
from ..store import Store

router = APIRouter(prefix="/coins", tags=["coins"])

MAX_COINS = 50


def _store(request: Request) -> Store:
    st = getattr(request.app.state.champis, "store", None)
    if st is None:
        raise HTTPException(503, "coins non configurés (CHAMPIS_DATABASE_URL)")
    return st


StoreDep = Annotated[Store, Depends(_store)]


def _groups() -> set[str]:
    return {load_species(s)["group"]["id"] for s in list_species() if load_species(s).get("group")}


class CoinIn(BaseModel):
    id: UUID
    name: str = Field(min_length=1, max_length=80)
    lat: float = Field(ge=41.0, le=51.5)
    lon: float = Field(ge=-5.5, le=10.0)
    target: str  # espèce ou groupe que l'utilisateur a choisi d'afficher
    alt_class: int = Field(ge=0, le=5000)  # centre de la classe d'altitude du pixel (m)
    t: dict[str, float] = Field(min_length=1)  # espèce → T du pixel, lu dans les tuiles

    @model_validator(mode="after")
    def _known(self) -> CoinIn:
        known = set(list_species())
        if self.target not in known and self.target not in _groups():
            raise ValueError(f"target inconnu (espèce ou groupe) : {self.target}")
        bad = [s for s in self.t if s not in known]
        if bad:
            raise ValueError(f"espèces inconnues dans t : {bad}")
        if any(not 0.0 <= v <= 1.0 for v in self.t.values()):
            raise ValueError("t doit être entre 0 et 1")
        return self


class CoinsIn(BaseModel):
    apns_token: str | None = Field(
        default=None, min_length=16, max_length=200, pattern=r"^[0-9a-fA-F]+$"
    )
    sandbox: bool = False  # build de développement : passerelle APNs sandbox
    coins: list[CoinIn] = Field(max_length=MAX_COINS)


@router.put("")
def put_coins(body: CoinsIn, user: UserId, store: StoreDep, request: Request) -> dict[str, Any]:
    """Remplace la liste complète (le serveur ne fusionne pas) et le jeton de notification."""
    rows = [c.model_dump() | {"id": str(c.id)} for c in body.coins]
    n = store.put_coins(
        user, rows, body.apns_token, body.sandbox, request.app.state.champis.client.now()
    )
    return {"coins": n, "notifications": body.apns_token is not None}


@router.get("")
async def get_coins(user: UserId, store: StoreDep, request: Request) -> dict[str, Any]:
    """Mes coins avec le score du jour (et le meilleur jour à venir) calculés par le backend."""
    st = request.app.state.champis
    coins = store.coins_of(user)
    today = st.client.today()
    weather = await weather_for(coins, st.grid, st.client, today)
    scores = score_coins(coins, st.grid, weather, today)
    return {
        "today": today.isoformat(),
        "coins": [
            s.coin.as_dict()
            | {
                "score": s.today,
                "leading": s.leading,
                "best_day": s.best_day.isoformat() if s.best_day else None,
                "best": s.best,
                # onze jours J−3 → J+7 pour la fiche du coin et sa tendance (lot 1, 1er nov. 2026)
                "days": [
                    {"date": d.date.isoformat(), "value": d.value, "leading": d.leading}
                    for d in s.days
                ],
            }
            for s in scores
        ],
    }
