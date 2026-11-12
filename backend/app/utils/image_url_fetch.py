"""Download a product/garment image from a caller-supplied URL.

Ported from the AIWardrobe prototype's ``backend/api/manual_ingest.py`` (same
SSRF-hardening approach), adapted to this project's logging and exception
conventions. Lets a caller (a human, or a multimodal agent that already
looked at a product page) hand over an image URL instead of uploading bytes
directly, e.g. for ``POST /api/v1/items`` with ``image_url`` set.
"""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import socket
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status

from app.services.image_service import ALLOWED_MIME_TYPES

logger = logging.getLogger(__name__)

_ALLOWED_URL_SCHEMES = {"http", "https"}
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _reject_non_public_host(hostname: str, image_url: str) -> None:
    """SSRF guard: resolve every IP for `hostname` and refuse if any of them is
    private/loopback/link-local/reserved/multicast/unspecified (covers
    127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fc00::/7, etc).

    Known limitation: this validates-then-connects, so it does not fully
    defend against DNS rebinding (the resolution httpx performs for the
    actual request can differ from this check). Closing that gap needs the
    connection pinned to the already-validated IP (a custom transport) -
    deferred until this app is actually public-facing.
    """
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_url host could not be resolved",
        ) from exc

    for _, _, _, _, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            logger.warning(
                "Blocked manual item add from image_url resolving to a non-public address: "
                "%s -> %s",
                image_url,
                ip,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="image_url points to a host that is not allowed",
            )


def _validate_image_url(image_url: str) -> None:
    parsed = urlparse(image_url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_url must be a valid http/https URL",
        )
    _reject_non_public_host(parsed.hostname, image_url)


async def fetch_image_from_url(image_url: str) -> tuple[bytes, str]:
    """Download the image at `image_url`, returning (bytes, content_type).
    Validated against SSRF before any request is made; redirects are not
    followed, and failure detail is logged server-side only (never echoed
    back to the caller)."""
    _validate_image_url(image_url)

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.get(
                image_url,
                # Some storefront CDNs reject an obviously-bot user agent.
                headers={"User-Agent": _DESKTOP_USER_AGENT},
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to download image_url: %s", image_url, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not download image_url; confirm it is directly reachable",
        ) from None

    if response.status_code in (301, 302, 303, 307, 308):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image_url redirected; provide the final direct image URL",
        )
    if response.status_code >= 400:
        logger.warning(
            "Failed to download image_url: %s -> HTTP %s", image_url, response.status_code
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not download image_url")
    if not response.content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="image_url returned no content")

    # The server's declared type often carries extra parameters (e.g. "image/jpeg;
    # charset=binary") that validate_image()'s exact-match allowlist check would
    # reject outright; strip those. Fall back to guessing from the URL's own
    # extension, then to a generic default -- the real content check still happens
    # afterwards via Image.open() in validate_image(), this MIME gate is coarse.
    declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if declared in ALLOWED_MIME_TYPES:
        content_type = declared
    else:
        guessed, _ = mimetypes.guess_type(urlparse(image_url).path)
        content_type = guessed if guessed in ALLOWED_MIME_TYPES else "image/jpeg"

    return response.content, content_type
