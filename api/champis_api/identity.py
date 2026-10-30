"""Identité de l'app : identifiant aléatoire généré au premier lancement, gardé dans le trousseau
iCloud, envoyé dans l'en-tête `X-Champis-User` (spec › Backend, 30 oct. 2026). Pas de compte, pas
de mot de passe : c'est ce qui rend les observations et les coins privés à un appareil, et ce qui
permet de tout effacer d'un coup."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException

USER_HEADER = "X-Champis-User"


def _user_id(x_champis_user: Annotated[str | None, Header()] = None) -> str:
    if not x_champis_user:
        raise HTTPException(401, f"en-tête {USER_HEADER} requis (identifiant de l'app)")
    try:
        return str(uuid.UUID(x_champis_user))
    except ValueError as e:
        raise HTTPException(401, f"en-tête {USER_HEADER} : UUID attendu") from e


UserId = Annotated[str, Depends(_user_id)]
