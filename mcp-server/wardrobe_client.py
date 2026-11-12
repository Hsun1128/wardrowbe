"""HTTP client for the wardrowbe backend, shared by every MCP tool.

One process serves several people: each request carries an `Identity`, and this
client keeps a separate backend JWT per identity. Sign-in uses the backend's
dev-mode credential path (`POST /api/v1/auth/sync` with DEBUG=true and no OIDC),
which is the only path available until per-user OAuth lands.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

API_PREFIX = "/api/v1"
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# The backend issues 7-day JWTs (backend/app/api/auth.py: create_access_token).
# Re-sync a day early; a 401 also forces a re-sync.
JWT_LIFETIME_SECONDS = 6 * 24 * 3600

# Response keys that hold a backend-relative image path.
_IMAGE_KEYS = frozenset(
    {
        "image_url",
        "thumbnail_url",
        "medium_url",
        "original_image_url",
        "composite_image_url",
    }
)


class BackendError(RuntimeError):
    """The backend rejected a request, or could not be reached."""


@dataclass(frozen=True)
class Identity:
    """A wardrowbe account this server may act as."""

    email: str
    display_name: str

    @property
    def external_id(self) -> str:
        # Same derivation as the web UI's dev login (frontend/lib/auth.ts:
        # DevCredentialsProvider), so a person lands in the same account whether
        # they sign in through the browser or through MCP.
        return re.sub(r"[^a-z0-9]", "-", self.email.lower())


class WardrobeBackend:
    def __init__(self, base_url: str, public_url: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._public_url = public_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=HTTP_TIMEOUT)
        self._tokens: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _token(self, identity: Identity, *, force: bool = False) -> str:
        key = identity.external_id
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(key)
            if cached and not force and cached[1] > time.monotonic():
                return cached[0]
            try:
                response = await self._client.post(
                    f"{API_PREFIX}/auth/sync",
                    json={
                        "external_id": identity.external_id,
                        "email": identity.email,
                        "display_name": identity.display_name,
                    },
                )
            except httpx.HTTPError as e:
                raise BackendError(f"Cannot reach wardrowbe at {self._base_url}: {e}") from e
            if response.status_code >= 400:
                raise BackendError(
                    f"Sign-in failed ({response.status_code}): {_error_detail(response)}"
                )
            token = response.json()["access_token"]
            self._tokens[key] = (token, time.monotonic() + JWT_LIFETIME_SECONDS)
            return token

    async def request(
        self,
        identity: Identity,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        data: Optional[dict[str, str]] = None,
        files: Optional[dict[str, tuple[str, bytes, str]]] = None,
    ) -> Any:
        """Call an authenticated endpoint as `identity`; returns the decoded JSON body."""
        if params:
            params = {k: _query_value(v) for k, v in params.items() if v is not None}
        response: Optional[httpx.Response] = None
        for attempt in (0, 1):
            token = await self._token(identity, force=attempt == 1)
            try:
                response = await self._client.request(
                    method,
                    f"{API_PREFIX}{path}",
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as e:
                raise BackendError(f"Cannot reach wardrowbe at {self._base_url}: {e}") from e
            if response.status_code != 401:
                break
        assert response is not None
        if response.status_code >= 400:
            raise BackendError(
                f"{method} {path} failed ({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return self._absolute_image_urls(response.json())

    async def public_get(self, path: str) -> Any:
        """Call an endpoint that needs no sign-in (health, capabilities, auth config)."""
        try:
            response = await self._client.get(f"{API_PREFIX}{path}")
        except httpx.HTTPError as e:
            raise BackendError(f"Cannot reach wardrowbe at {self._base_url}: {e}") from e
        if response.status_code >= 400:
            raise BackendError(f"GET {path} failed ({response.status_code}): {_error_detail(response)}")
        return response.json()

    def _absolute_image_urls(self, payload: Any) -> Any:
        """Rewrite backend-relative image links to the origin clients can open."""
        if not self._public_url:
            return payload
        if isinstance(payload, list):
            return [self._absolute_image_urls(entry) for entry in payload]
        if isinstance(payload, dict):
            return {
                key: (
                    f"{self._public_url}{value}"
                    if key in _IMAGE_KEYS and isinstance(value, str) and value.startswith("/")
                    else self._absolute_image_urls(value)
                )
                for key, value in payload.items()
            }
        return payload

    async def aclose(self) -> None:
        await self._client.aclose()


def _query_value(value: Any) -> Any:
    # httpx would send Python's "True"/"False"; FastAPI accepts those, but the
    # lowercase form is what the web UI sends and what the API documents.
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        return response.text
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)
