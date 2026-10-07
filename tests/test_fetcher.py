import asyncio
import gzip

import httpx
import pytest

from app.config import Settings
from app.errors import BlockedURLError, UnsupportedContentTypeError, UpstreamError, UpstreamTimeoutError
from app.fetcher import Fetcher, build_client
from tests.conftest import PUBLIC_IP, FakeResolver

HTML = {"content-type": "text/html; charset=utf-8"}


def make_fetcher(handler, resolver=None, **overrides) -> Fetcher:
    settings = Settings(**overrides)
    client = build_client(settings, httpx.MockTransport(handler))
    return Fetcher(client, settings, resolver or FakeResolver())


def page(title="ok"):
    return httpx.Response(200, headers=HTML, text=f"<title>{title}</title>")


async def test_connects_to_validated_ip_with_original_host_header():
    seen = []

    def handler(request):
        seen.append(request)
        return page()

    await make_fetcher(handler).fetch("https://example.com/path?q=1")
    req = seen[0]
    assert req.url.host == PUBLIC_IP
    assert req.url.path == "/path"
    assert req.headers["host"] == "example.com"
    assert req.extensions["sni_hostname"] == "example.com"
    assert req.headers["user-agent"].startswith("linkpeek/")


async def test_dns_is_resolved_exactly_once_per_hop():
    resolver = FakeResolver()
    await make_fetcher(lambda r: page(), resolver).fetch("https://example.com/")
    assert resolver.calls == ["example.com"]


async def test_non_default_port_kept_in_host_header():
    seen = []

    def handler(request):
        seen.append(request)
        return page()

    await make_fetcher(handler).fetch("http://example.com:8080/")
    assert seen[0].url.port == 8080
    assert seen[0].headers["host"] == "example.com:8080"


async def test_follows_redirects_and_reports_final_url():
    resolver = FakeResolver({"www.example.com": [PUBLIC_IP]})

    def handler(request):
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/landing#frag"})
        return page("final")

    result = await make_fetcher(handler, resolver).fetch("http://example.com/")
    assert result.final_url == "https://www.example.com/landing"
    assert resolver.calls == ["example.com", "www.example.com"]


async def test_relative_redirect_resolved_against_current_url():
    def handler(request):
        if request.url.path == "/a/start":
            return httpx.Response(302, headers={"location": "../b/end"})
        return page()

    result = await make_fetcher(handler).fetch("https://example.com/a/start")
    assert result.final_url == "https://example.com/b/end"


async def test_redirect_to_internal_ip_blocked():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(BlockedURLError):
        await make_fetcher(handler).fetch("https://example.com/")
    assert len(calls) == 1


async def test_redirect_to_hostname_resolving_internal_blocked():
    resolver = FakeResolver({"sneaky.example": ["10.1.2.3"]})

    def handler(request):
        return httpx.Response(307, headers={"location": "http://sneaky.example/admin"})

    with pytest.raises(BlockedURLError):
        await make_fetcher(handler, resolver).fetch("https://example.com/")


@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "http://localhost/",
        "http://[::1]/",
        "http://user:pw@example.com/",
        "http://example.com:6379/",
        "http://2130706433/",
    ],
)
async def test_every_hop_gets_full_validation(location):
    def handler(request):
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(BlockedURLError):
        await make_fetcher(handler).fetch("https://example.com/")


async def test_redirect_chain_limit():
    hops = []

    def handler(request):
        hops.append(request.url.path)
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})

    with pytest.raises(UpstreamError, match="Too many redirects"):
        await make_fetcher(handler, max_redirects=3).fetch("https://example.com/0")
    # The original request plus three followed redirects, then give up.
    assert hops == ["/0", "/1", "/2", "/3"]


async def test_exactly_max_redirects_succeeds():
    def handler(request):
        n = int(request.url.path.strip("/"))
        if n < 3:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return page("end")

    result = await make_fetcher(handler, max_redirects=3).fetch("https://example.com/0")
    assert result.final_url == "https://example.com/3"


async def test_redirect_without_location_is_bad_upstream():
    with pytest.raises(UpstreamError):
        await make_fetcher(lambda r: httpx.Response(302)).fetch("https://example.com/")


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t")],
)
async def test_httpx_timeouts_map_to_upstream_timeout(exc):
    def handler(request):
        raise exc

    with pytest.raises(UpstreamTimeoutError):
        await make_fetcher(handler).fetch("https://example.com/")


