from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LINKPEEK_", env_file=".env", extra="ignore")

    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    total_timeout: float = 8.0
    max_body_bytes: int = 1_000_000
    max_redirects: int = 3
    max_url_length: int = 2048
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443, 8080, 8443])
    user_agent: str = "linkpeek/1.0 (+https://github.com/samiktare/linkpeek)"

    cache_ttl: float = 3600.0
    negative_cache_ttl: float = 60.0
    cache_max_size: int = 1000

    rate_limit_requests: int = 30
    rate_limit_window: float = 60.0
    # Only enable behind a proxy that appends the real client IP (Render, Fly);
    # otherwise any client can spoof X-Forwarded-For and dodge the rate limit.
    trust_x_forwarded_for: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
