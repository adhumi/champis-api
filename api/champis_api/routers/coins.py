"""`PUT /coins` (liste complète des favoris + jeton APNs) et `GET /coins` (mes coins avec leur
score du jour). Spec › App iOS › Mes coins (30 oct. 2026) : la notification est calculée et
envoyée par le backend après la pré-chauffe du matin (`champis_api.coins.notify_all`)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .. import offload
from ..catalog import served_groups, served_species
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


class CoinIn(BaseModel):
    id: UUID
    name: str = Field(min_length=1, max_length=80)
    lat: float = Field(ge=41.0, le=51.5)
    lon: float = Field(ge=-5.5, le=10.0)
    target: str  # espèce ou groupe que l'utilisateur a choisi d'afficher
    # altitude du pixel (m) lue dans la couche E, ou centre de sa classe de 300 m (couche A)
    alt_class: int = Field(ge=0, le=5000)
    t: dict[str, float] = Field(min_length=1)  # espèce → T du pixel, lu dans les tuiles

    @model_validator(mode="after")
    def _known(self) -> CoinIn:
        known = set(served_species())  # ce que l'app peut afficher (catalogue)
        if self.target not in known and self.target not in served_groups():
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
    # lecture de la base et calcul des scores dans des fils, hors de la boucle d'événements
    # (13 nov. 2026) ; seuls les appels Open-Meteo de `weather_for` restent dans la boucle
    coins = await offload.run(store.coins_of, user)
    today = st.client.today()
    weather = await weather_for(coins, st.grid, st.client, today)
    scores = await offload.run(score_coins, coins, st.grid, weather, today)
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
                # phrases du jour, espèce porteuse, classe du coin (étape B2, 6 nov. 2026)
                "summary": list(s.summary),
            }
            for s in scores
        ],
    }