async def test_total_budget_enforced_across_slow_hops():
    async def handler(request):
        await asyncio.sleep(0.15)
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/next"})
        return page()

    # Each hop is under any per-phase timeout, but together they blow the budget.
    with pytest.raises(UpstreamTimeoutError):
        await make_fetcher(handler, total_timeout=0.2).fetch("https://example.com/")


async def test_slow_dns_counts_against_total_budget():
    class SlowResolver(FakeResolver):
        async def __call__(self, host):
            await asyncio.sleep(1)
            return await super().__call__(host)

    with pytest.raises(UpstreamTimeoutError):
        await make_fetcher(lambda r: page(), SlowResolver(), total_timeout=0.05).fetch("https://example.com/")


async def test_slow_body_trickle_hits_total_budget():
    async def trickle():
        yield b"<html><head>"
        while True:
            await asyncio.sleep(0.02)
            yield b" "

    def handler(request):
        return httpx.Response(200, headers=HTML, content=trickle())

    with pytest.raises(UpstreamTimeoutError):
        await make_fetcher(handler, total_timeout=0.2).fetch("https://example.com/")


async def test_connect_error_maps_to_bad_gateway():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(UpstreamError):
        await make_fetcher(handler).fetch("https://example.com/")


async def test_connect_error_falls_back_to_next_validated_ip():
    resolver = FakeResolver({"multi.example": ["93.184.215.1", "93.184.215.2"]})
    tried = []

    def handler(request):
        tried.append(request.url.host)
        if request.url.host == "93.184.215.1":
            raise httpx.ConnectError("down")
        return page()

    await make_fetcher(handler, resolver).fetch("https://multi.example/")
    assert tried == ["93.184.215.1", "93.184.215.2"]


async def test_remote_protocol_error_maps_to_bad_gateway():
    def handler(request):
        raise httpx.RemoteProtocolError("garbage")

    with pytest.raises(UpstreamError):
        await make_fetcher(handler).fetch("https://example.com/")


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_upstream_error_status_is_bad_gateway(status):
    with pytest.raises(UpstreamError, match=str(status)):
        await make_fetcher(lambda r: httpx.Response(status, headers=HTML)).fetch("https://example.com/")


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "image/png", "application/pdf", "text/plain", "", "text/htmlx"],
)
async def test_non_html_rejected(content_type):
    headers = {"content-type": content_type} if content_type else {}
    with pytest.raises(UnsupportedContentTypeError):
        await make_fetcher(lambda r: httpx.Response(200, headers=headers, content=b"x")).fetch("https://example.com/")


@pytest.mark.parametrize("content_type", ["text/html", "TEXT/HTML; charset=utf-8", "application/xhtml+xml"])
async def test_html_content_types_accepted(content_type):
    resp = httpx.Response(200, headers={"content-type": content_type}, content=b"<title>t</title>")
    result = await make_fetcher(lambda r: resp).fetch("https://example.com/")
    assert "<title>t</title>" in result.html


async def test_body_capped_and_stream_abandoned_early():
    yielded = 0

    async def endless():
        nonlocal yielded
        while True:
            yielded += 1
            yield b"a" * 1024

    def handler(request):
        return httpx.Response(200, headers=HTML, content=endless())

    result = await make_fetcher(handler, max_body_bytes=10_000).fetch("https://example.com/")
    assert len(result.html) == 10_000
    assert yielded <= 11


async def test_cap_applies_to_decompressed_size():
    bomb = gzip.compress(b"a" * 5_000_000)
    assert len(bomb) < 10_000

    def handler(request):
        return httpx.Response(200, headers={**HTML, "content-encoding": "gzip"}, content=bomb)

    result = await make_fetcher(handler, max_body_bytes=50_000).fetch("https://example.com/")
    assert len(result.html) == 50_000


async def test_charset_from_meta_tag():
    body = '<meta charset="iso-8859-1"><title>caf\xe9</title>'.encode("latin-1")
    resp = httpx.Response(200, headers={"content-type": "text/html"}, content=body)
    result = await make_fetcher(lambda r: resp).fetch("https://example.com/")
    assert "café" in result.html
