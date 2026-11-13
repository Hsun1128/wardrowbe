import ipaddress
import socket

import httpx
import pytest
from fastapi import HTTPException

from app.utils import image_url_fetch
from app.utils.image_url_fetch import _download, fetch_image_from_url, is_public_address


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.0.104",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT: not private, not global
        "0.0.0.0",
        "224.0.0.1",
        "198.18.0.1",  # benchmarking
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.5",
        "2002:7f00:0001::",  # 6to4 wrapping 127.0.0.1
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 wrapping 169.254.169.254
    ],
)
def test_non_public_addresses_are_refused(address):
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"]
)
def test_public_addresses_are_allowed(address):
    assert is_public_address(ipaddress.ip_address(address))


def _resolves_to(monkeypatch, *addresses):
    async def fake_getaddrinfo(self, host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in addresses
        ]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "url", ["ftp://example.com/a.jpg", "file:///etc/passwd", "http:///a.jpg", "http://example.com:99999/a.jpg"]
)
async def test_bad_urls_are_refused(url):
    with pytest.raises(HTTPException) as e:
        await fetch_image_from_url(url)
    assert e.value.status_code == 400


async def test_host_with_any_internal_address_is_refused(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34", "10.0.0.5")
    with pytest.raises(HTTPException) as e:
        await fetch_image_from_url("https://shop.example/a.jpg")
    assert e.value.detail == "image_url points to a host that is not allowed"


async def test_request_goes_to_the_validated_address_with_original_host(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, content=b"img", headers={"content-type": "image/png; charset=binary"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        image_url_fetch.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    content, content_type = await fetch_image_from_url("https://shop.example/p/a.jpg?w=480")
    assert (content, content_type) == (b"img", "image/png")
    assert seen == {
        "url": "https://93.184.216.34/p/a.jpg?w=480",
        "host": "shop.example",
        "sni": "shop.example",
    }


async def test_redirect_is_not_followed(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    real_client = httpx.AsyncClient
    handler = lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/"})  # noqa: E731
    monkeypatch.setattr(
        image_url_fetch.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(HTTPException) as e:
        await fetch_image_from_url("http://shop.example/a.jpg")
    assert "redirected" in e.value.detail


async def _send(handler, max_bytes):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await _download(client, client.build_request("GET", "http://93.184.216.34/a.jpg"), max_bytes)


async def test_body_within_the_limit_is_returned():
    _, content = await _send(lambda r: httpx.Response(200, content=b"x" * 100), max_bytes=100)
    assert content == b"x" * 100


async def test_declared_length_over_the_limit_is_refused():
    with pytest.raises(HTTPException) as e:
        await _send(lambda r: httpx.Response(200, content=b"x" * 101), max_bytes=100)
    assert e.value.status_code == 413


async def test_undeclared_body_is_cut_off_at_the_limit():
    produced = 0

    async def endless():
        nonlocal produced
        while True:
            produced += 10
            yield b"x" * 10

    with pytest.raises(HTTPException) as e:
        await _send(lambda r: httpx.Response(200, content=endless()), max_bytes=100)
    assert e.value.status_code == 413
    assert produced <= 120  # stopped reading right after the limit, not at "the end"
