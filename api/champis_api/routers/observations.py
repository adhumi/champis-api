"""`POST /observations` (lot idempotent), `GET /observations` (les miennes), `DELETE /observations`
(tout effacer : droit RGPD). Spec › Backend et › Retour terrain (30 oct. 2026).

Une observation = espèce, position, date, trouvé / rien trouvé, quantité 1–3 si trouvé. Pas de
photo, pas de commentaire, jamais partagée. Identité : en-tête `X-Champis-User`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from champis_pipeline.species import list_species
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from ..identity import UserId
from ..store import Store

router = APIRouter(prefix="/observations", tags=["observations"])

MAX_BATCH = 200


def _store(request: Request) -> Store:
    st = getattr(request.app.state.champis, "store", None)
    if st is None:
        raise HTTPException(503, "observations non configurées (CHAMPIS_DATABASE_URL)")
    return st


def _now(request: Request) -> datetime:
    return request.app.state.champis.client.now()


StoreDep = Annotated[Store, Depends(_store)]
Now = Annotated[datetime, Depends(_now)]


class ObservationIn(BaseModel):
    id: UUID
    species: str
    lat: float = Field(ge=41.0, le=51.5)  # France métropolitaine
    lon: float = Field(ge=-5.5, le=10.0)
    accuracy_m: float | None = Field(default=None, ge=0, le=10_000)
    date: date
    found: bool
    quantity: int | None = Field(default=None, ge=1, le=3)

    @model_validator(mode="after")
    def _coherent(self) -> ObservationIn:
        if self.species not in list_species():
            raise ValueError(f"espèce inconnue : {self.species}")
        if self.found and self.quantity is None:
            raise ValueError("quantity (1–3) requise quand found est vrai")
        if not self.found and self.quantity is not None:
            raise ValueError("quantity sans objet quand found est faux")
        return self


class ObservationBatch(BaseModel):
    observations: list[ObservationIn] = Field(min_length=1, max_length=MAX_BATCH)


@router.post("", status_code=201)
def post_observations(
    batch: ObservationBatch, user: UserId, store: StoreDep, now: Now
) -> dict[str, Any]:
    """Enregistre un lot. Les `id` déjà connus sont ignorés : renvoyer le même lot après une
    coupure réseau ne crée pas de doublon."""
    today = now.date()
    for o in batch.observations:
        if o.date > today + timedelta(days=1):
            raise HTTPException(422, f"date dans le futur : {o.date}")
    rows = [o.model_dump() | {"id": str(o.id)} for o in batch.observations]
    return store.add_observations(user, rows, now)


@router.get("")
def get_observations(user: UserId, store: StoreDep) -> dict[str, Any]:
    """Mes observations, les plus récentes d'abord. Celles des autres n'existent pas ici."""
    rows = store.observations_of(user)
    return {"count": len(rows), "observations": rows}


@router.delete("")
def delete_observations(user: UserId, store: StoreDep) -> dict[str, Any]:
    """Efface toutes mes observations, mes coins et mon jeton de notification."""
    return {"deleted": store.delete_observations(user)}
