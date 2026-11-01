"""Platform errors. Every interface maps them the same way (HTTP status, CLI exit code, SDK type).

The REST API renders them as RFC 9457 problem details:
`{"type": "urn:hollis:problem:<code>", "title", "status", "detail", "code"}`.
"""


class PlatformError(Exception):
    code = "error"
    status = 500
    exit_code = 1
    title = "Unexpected error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvalidRequest(PlatformError):
    code, status, exit_code, title = "invalid_request", 422, 2, "Invalid request"


class Unauthenticated(PlatformError):
    code, status, exit_code, title = "unauthenticated", 401, 3, "Authentication required"


class Forbidden(PlatformError):
    code, status, exit_code, title = "forbidden", 403, 3, "Not allowed"


class NotFound(PlatformError):
    code, status, exit_code, title = "not_found", 404, 4, "Not found"


class Conflict(PlatformError):
    code, status, exit_code, title = "conflict", 409, 5, "Conflict"


class Unavailable(PlatformError):
    code, status, exit_code, title = "unavailable", 503, 6, "Unavailable"


ERRORS: dict[str, type[PlatformError]] = {
    cls.code: cls
    for cls in (
        PlatformError,
        InvalidRequest,
        Unauthenticated,
        Forbidden,
        NotFound,
        Conflict,
        Unavailable,
    )
}
