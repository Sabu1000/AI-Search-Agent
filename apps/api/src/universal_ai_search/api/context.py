"""Request correlation context and middleware."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID, uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[UUID | None] = ContextVar("request_id", default=None)


def normalize_request_id(value: str | None) -> UUID:
    """Return a canonical client UUID or a new server-generated UUID."""

    if value is not None:
        try:
            return UUID(value)
        except (ValueError, AttributeError):
            pass
    return uuid4()


def current_request_id() -> UUID:
    """Return the request ID for the active request context."""

    value = _request_id.get()
    return value if value is not None else uuid4()


def request_id_for(request: Request) -> UUID:
    """Return the ID attached by middleware, with a safe fallback."""

    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else current_request_id()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a canonical correlation ID and return it on every response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = str(request_id)
        return response
