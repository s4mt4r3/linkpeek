import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from app.cache import CacheBackend, InMemoryTTLCache, PreviewCache, normalize_url
from app.config import Settings, get_settings
from app.errors import LinkpeekError, RateLimitedError
from app.fetcher import Fetcher, build_client
from app.models import ErrorResponse, HealthResponse, PreviewResponse
from app.parser import parse_metadata
from app.ratelimit import SlidingWindowRateLimiter
from app.security import Resolver, parse_url, system_resolver

ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (400, 415, 429, 502, 504)}


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = system_resolver,
    cache_backend: CacheBackend | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    settings = settings or get_settings()
    client = build_client(settings, transport)
    fetcher = Fetcher(client, settings, resolver)
    cache = PreviewCache(
        cache_backend or InMemoryTTLCache(max_size=settings.cache_max_size, clock=clock),
        ttl=settings.cache_ttl,
        negative_ttl=settings.negative_cache_ttl,
    )

    limiter = SlidingWindowRateLimiter(settings.rate_limit_requests, settings.rate_limit_window, clock)

    def client_ip(request: Request) -> str:
        if settings.trust_x_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                # Rightmost entry is the one our proxy appended; anything to
                # its left came from the client and can be forged.
                return forwarded.split(",")[-1].strip()
        return request.client.host if request.client else "unknown"

    def rate_limit(request: Request) -> None:
        wait = limiter.check(client_ip(request))
        if wait is not None:
            raise RateLimitedError(limiter.retry_after_header(wait))

    async def load_preview(url: str) -> dict:
        result = await fetcher.fetch(url)
        meta = parse_metadata(result.html, result.final_url)
        return {
            "url": result.final_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            **meta.model_dump(),
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(title="linkpeek", version="1.0.0", lifespan=lifespan)
    app.state.cache = cache

    @app.exception_handler(LinkpeekError)
    async def linkpeek_error_handler(request: Request, exc: LinkpeekError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error=exc.code, detail=exc.message).model_dump(),
        )

    @app.exception_handler(RateLimitedError)
    async def rate_limited_handler(request: Request, exc: RateLimitedError) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": exc.retry_after},
            content=ErrorResponse(error="rate_limited", detail="Too many requests").model_dump(),
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/preview",
        response_model=PreviewResponse,
        responses=ERROR_RESPONSES,
        dependencies=[Depends(rate_limit)],
    )
    async def preview(
        response: Response, url: str = Query(..., max_length=settings.max_url_length)
    ) -> PreviewResponse:
        # Cheap static checks first, so garbage input never touches the cache.
        parse_url(url, allowed_ports=settings.allowed_ports, max_length=settings.max_url_length)
        key = normalize_url(url)
        data, cached = await cache.get_or_load(key, lambda: load_preview(key))
        response.headers["X-Cache"] = "HIT" if cached else "MISS"
        return PreviewResponse(**data, cached=cached)

    return app


app = create_app()
