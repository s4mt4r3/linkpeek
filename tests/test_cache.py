import asyncio

import httpx
import pytest

from app.cache import InMemoryTTLCache, PreviewCache, normalize_url
from app.config import Settings
from app.errors import BlockedURLError, UpstreamError, UpstreamTimeoutError
from app.main import create_app
from tests.conftest import FakeResolver


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTP://EXAMPLE.COM", "http://example.com/"),
        ("http://Example.com/Path", "http://example.com/Path"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?b=2&a=1#x", "https://example.com/a?b=2&a=1"),
        ("https://example.com./", "https://example.com/"),
        ("http://[2606:2800::1]:80/", "http://[2606:2800::1]/"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_rejects_garbage():
    with pytest.raises(BlockedURLError):
        normalize_url("http://①.com/")


async def test_ttl_expiry():
    clock = FakeClock()
    store = InMemoryTTLCache(clock=clock)
    await store.set("k", {"v": 1}, ttl=10)
    clock.now += 9.9
    assert await store.get("k") == {"v": 1}
    clock.now += 0.2
    assert await store.get("k") is None
    assert len(store) == 0


async def test_lru_eviction_at_max_size():
    store = InMemoryTTLCache(max_size=2)
    await store.set("a", {"v": "a"}, 60)
    await store.set("b", {"v": "b"}, 60)
    await store.get("a")  # a becomes most recently used
    await store.set("c", {"v": "c"}, 60)
    assert await store.get("b") is None
    assert await store.get("a") is not None
    assert await store.get("c") is not None


def make_cache(clock=None, ttl=3600, negative_ttl=60):
    return PreviewCache(InMemoryTTLCache(clock=clock or FakeClock()), ttl=ttl, negative_ttl=negative_ttl)


async def test_miss_then_hit():
    cache = make_cache()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return {"title": "t"}

    assert await cache.get_or_load("k", loader) == ({"title": "t"}, False)
    assert await cache.get_or_load("k", loader) == ({"title": "t"}, True)
    assert calls == 1


async def test_entry_refetched_after_ttl():
    clock = FakeClock()
    cache = make_cache(clock, ttl=100)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return {"n": calls}

    await cache.get_or_load("k", loader)
    clock.now += 101
    assert await cache.get_or_load("k", loader) == ({"n": 2}, False)


async def test_negative_caching_replays_error_then_expires():
    clock = FakeClock()
    cache = make_cache(clock, negative_ttl=60)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        raise UpstreamTimeoutError("Upstream request timed out")

    for _ in range(3):
        with pytest.raises(UpstreamTimeoutError, match="timed out"):
            await cache.get_or_load("k", loader)
    assert calls == 1

    clock.now += 61
    with pytest.raises(UpstreamTimeoutError):
        await cache.get_or_load("k", loader)
    assert calls == 2


async def test_negative_ttl_shorter_than_positive():
    clock = FakeClock()
    cache = make_cache(clock, ttl=3600, negative_ttl=60)

    async def fail():
        raise UpstreamError("Upstream returned HTTP 503")

    with pytest.raises(UpstreamError):
        await cache.get_or_load("k", fail)
    clock.now += 61

    async def ok():
        return {"title": "recovered"}

    assert await cache.get_or_load("k", ok) == ({"title": "recovered"}, False)


async def test_unexpected_errors_are_not_cached():
    cache = make_cache()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        raise RuntimeError("bug")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cache.get_or_load("k", loader)
    assert calls == 2


async def test_concurrent_requests_share_one_fetch():
    cache = make_cache()
    calls = 0
    release = asyncio.Event()

    async def loader():
        nonlocal calls
        calls += 1
        await release.wait()
        return {"title": "shared"}

    waiters = [asyncio.create_task(cache.get_or_load("k", loader)) for _ in range(50)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters)
    assert calls == 1
    assert all(r == ({"title": "shared"}, False) for r in results)
    assert cache._inflight == {}


async def test_concurrent_failures_share_one_fetch():
    cache = make_cache()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        raise UpstreamError("Could not connect to upstream")

    results = await asyncio.gather(*(cache.get_or_load("k", loader) for _ in range(20)), return_exceptions=True)
    assert calls == 1
    assert all(isinstance(r, UpstreamError) for r in results)


async def test_different_keys_fetch_independently():
    cache = make_cache()
    seen = []

    async def loader_for(key):
        seen.append(key)
        await asyncio.sleep(0.01)
        return {"k": key}

    await asyncio.gather(*(cache.get_or_load(k, lambda k=k: loader_for(k)) for k in ("a", "b", "a", "b")))
    assert sorted(seen) == ["a", "b"]


async def test_cancelled_waiter_does_not_cancel_shared_fetch():
    cache = make_cache()
    release = asyncio.Event()

    async def loader():
        await release.wait()
        return {"title": "t"}

    first = asyncio.create_task(cache.get_or_load("k", loader))
    second = asyncio.create_task(cache.get_or_load("k", loader))
    await asyncio.sleep(0)
    first.cancel()
    release.set()
    assert await second == ({"title": "t"}, False)
    assert await cache.get_or_load("k", loader) == ({"title": "t"}, True)


async def test_endpoint_concurrent_requests_hit_upstream_once():
    upstream_calls = 0

    async def handler(request):
        nonlocal upstream_calls
        upstream_calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>Hi</title>")

    resolver = FakeResolver()
    app = create_app(
        Settings(rate_limit_requests=1000),
        transport=httpx.MockTransport(handler),
        resolver=resolver,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        # Different spellings of the same URL normalize to one key.
        urls = ["https://example.com/", "HTTPS://EXAMPLE.COM", "https://example.com:443/#top"] * 10
        responses = await asyncio.gather(*(client.get("/preview", params={"url": u}) for u in urls))
        assert {r.status_code for r in responses} == {200}
        assert upstream_calls == 1
        assert resolver.calls == ["example.com"]

        again = await client.get("/preview", params={"url": "https://example.com"})
        assert again.json()["cached"] is True
        assert again.headers["x-cache"] == "HIT"
        assert again.json()["fetched_at"] == responses[0].json()["fetched_at"]
