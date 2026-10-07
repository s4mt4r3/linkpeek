import asyncio
import re
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.errors import UnsupportedContentTypeError, UpstreamError, UpstreamTimeoutError
from app.security import Resolver, ValidatedTarget, system_resolver, validate_url

ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_.:-]+)""", re.IGNORECASE)


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    html: str


def build_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.read_timeout,
            pool=settings.connect_timeout,
        ),
        follow_redirects=False,
        # Proxy env vars would route traffic somewhere other than the IP we validated.
        trust_env=False,
        # Connections are keyed by IP, not hostname. Reusing a TLS connection
        # that was verified for host A to serve host B on the same IP would
        # skip B's certificate check, so every request gets a fresh connection.
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=100),
    )


def _decode(body: bytes, header_charset: str | None) -> str:
    charset = header_charset
    if not charset:
        match = _META_CHARSET_RE.search(body[:4096])
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, settings: Settings, resolver: Resolver = system_resolver):
        self.client = client
        self.settings = settings
        self.resolver = resolver

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a page under one overall deadline covering DNS, every redirect
        hop and the body read. Per-phase httpx timeouts alone don't bound the
        total: a server trickling one byte every 4.9s never trips a 5s read
        timeout."""
        try:
            async with asyncio.timeout(self.settings.total_timeout):
                return await self._fetch_with_redirects(url)
        except TimeoutError:
            raise UpstreamTimeoutError("Upstream request timed out") from None

    async def _fetch_with_redirects(self, url: str) -> FetchResult:
        current = url
        for _ in range(self.settings.max_redirects + 1):
            target = await validate_url(
                current,
                allowed_ports=self.settings.allowed_ports,
                resolver=self.resolver,
                max_length=self.settings.max_url_length,
            )
            result = await self._fetch_once(target)
            if isinstance(result, FetchResult):
                return result
            current = result
        raise UpstreamError("Too many redirects")

    async def _fetch_once(self, target: ValidatedTarget) -> FetchResult | str:
        """Returns the page, or the next URL to visit if this hop redirects."""
        last_error: Exception | None = None
        # All IPs were validated; try them in order so one dead address
        # doesn't fail a host that has healthy ones.
        for ip in target.ips:
            try:
                return await self._request(target, ip)
            except httpx.ConnectError as exc:
                last_error = exc
        raise UpstreamError("Could not connect to upstream") from last_error

    async def _request(self, target: ValidatedTarget, ip) -> FetchResult | str:
        # Pin the connection to the validated IP. The Host header and TLS SNI
        # keep the original hostname, and httpcore verifies the certificate
        # against the SNI name, so HTTPS stays fully verified.
        request_url = target.url.copy_with(host=str(ip), fragment=None)
        headers = {
            "Host": target.host_header,
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
        }
        extensions = {"sni_hostname": target.host} if target.url.scheme == "https" else {}
        try:
            async with self.client.stream("GET", request_url, headers=headers, extensions=extensions) as resp:
                if resp.status_code in REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise UpstreamError("Upstream sent a redirect without a location")
                    return str(target.url.join(location).copy_with(fragment=None))
                if resp.status_code >= 400:
                    raise UpstreamError(f"Upstream returned HTTP {resp.status_code}")
                if resp.status_code != 200:
                    raise UpstreamError(f"Unexpected upstream status {resp.status_code}")

                media_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if media_type not in ALLOWED_CONTENT_TYPES:
                    raise UnsupportedContentTypeError("Only HTML pages can be previewed")

                body = await self._read_capped(resp)
                return FetchResult(final_url=str(target.url.copy_with(fragment=None)), html=_decode(body, resp.charset_encoding))
        except httpx.TimeoutException:
            raise UpstreamTimeoutError("Upstream request timed out") from None
        except httpx.ConnectError:
            raise
        except (httpx.TransportError, httpx.DecodingError):
            raise UpstreamError("Upstream connection failed") from None

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Read at most max_body_bytes of *decompressed* body, then hang up.

        Counting decoded bytes (aiter_bytes) rather than wire bytes is what
        stops a gzip bomb. The page is truncated rather than rejected:
        metadata lives in <head>, so the first megabyte is almost always enough.
        """
        limit = self.settings.max_body_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            remaining = limit - size
            if len(chunk) >= remaining:
                chunks.append(chunk[:remaining])
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
