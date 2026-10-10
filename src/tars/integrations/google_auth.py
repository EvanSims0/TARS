"""Google sign-in with your own OAuth client.

Publish the OAuth app to production (as unverified, personal use) so the
refresh token doesn't expire every 7 days the way testing-mode tokens do.

Scopes are limited to reading/labelling mail, drafting and sending, and calendar
events. ``gmail.modify`` covers read and label/archive but not permanent
deletion; the full-mailbox ``https://mail.google.com/`` scope is never requested.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .. import net
from ..actions import ToolError
from ..secrets import GOOGLE_OAUTH_TOKEN, get_secret, set_secret

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def run_consent_flow(client_secrets_file: Path) -> None:
    """One-time browser sign-in; stores the refresh token in the credential store."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        raise SystemExit("Google didn't return a refresh token; remove TARS at "
                         "https://myaccount.google.com/permissions and run this again.")
    # Only what a refresh needs: no access token, and small enough for Windows Credential Manager.
    set_secret(GOOGLE_OAUTH_TOKEN, json.dumps({
        "refresh_token": creds.refresh_token, "client_id": creds.client_id, "client_secret": creds.client_secret,
    }))


class GoogleSession:
    """Holds an access token and refreshes it with the stored refresh token."""

    def __init__(self, token_info: dict[str, Any], http: httpx.AsyncClient | None = None):
        self._info = token_info
        self._http = http or net.client()
        self._access: str | None = None
        self._expires = 0.0

    @classmethod
    def from_store(cls, http: httpx.AsyncClient | None = None) -> GoogleSession | None:
        raw = get_secret(GOOGLE_OAUTH_TOKEN)
        return cls(json.loads(raw), http) if raw else None

    async def token(self) -> str:
        if self._access and time.time() < self._expires - 60:
            return self._access
        try:
            resp = await self._http.post(TOKEN_URI, data={
                "grant_type": "refresh_token",
                "refresh_token": self._info["refresh_token"],
                "client_id": self._info["client_id"],
                "client_secret": self._info["client_secret"],
            }, extensions={"idempotent": True})
        except httpx.HTTPError as e:
            raise ToolError("Google isn't reachable right now.") from e
        if resp.status_code != 200:
            raise ToolError("Google sign-in has expired; run `tars google-auth` at the PC.")
        body = resp.json()
        self._access = body["access_token"]
        self._expires = time.time() + body.get("expires_in", 3600)
        return self._access

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {await self.token()}"}
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as e:
                raise ToolError("Google isn't reachable right now.") from e
            if resp.status_code != 401:
                break
            # The hour-long access token ran out (or was revoked): sign in again and repeat once.
            # A 401 means Google refused the request before doing anything, so repeating is safe.
            self._access = None
        if resp.status_code == 401:
            raise ToolError("Google rejected the sign-in; reconnect Google in Settings, Accounts.")
        if resp.status_code >= 400:
            raise ToolError(f"Google returned an error ({resp.status_code}).")
        return resp.json() if resp.content else None
