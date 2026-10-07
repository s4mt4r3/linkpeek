"""End-to-end through the HTTP layer: status mapping and response shapes."""

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.conftest import PUBLIC_IP, FakeResolver

OG_PAGE = """
<html><head>
  <title>Fallback</title>
  <meta property="og:title" content="Hello">
  <meta property="og:description" content="A page">
  <meta property="og:image" content="/img.png">
  <meta property="og:site_name" content="Example">
  <link rel="icon" href="/favicon.ico">
</head></html>
"""


def html(text=OG_PAGE):
    return httpx.Response(200, headers={"content-type": "text/html"}, text=text)


def raises(exc):
    def handler(request):
        raise exc

    return handler


def client_for(handler, resolver=None, **settings) -> httpx.AsyncClient:
    app = create_app(
        Settings(**{"rate_limit_requests": 1000, **settings}),
        transport=httpx.MockTransport(handler),
        resolver=resolver or FakeResolver(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t")


async def get_preview(client, url):
    return await client.get("/preview", params={"url": url})


async def test_health():
    async with client_for(lambda r: html()) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}


async def test_preview_full_response():
    async with client_for(lambda r: html()) as client:
        resp = await get_preview(client, "https://example.com/post")
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://example.com/post"
    assert body["title"] == "Hello"
    assert body["description"] == "A page"
    assert body["image"] == "https://example.com/img.png"
    assert body["site_name"] == "Example"
    assert body["favicon"] == "https://example.com/favicon.ico"
    assert body["cached"] is False
    assert body["fetched_at"].endswith(("Z", "+00:00"))
    assert resp.headers["x-cache"] == "MISS"


async def test_relative_urls_resolved_against_final_url_after_redirect():
    resolver = FakeResolver({"cdn.example.org": [PUBLIC_IP]})

    def handler(request):
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://cdn.example.org/deep/page"})
        return html('<meta property="og:image" content="pic.jpg">')

    async with client_for(handler, resolver) as client:
        body = (await get_preview(client, "https://example.com/")).json()
    assert body["url"] == "https://cdn.example.org/deep/page"
    assert body["image"] == "https://cdn.example.org/deep/pic.jpg"


async def test_missing_url_param_is_422():
    async with client_for(lambda r: html()) as client:
        assert (await client.get("/preview")).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://[::ffff:10.0.0.1]/",
        "http://0x7f000001/",
        "file:///etc/passwd",
        "http://admin:hunter2@example.com/",
        "http://example.com:25/",
    ],
)
async def test_blocked_urls_are_400_and_never_fetched(url):
    fetched = []
    async with client_for(lambda r: fetched.append(r) or html()) as client:
        resp = await get_preview(client, url)
    assert resp.status_code == 400
    assert resp.json()["error"] == "blocked_url"
    assert fetched == []


async def test_blocked_message_does_not_leak_resolved_ip():
    resolver = FakeResolver({"internal.example": ["10.20.30.40"]})
    async with client_for(lambda r: html(), resolver) as client:
        resp = await get_preview(client, "https://internal.example/")
    assert resp.status_code == 400
    assert "10.20.30.40" not in resp.text
    assert "private" not in resp.text.lower()


async def test_redirect_to_metadata_endpoint_is_400():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    async with client_for(handler) as client:
        resp = await get_preview(client, "https://example.com/")
    assert resp.status_code == 400
    assert len(hits) == 1


@pytest.mark.parametrize(
    "handler, status, error",
    [
        (raises(httpx.ReadTimeout("slow")), 504, "upstream_timeout"),
        (raises(httpx.ConnectTimeout("slow")), 504, "upstream_timeout"),
        (raises(httpx.ConnectError("refused")), 502, "upstream_error"),
        (lambda r: httpx.Response(500, headers={"content-type": "text/html"}), 502, "upstream_error"),
        (lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}), 415, "unsupported_content_type"),
        (lambda r: httpx.Response(302, headers={"location": "/loop"}), 502, "upstream_error"),
    ],
)
async def test_upstream_failure_status_mapping(handler, status, error):
    async with client_for(handler) as client:
        resp = await get_preview(client, "https://example.com/loop")
    assert resp.status_code == status
    assert resp.json()["error"] == error
    assert set(resp.json()) == {"error", "detail"}


async def test_failures_are_negatively_cached():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("refused")

    async with client_for(handler) as client:
        codes = [(await get_preview(client, "https://example.com/")).status_code for _ in range(5)]
    assert codes == [502] * 5
    assert len(calls) == 1


async def test_fragment_and_case_share_cache_entry():
    calls = []

    def handler(request):
        calls.append(request)
        return html()

    async with client_for(handler) as client:
        first = await get_preview(client, "https://Example.com/a#one")
        second = await get_preview(client, "HTTPS://EXAMPLE.COM:443/a#two")
    assert len(calls) == 1
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert second.headers["x-cache"] == "HIT"


async def test_url_too_long_rejected():
    async with client_for(lambda r: html()) as client:
        resp = await get_preview(client, "https://example.com/" + "a" * 3000)
    assert resp.status_code == 422
