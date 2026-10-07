import httpx

from app.config import Settings
from app.main import create_app
from app.ratelimit import SlidingWindowRateLimiter
from tests.conftest import FakeResolver


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_then_blocks():
    limiter = SlidingWindowRateLimiter(limit=3, window=60, clock=FakeClock())
    assert [limiter.check("ip") for _ in range(3)] == [None, None, None]
    assert limiter.check("ip") == 60


def test_window_slides_rather_than_resets():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=2, window=60, clock=clock)
    limiter.check("ip")  # t=1000
    clock.now += 30
    limiter.check("ip")  # t=1030
    clock.now += 31  # t=1061: first hit has aged out, second has not
    assert limiter.check("ip") is None
    wait = limiter.check("ip")
    assert wait is not None and 28 < wait < 30


def test_rejected_requests_do_not_extend_the_block():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window=10, clock=clock)
    limiter.check("ip")
    for _ in range(100):
        limiter.check("ip")
    clock.now += 10.01
    assert limiter.check("ip") is None


def test_clients_are_independent():
    limiter = SlidingWindowRateLimiter(limit=1, window=60, clock=FakeClock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None


def test_idle_clients_swept_from_memory():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=5, window=60, clock=clock)
    for i in range(100):
        limiter.check(f"ip{i}")
    clock.now += 61
    limiter.check("fresh")
    assert set(limiter._hits) == {"fresh"}


def test_retry_after_is_whole_seconds_and_at_least_one():
    limiter = SlidingWindowRateLimiter(limit=1, window=60)
    assert limiter.retry_after_header(0.2) == "1"
    assert limiter.retry_after_header(29.1) == "30"


def _app(**settings):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>t</title>")

    clock = FakeClock()
    app = create_app(Settings(**settings), transport=httpx.MockTransport(handler), resolver=FakeResolver(), clock=clock)
    return app, clock


async def test_endpoint_returns_429_with_retry_after():
    app, clock = _app(rate_limit_requests=2, rate_limit_window=60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        for _ in range(2):
            assert (await client.get("/preview", params={"url": "https://example.com/"})).status_code == 200
        resp = await client.get("/preview", params={"url": "https://example.com/"})
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "60"
        assert resp.json() == {"error": "rate_limited", "detail": "Too many requests"}

        clock.now += 61
        assert (await client.get("/preview", params={"url": "https://example.com/"})).status_code == 200


async def test_cache_hits_still_count_toward_limit():
    app, _ = _app(rate_limit_requests=3)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        codes = [(await client.get("/preview", params={"url": "https://example.com/"})).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


async def test_health_is_not_rate_limited():
    app, _ = _app(rate_limit_requests=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        assert {(await client.get("/health")).status_code for _ in range(5)} == {200}


async def test_forwarded_for_ignored_by_default():
    app, _ = _app(rate_limit_requests=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        await client.get("/preview", params={"url": "https://example.com/"}, headers={"x-forwarded-for": "1.1.1.1"})
        resp = await client.get("/preview", params={"url": "https://example.com/"}, headers={"x-forwarded-for": "2.2.2.2"})
    assert resp.status_code == 429


async def test_forwarded_for_uses_rightmost_hop_when_trusted():
    app, _ = _app(rate_limit_requests=1, trust_x_forwarded_for=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        url = {"url": "https://example.com/"}
        assert (await client.get("/preview", params=url, headers={"x-forwarded-for": "6.6.6.6, 1.1.1.1"})).status_code == 200
        # Spoofing the leftmost entry doesn't buy a fresh bucket.
        assert (await client.get("/preview", params=url, headers={"x-forwarded-for": "7.7.7.7, 1.1.1.1"})).status_code == 429
        assert (await client.get("/preview", params=url, headers={"x-forwarded-for": "2.2.2.2"})).status_code == 200
