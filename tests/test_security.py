import ipaddress

import pytest

from app.errors import BlockedURLError
from app.security import GENERIC_BLOCK_MESSAGE, is_ip_allowed, validate_url
from tests.conftest import PUBLIC_IP, FakeResolver

PORTS = [80, 443, 8080, 8443]


async def validate(url: str, resolver: FakeResolver | None = None):
    return await validate_url(url, allowed_ports=PORTS, resolver=resolver or FakeResolver())


@pytest.mark.parametrize(
    "url",
    [
        # loopback
        "http://127.0.0.1/",
        "http://127.255.255.254/",
        "http://[::1]/",
        # RFC 1918
        "http://10.0.0.1/",
        "http://10.255.255.255/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.0.1/",
        "http://192.168.1.1:8080/",
        # link-local / cloud metadata
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.0.1/",
        "http://[fe80::1]/",
        "http://[fd00:ec2::254]/",
        # unspecified, CGNAT, multicast, reserved, broadcast
        "http://0.0.0.0/",
        "http://[::]/",
        "http://100.64.0.1/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://255.255.255.255/",
        # unique local IPv6
        "http://[fc00::1]/",
        "http://[fd12:3456::1]/",
        # IPv4 smuggled inside IPv6
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[::127.0.0.1]/",
        # legacy numeric IPv4 forms that getaddrinfo would happily resolve
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0x7f.0x0.0x0.0x1/",
        "http://017700000001/",
        "http://127.1/",
        "http://0/",
        "http://3232235521/",  # 192.168.0.1
        "http://2852039166/",  # 169.254.169.254
    ],
)
async def test_internal_addresses_blocked(url):
    # A resolver that says every name is public, so only the IP-literal and
    # numeric-form checks themselves can cause the rejection.
    resolver = FakeResolver(default=[PUBLIC_IP])
    with pytest.raises(BlockedURLError) as exc:
        await validate(url, resolver)
    assert exc.value.message == GENERIC_BLOCK_MESSAGE
    assert resolver.calls == []


async def test_octal_dotted_form_blocked():
    # httpx itself refuses this form; either way it must never be fetched.
    with pytest.raises(BlockedURLError):
        await validate("http://0177.0.0.1/")


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "foo.localhost",
        "printer.local",
        "metadata.google.internal",
        "db.internal",
        "intranet",  # single-label names go through search domains
    ],
)
async def test_internal_hostnames_blocked_without_dns(host):
    resolver = FakeResolver({host.lower().rstrip("."): [PUBLIC_IP]})
    with pytest.raises(BlockedURLError):
        await validate(f"http://{host}/", resolver)
    assert resolver.calls == []


@pytest.mark.parametrize(
    "url, message",
    [
        ("ftp://example.com/", "Only http and https URLs are allowed"),
        ("file:///etc/passwd", "Only http and https URLs are allowed"),
        ("gopher://example.com/", "Only http and https URLs are allowed"),
        ("javascript:alert(1)", "Only http and https URLs are allowed"),
        ("dict://example.com:11211/", "Only http and https URLs are allowed"),
        ("http://user:pass@example.com/", "URLs with credentials are not allowed"),
        ("http://user@example.com/", "URLs with credentials are not allowed"),
        ("http://example.com:22/", "Port is not allowed"),
        ("http://example.com:6379/", "Port is not allowed"),
        ("https://example.com:99999/", "Port is not allowed"),
        ("http:///nohost", "URL must include a host"),
        ("", "URL is missing or too long"),
        ("http://example.com/" + "a" * 3000, "URL is missing or too long"),
        ("http://example.com/\r\nHost: evil", "URL contains invalid characters"),
    ],
)
async def test_static_rejections(url, message):
    with pytest.raises(BlockedURLError) as exc:
        await validate(url)
    assert exc.value.message == message


async def test_dns_resolving_to_private_ip_blocked():
    resolver = FakeResolver({"evil.example": ["10.0.0.5"]})
    with pytest.raises(BlockedURLError) as exc:
        await validate("http://evil.example/", resolver)
    assert exc.value.message == GENERIC_BLOCK_MESSAGE
    assert "10.0.0.5" not in exc.value.message


async def test_any_private_record_blocks_whole_host():
    resolver = FakeResolver({"mixed.example": [PUBLIC_IP, "127.0.0.1"]})
    with pytest.raises(BlockedURLError):
        await validate("http://mixed.example/", resolver)


async def test_dns_returning_mapped_ipv6_blocked():
    resolver = FakeResolver({"v6.example": ["::ffff:192.168.1.1"]})
    with pytest.raises(BlockedURLError):
        await validate("http://v6.example/", resolver)


async def test_unresolvable_host_uses_generic_message():
    with pytest.raises(BlockedURLError) as exc:
        await validate("http://does-not-exist.example/")
    assert exc.value.message == GENERIC_BLOCK_MESSAGE


@pytest.mark.parametrize(
    "url, port",
    [
        ("http://example.com/", 80),
        ("https://example.com/", 443),
        ("http://example.com:8080/", 8080),
        ("https://EXAMPLE.com:8443/path?q=1", 8443),
        ("https://example.com./", 443),
    ],
)
async def test_public_urls_allowed(url, port):
    target = await validate(url)
    assert target.host == "example.com"
    assert target.port == port
    assert target.ips == (ipaddress.ip_address(PUBLIC_IP),)


async def test_public_ip_literal_allowed_without_dns():
    resolver = FakeResolver()
    target = await validate(f"https://{PUBLIC_IP}/", resolver)
    assert resolver.calls == []
    assert str(target.ips[0]) == PUBLIC_IP


async def test_public_ipv6_allowed():
    resolver = FakeResolver({"v6ok.example": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"]})
    target = await validate("http://v6ok.example/", resolver)
    assert target.ips[0].version == 6


def test_host_header_includes_non_default_port():
    from app.security import ValidatedTarget
    import httpx

    t = ValidatedTarget(httpx.URL("http://example.com:8080/"), "example.com", 8080, ())
    assert t.host_header == "example.com:8080"
    t = ValidatedTarget(httpx.URL("https://example.com/"), "example.com", 443, ())
    assert t.host_header == "example.com"


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"])
def test_is_ip_allowed_public(ip):
    assert is_ip_allowed(ipaddress.ip_address(ip))
