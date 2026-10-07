class LinkpeekError(Exception):
    """Base for errors that map to a client-facing HTTP status.

    Messages are shown to the caller verbatim, so they must never include
    resolved IPs, internal hostnames, or upstream exception text.
    """

    status_code: int = 502
    code: str = "upstream_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BlockedURLError(LinkpeekError):
    status_code = 400
    code = "blocked_url"


class UpstreamTimeoutError(LinkpeekError):
    status_code = 504
    code = "upstream_timeout"


class UpstreamError(LinkpeekError):
    status_code = 502
    code = "upstream_error"


class UnsupportedContentTypeError(LinkpeekError):
    status_code = 415
    code = "unsupported_content_type"


class RateLimitedError(Exception):
    """Kept outside LinkpeekError so it is never negatively cached."""

    def __init__(self, retry_after: str):
        self.retry_after = retry_after


ERRORS_BY_CODE: dict[str, type[LinkpeekError]] = {
    cls.code: cls
    for cls in (BlockedURLError, UpstreamTimeoutError, UpstreamError, UnsupportedContentTypeError)
}
