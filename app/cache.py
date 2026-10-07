"""Preview cache: TTL + LRU storage behind a swappable backend interface,
with negative caching and per-key request coalescing on top."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from app.errors import ERRORS_BY_CODE, BlockedURLError, LinkpeekError

DEFAULT_PORTS = {"http": 80, "https": 443}
CacheValue = dict[str, Any]


def normalize_url(raw: str) -> str:
    """Canonical cache key: lowercase scheme and host, no fragment, no default
    port, and an explicit "/" path. Query strings are kept byte-for-byte
    because reordering parameters can change what a server returns."""
    try:
        url = httpx.URL(raw)
        host = url.raw_host.decode("ascii").lower().rstrip(".")
    except (httpx.InvalidURL, ValueError, UnicodeDecodeError):
        raise BlockedURLError("URL is malformed") from None
    if ":" in host:
        host = f"[{host}]"
    port = "" if url.port in (None, DEFAULT_PORTS.get(url.scheme)) else f":{url.port}"
    path = url.raw_path.decode("ascii") or "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"{url.scheme}://{host}{port}{path}"


class CacheBackend(Protocol):
    """Storage interface. Values are plain JSON-compatible dicts so a Redis
    backend can implement this with GET / SET EX and json.dumps."""

    async def get(self, key: str) -> CacheValue | None: ...

    async def set(self, key: str, value: CacheValue, ttl: float) -> None: ...


class InMemoryTTLCache:
    """Per-entry TTL with LRU eviction once max_size is reached.

    No lock is needed: methods never await, so on a single event loop each
    call runs to completion without interleaving.
    """

    def __init__(self, max_size: int = 1000, clock: Callable[[], float] = time.monotonic):
        self.max_size = max_size
        self.clock = clock
        self._data: OrderedDict[str, tuple[float, CacheValue]] = OrderedDict()

    async def get(self, key: str) -> CacheValue | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= self.clock():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: CacheValue, ttl: float) -> None:
        self._data[key] = (self.clock() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class PreviewCache:
    def __init__(self, backend: CacheBackend, ttl: float, negative_ttl: float):
        self.backend = backend
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self._inflight: dict[str, asyncio.Task[CacheValue]] = {}

    async def get_or_load(self, key: str, loader: Callable[[], Awaitable[CacheValue]]) -> tuple[CacheValue, bool]:
        """Return (value, from_cache). Raises the original LinkpeekError for
        failures, whether fresh or served from the negative cache.

        Concurrent callers for the same uncached key share one loader call.
        The in-flight check and task registration contain no await between
        them, which is what makes this race-free on a single event loop.
        """
        task = self._inflight.get(key)
        if task is None:
            hit = await self.backend.get(key)
            if hit is not None:
                return self._unwrap(hit), True
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._load_and_store(key, loader))
                self._inflight[key] = task
                # Marks the exception as retrieved if every waiter disconnects,
                # avoiding "Task exception was never retrieved" noise.
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
        # shield: one client disconnecting must not cancel the fetch that
        # other waiters (and the cache) are depending on.
        return self._unwrap(await asyncio.shield(task)), False

    async def _load_and_store(self, key: str, loader: Callable[[], Awaitable[CacheValue]]) -> CacheValue:
        try:
            try:
                value = {"ok": True, "data": await loader()}
                ttl = self.ttl
            except LinkpeekError as exc:
                value = {"ok": False, "code": exc.code, "detail": exc.message}
                ttl = self.negative_ttl
            await self.backend.set(key, value, ttl)
            return value
        finally:
            self._inflight.pop(key, None)

    @staticmethod
    def _unwrap(value: CacheValue) -> CacheValue:
        if value["ok"]:
            return value["data"]
        raise ERRORS_BY_CODE[value["code"]](value["detail"])
