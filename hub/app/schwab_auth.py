import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.adapters.base import AdapterConnectionError, AdapterRejectedError
from app.db import get_config_value, set_config_value

logger = logging.getLogger(__name__)

REFRESH_TOKEN_CONFIG_KEY = "schwab_refresh_token"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float


class SchwabAuth:
    """Schwab's OAuth2 flow is authorization-code, not client-credentials:

    1. One-time, manual: visit `build_authorize_url()` in a browser, log in
       and approve, then the app's redirect_uri receives a `code` query
       param -- pass that to `exchange_code()` (see app/schwab_routes.py).
    2. After that, the refresh_token is valid for 7 days and this class
       auto-refreshes the short-lived (~30 min) access_token from it.
    3. When the refresh_token itself expires (7 days), step 1 must be
       repeated manually -- Schwab does not support a fully unattended
       flow beyond that window.

    The refresh_token is persisted in the `config` table so it survives
    hub restarts.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        auth_base: str,
        refresh_token: Optional[str] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.auth_base = auth_base
        self._refresh_token = refresh_token or get_config_value(REFRESH_TOKEN_CONFIG_KEY)
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def build_authorize_url(self) -> str:
        return f"{self.auth_base}/authorize?client_id={self.client_id}&redirect_uri={self.redirect_uri}"

    def _basic_auth_header(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return base64.b64encode(raw).decode()

    def _store_tokens(self, data: dict) -> None:
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data["expires_in"]
        if "refresh_token" in data:  # Schwab occasionally rotates it
            self._refresh_token = data["refresh_token"]
            set_config_value(REFRESH_TOKEN_CONFIG_KEY, self._refresh_token)

    async def exchange_code(self, code: str) -> TokenSet:
        """One-time exchange of an authorization `code` for the first token pair."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.auth_base}/token",
                headers={"Authorization": f"Basic {self._basic_auth_header()}"},
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
            )
        if resp.status_code >= 400:
            raise AdapterRejectedError(f"Schwab code exchange failed: {resp.status_code} {resp.text}")
        self._store_tokens(resp.json())
        logger.info("Schwab authorization code exchanged for tokens")
        return TokenSet(self._access_token, self._refresh_token, self._expires_at)

    async def _refresh(self) -> None:
        if not self._refresh_token:
            # A permanent configuration problem, not a transient blip -- retrying
            # won't ever fix a missing refresh_token, only the manual OAuth step will.
            raise AdapterRejectedError(
                "no Schwab refresh_token configured -- visit /schwab/authorize to run the one-time OAuth flow"
            )
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.auth_base}/token",
                    headers={"Authorization": f"Basic {self._basic_auth_header()}"},
                    data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                )
            except httpx.RequestError as exc:
                raise AdapterConnectionError(f"Schwab token refresh failed: {exc}") from exc
        if resp.status_code >= 500:
            # Schwab's own infrastructure having a bad moment -- worth a retry.
            raise AdapterConnectionError(f"Schwab token refresh failed: {resp.status_code} {resp.text}")
        if resp.status_code >= 400:
            # Refresh token itself is expired/revoked -- needs the manual OAuth
            # step again, not a retry.
            raise AdapterRejectedError(f"Schwab token refresh rejected: {resp.status_code} {resp.text}")
        self._store_tokens(resp.json())
        logger.info("Schwab access token refreshed")

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._access_token is None or time.time() > self._expires_at - 60:
                await self._refresh()
            return self._access_token
