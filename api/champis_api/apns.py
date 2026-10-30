"""Envoi de notifications APNs (Apple Push Notification service), authentification par jeton.

Une clé `.p8` (App Store Connect › Keys), son identifiant, l'identifiant d'équipe et le sujet
(identifiant de bundle de l'app) suffisent ; le JWT ES256 est régénéré toutes les 50 minutes
(Apple exige moins d'une heure, pas plus d'une par 20 minutes). HTTP/2 obligatoire (`httpx[http2]`).

Un jeton d'appareil refusé (410 `Unregistered`, 400 `BadDeviceToken`) est signalé à l'appelant,
qui cesse d'y envoyer : on n'insiste jamais sur un appareil désinscrit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

HOST = "https://api.push.apple.com"
HOST_SANDBOX = "https://api.sandbox.push.apple.com"
JWT_LIFETIME_S = 50 * 60
INVALID_TOKEN_REASONS = frozenset({"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"})


@dataclass(frozen=True)
class ApnsResult:
    ok: bool
    status: int
    reason: str | None = None

    @property
    def token_invalid(self) -> bool:
        return self.reason in INVALID_TOKEN_REASONS


class ApnsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key_pem: str,
        key_id: str,
        team_id: str,
        topic: str,
        now: Callable[[], float] = time.time,
    ):
        self.http = http
        self.key_pem = key_pem
        self.key_id = key_id
        self.team_id = team_id
        self.topic = topic
        self.now = now
        self._jwt: tuple[float, str] | None = None

    def bearer(self) -> str:
        t = self.now()
        if self._jwt is None or t - self._jwt[0] > JWT_LIFETIME_S:
            token = jwt.encode(
                {"iss": self.team_id, "iat": int(t)},
                self.key_pem,
                algorithm="ES256",
                headers={"kid": self.key_id},
            )
            self._jwt = (t, token)
        return self._jwt[1]

    async def send(
        self,
        device_token: str,
        title: str,
        body: str,
        sandbox: bool = False,
        collapse_id: str = "champis-coins",
        extra: dict[str, Any] | None = None,
    ) -> ApnsResult:
        url = f"{HOST_SANDBOX if sandbox else HOST}/3/device/{device_token}"
        payload: dict[str, Any] = {
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
            **(extra or {}),
        }
        headers = {
            "authorization": f"bearer {self.bearer()}",
            "apns-topic": self.topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-collapse-id": collapse_id,
        }
        try:
            r = await self.http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            log.warning("APNs injoignable : %s", e)
            return ApnsResult(False, 0, "unreachable")
        if r.status_code == 200:
            return ApnsResult(True, 200)
        reason = None
        with suppress(ValueError):
            reason = r.json().get("reason")
        log.warning("APNs %s : %s", r.status_code, reason)
        return ApnsResult(False, r.status_code, reason)
