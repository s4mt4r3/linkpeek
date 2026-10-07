import socket

import pytest

PUBLIC_IP = "93.184.215.14"


class FakeResolver:
    """Deterministic DNS: hostnames map to fixed answers, unknown names fail.

    Tracks calls so tests can assert how many lookups happened.
    """

    def __init__(self, records: dict[str, list[str]] | None = None, default: list[str] | None = None):
        self.records = {"example.com": [PUBLIC_IP], **(records or {})}
        self.default = default
        self.calls: list[str] = []

    async def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        if host not in self.records:
            if self.default is not None:
                return self.default
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return self.records[host]


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """Fail loudly if any code path reaches the real system resolver."""

    def blocked(*args, **kwargs):
        raise AssertionError("real DNS lookup attempted in tests")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
