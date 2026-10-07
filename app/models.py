from datetime import datetime

from pydantic import BaseModel


class Metadata(BaseModel):
    title: str | None = None
    description: str | None = None
    image: str | None = None
    site_name: str | None = None
    favicon: str | None = None


class PreviewResponse(Metadata):
    url: str
    fetched_at: datetime
    cached: bool


class ErrorResponse(BaseModel):
    error: str
    detail: str


class HealthResponse(BaseModel):
    status: str = "ok"
