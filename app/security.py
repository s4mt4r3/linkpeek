"""SSRF defenses: URL validation, DNS resolution, and IP classification.

Every URL the fetcher touches, including each redirect hop, goes through
`validate_url`. It returns the IPs it validated, and the fetcher connects to
one of those IPs directly, so a second DNS lookup can't swap in a private
address between check and use (DNS rebinding).
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import httpx

from app.errors import BlockedURLError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Awaitable[list[str]]]

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")

# Ranges that `ipaddress` flags as non-global anyway; listed explicitly so the
# intent is reviewable and survives stdlib classification changes.
EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT, often reachable inside clouds
        "169.254.0.0/16",  # link-local, includes 169.254.169.254 metadata
        "fd00:ec2::254/128",  # AWS IPv6 metadata endpoint
        "64:ff9b::/96",  # NAT64: embeds an IPv4 address, checked separately
    )
)

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*$")

GENERIC_BLOCK_MESSAGE = "URL is not allowed or could not be resolved"


@dataclass(frozen=True)
class ValidatedTarget:
    url: httpx.URL
    host: str
    port: int
    ips: tuple[IPAddress, ...]

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == DEFAULT_PORTS[self.url.scheme]:
            return host
        return f"{host}:{self.port}"


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    # Deprecated IPv4-compatible form (::a.b.c.d).
    if int(ip) >> 32 == 0 and int(ip) > 1:
        return ipaddress.IPv4Address(int(ip))
    return None


def is_ip_allowed(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return is_ip_allowed(embedded)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    return not any(ip in net for net in EXTRA_BLOCKED_NETWORKS if net.version == ip.version)


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _parse_ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def _is_legacy_numeric_ipv4(host: str) -> bool:
    """True for forms like 2130706433, 0x7f000001, 0177.0.0.1 or 127.1.

    `ipaddress` rejects these, but getaddrinfo/inet_aton accept them and
    turn them into real addresses, so they get past naive string checks.
    """
    try:
        socket.inet_aton(host)
    except OSError:
        return False
    return True


def _check_hostname(host: str) -> None:
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise BlockedURLError(GENERIC_BLOCK_MESSAGE)
    if _is_legacy_numeric_ipv4(host):
        raise BlockedURLError(GENERIC_BLOCK_MESSAGE)
    if not _HOSTNAME_RE.match(host) or "." not in host:
        raise BlockedURLError(GENERIC_BLOCK_MESSAGE)


def parse_url(raw: str, *, allowed_ports: Iterable[int], max_length: int = 2048) -> tuple[httpx.URL, str, int]:
    """Static checks that need no network. Returns (url, ascii_host, port).

    Errors here describe the problem precisely because they reveal nothing
    the caller didn't already send us.
    """
    if not raw or len(raw) > max_length:
        raise BlockedURLError("URL is missing or too long")
    if any(c in raw for c in "\r\n\t\x00") or raw != raw.strip():
        raise BlockedURLError("URL contains invalid characters")
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError):
        raise BlockedURLError("URL is malformed") from None

    if url.scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError("Only http and https URLs are allowed")
    if url.userinfo:
        raise BlockedURLError("URLs with credentials are not allowed")

    try:
        host = url.raw_host.decode("ascii").lower().rstrip(".")
    except UnicodeDecodeError:
        raise BlockedURLError("URL is malformed") from None
    if not host:
        raise BlockedURLError("URL must include a host")

    port = url.port or DEFAULT_PORTS[url.scheme]
    if port not in set(allowed_ports):
        raise BlockedURLError("Port is not allowed")
    return url, host, port


async def validate_url(
    raw: str,
    *,
    allowed_ports: Iterable[int],
    resolver: Resolver = system_resolver,
    max_length: int = 2048,
) -> ValidatedTarget:
    """Full validation: static checks, then DNS, then every resolved IP.

    Rejects if ANY resolved address is disallowed. Allowing the request when
    only some are bad would let an attacker publish one public and one
    private A record and win whenever the client picks the private one.
    """
    url, host, port = parse_url(raw, allowed_ports=allowed_ports, max_length=max_length)

    literal = _parse_ip(host)
    if literal is not None:
        ips = [literal]
    else:
        _check_hostname(host)
        try:
            resolved = await resolver(host)
        except (OSError, UnicodeError):
            raise BlockedURLError(GENERIC_BLOCK_MESSAGE) from None
        ips = [ip for ip in (_parse_ip(r) for r in resolved) if ip is not None]
        if not ips or len(ips) != len(resolved):
            raise BlockedURLError(GENERIC_BLOCK_MESSAGE)

    if not all(is_ip_allowed(ip) for ip in ips):
        raise BlockedURLError(GENERIC_BLOCK_MESSAGE)

    unique = tuple(dict.fromkeys(ips))
    return ValidatedTarget(url=url, host=host, port=port, ips=unique)
