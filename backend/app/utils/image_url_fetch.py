"""Download a product/garment image from a caller-supplied URL.

Ported from the AIWardrobe prototype's ``backend/api/manual_ingest.py``, adapted
to this project's logging and exception conventions. Lets a caller (a human, or a
multimodal agent that already looked at a product page) hand over an image URL
instead of uploading bytes, e.g. for ``POST /api/v1/items`` with ``image_url`` set.

The URL is attacker-controlled input, so the fetch is built to resist SSRF and
resource exhaustion:

* only http/https, and only to addresses that are globally routable;
* the hostname is resolved once and the connection goes to that exact address, so
  a DNS answer cannot change between the check and the request (DNS rebinding);
* redirects are not followed;
* the body is streamed and cut off at the upload size limit, under an overall
  deadline, so a huge or endless response cannot fill memory or hold a worker.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import mimetypes
import socket
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, status

from app.config import get_settings
from app.services.image_service import ALLOWED_MIME_TYPES

logger = logging.getLogger(__name__)

_ALLOWED_URL_SCHEMES = {"http", "https"}
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
# Per-phase limits (connect, each read); the whole download also has a deadline.
_PHASE_TIMEOUT = httpx.Timeout(10.0)
_TOTAL_DEADLINE_SECONDS = 30.0
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 transition address really leads to, if any."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(ip: IpAddress) -> bool:
    """True only for an address on the public internet.

    An allowlist (`is_global`) rather than a list of bad ranges, so ranges nobody
    thought to name (carrier-grade NAT 100.64/10, benchmarking, documentation,
    future reservations) are refused too. IPv6 forms that wrap an IPv4 address
    are judged by the address inside them.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return is_public_address(embedded)
    return ip.is_global and not ip.is_multicast


async def _resolve_public_address(hostname: str, port: int, image_url: str) -> IpAddress:
    """Resolve `hostname` once and return the address to connect to.

    Every address the name resolves to must be public: a name that answers with
    both a public and an internal address is refused outright.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise _bad_request("image_url host could not be resolved") from exc

    addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addresses:
        raise _bad_request("image_url host could not be resolved")
    for ip in addresses:
        if not is_public_address(ip):
            logger.warning(
                "Blocked item add from image_url resolving to a non-public address: %s -> %s",
                image_url,
                ip,
            )
            raise _bad_request("image_url points to a host that is not allowed")

    # IPv4 first: containers often have no IPv6 route.
    return next((ip for ip in addresses if ip.version == 4), addresses[0])


async def _download(
    client: httpx.AsyncClient, request: httpx.Request, max_bytes: int
) -> tuple[httpx.Response, bytes]:
    """Send `request` and read at most `max_bytes` of body, returning the response
    (for its status and headers) and the body. Raises 413 as soon as the response
    is known to be larger."""
    too_large = HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"image_url is larger than {max_bytes // (1024 * 1024)}MB",
    )
    response = await client.send(request, stream=True)
    try:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise too_large
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise too_large
    finally:
        await response.aclose()
    return response, bytes(body)


async def fetch_image_from_url(image_url: str) -> tuple[bytes, str]:
    """Download the image at `image_url`, returning (bytes, content_type).

    Failure detail is logged server-side only and never echoed to the caller, so
    the endpoint cannot be used to probe what an internal address answers.
    """
    try:
        parsed = urlsplit(image_url)
        port = parsed.port
    except ValueError as exc:
        raise _bad_request("image_url must be a valid http/https URL") from exc
    if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
        raise _bad_request("image_url must be a valid http/https URL")
    hostname = parsed.hostname
    port = port or (443 if parsed.scheme == "https" else 80)

    ip = await _resolve_public_address(hostname, port, image_url)

    # Connect to the address that was just validated instead of letting the HTTP
    # client resolve the name again. The Host header and the TLS server name stay
    # the original hostname, so virtual hosting works and the certificate is still
    # verified against the name the caller asked for.
    ip_host = f"[{ip}]" if ip.version == 6 else str(ip)
    path = parsed.path or "/"
    target = f"{parsed.scheme}://{ip_host}:{port}{path}" + (f"?{parsed.query}" if parsed.query else "")
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = hostname if port == default_port else f"{hostname}:{port}"
    max_bytes = get_settings().max_upload_size_mb * 1024 * 1024

    try:
        async with asyncio.timeout(_TOTAL_DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                timeout=_PHASE_TIMEOUT, follow_redirects=False, trust_env=False
            ) as client:
                request = client.build_request(
                    "GET",
                    target,
                    headers={
                        "Host": host_header,
                        # Some storefront CDNs reject an obviously-bot user agent.
                        "User-Agent": _DESKTOP_USER_AGENT,
                    },
                    extensions={"sni_hostname": hostname},
                )
                response, content = await _download(client, request, max_bytes)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to download image_url: %s", image_url, exc_info=True)
        raise _bad_request(
            "Could not download image_url; confirm it is directly reachable"
        ) from None

    if response.status_code in (301, 302, 303, 307, 308):
        raise _bad_request("image_url redirected; provide the final direct image URL")
    if response.status_code >= 400:
        logger.warning(
            "Failed to download image_url: %s -> HTTP %s", image_url, response.status_code
        )
        raise _bad_request("Could not download image_url")
    if not content:
        raise _bad_request("image_url returned no content")

    # The server's declared type often carries extra parameters (e.g. "image/jpeg;
    # charset=binary") that validate_image()'s exact-match allowlist check would
    # reject outright; strip those. Fall back to guessing from the URL's own
    # extension, then to a generic default -- the real content check still happens
    # afterwards via Image.open() in validate_image(), this MIME gate is coarse.
    declared_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if declared_type in ALLOWED_MIME_TYPES:
        content_type = declared_type
    else:
        guessed, _ = mimetypes.guess_type(parsed.path)
        content_type = guessed if guessed in ALLOWED_MIME_TYPES else "image/jpeg"

    return content, content_type
