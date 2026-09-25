"""OAuth2 client-credentials token management.

The token is cached in memory and refreshed slightly before it expires.  The
token value itself is never logged, never rendered in a template and never
returned to the browser.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# Refresh this many seconds before the server-stated expiry.
_EXPIRY_SKEW = 60


class OpsRampAuthError(RuntimeError):
    """Raised when an access token cannot be obtained."""


@dataclass
class _Token:
    value: str
    expires_at: float

    @property
    def valid(self) -> bool:
        return bool(self.value) and time.time() < (self.expires_at - _EXPIRY_SKEW)


class TokenManager:
    """Thread-safe OAuth2 token cache."""

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        token_path: str,
        client_id: str,
        client_secret: str,
        timeout: tuple[float, float],
        verify: bool = True,
    ) -> None:
        self._session = session
        self._url = f"{base_url.rstrip('/')}{token_path}"
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._verify = verify
        self._lock = threading.Lock()
        self._token: _Token | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and self._token.valid:
                return self._token.value
            self._token = self._fetch()
            return self._token.value

    # ------------------------------------------------------------------ internal
    def _fetch(self) -> _Token:
        if not self._client_id or not self._client_secret:
            raise OpsRampAuthError(
                "OpsRamp OAuth credentials are not configured. "
                "Set OPSRAMP_OAUTH_CLIENT_ID and OPSRAMP_OAUTH_CLIENT_SECRET."
            )
        logger.info("Requesting a new OpsRamp access token")
        try:
            response = self._session.post(
                self._url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
                verify=self._verify,
            )
        except requests.RequestException as exc:
            logger.error("OpsRamp token request failed at transport level: %s", exc)
            raise OpsRampAuthError(
                "Unable to reach the OpsRamp authentication endpoint."
            ) from exc

        if response.status_code in (400, 401, 403):
            logger.error(
                "OpsRamp rejected the OAuth credentials (HTTP %s)", response.status_code
            )
            raise OpsRampAuthError(
                "OpsRamp rejected the configured OAuth credentials "
                f"(HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            logger.error("OpsRamp token endpoint returned HTTP %s", response.status_code)
            raise OpsRampAuthError(
                f"OpsRamp token endpoint returned HTTP {response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OpsRampAuthError(
                "OpsRamp token endpoint returned a non-JSON response."
            ) from exc

        access_token = payload.get("access_token")
        if not access_token:
            raise OpsRampAuthError("OpsRamp token response did not contain a token.")

        try:
            expires_in = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600

        logger.info("Obtained OpsRamp access token (expires in %ss)", expires_in)
        return _Token(value=access_token, expires_at=time.time() + expires_in)
