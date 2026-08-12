"""RFC 9457-compatible, privacy-safe API problems."""

from __future__ import annotations

from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.exceptions import HTTPException

from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.models import ResponseModel

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"
_SAFE_HTTP_ERROR_HEADERS: Final = frozenset(
    {"allow", "retry-after", "www-authenticate"}
)


class ProblemIssue(ResponseModel):
    pointer: str
    code: str


class Problem(ResponseModel):
    type: str
    title: str
    status: int = Field(ge=400, le=599)
    code: str
    request_id: str
    retryable: bool
    detail: str | None = None
    errors: list[ProblemIssue] | None = None


class ProblemError(Exception):
    """An expected public problem raised by platform or feature code."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        retryable: bool = False,
        errors: list[ProblemIssue] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
        self.errors = errors
        self.headers = headers or {}


def _problem_type(code: str) -> str:
    return f"urn:uas:problem:{code.lower().replace('_', '-')}"


def _problem_response(request: Request, error: ProblemError) -> JSONResponse:
    problem = Problem(
        type=_problem_type(error.code),
        title=error.title,
        status=error.status,
        code=error.code,
        detail=error.detail,
        request_id=str(request_id_for(request)),
        retryable=error.retryable,
        errors=error.errors,
    )
    return JSONResponse(
        status_code=error.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        headers=error.headers,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def _json_pointer(location: tuple[str | int, ...]) -> str:
    parts = location[1:] if location and location[0] == "body" else location
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else "/"


def _http_title(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Request failed"


def install_problem_handlers(app: FastAPI) -> None:
    """Install the one public error format for all non-SSE responses."""

    @app.exception_handler(ProblemError)
    async def handle_problem(request: Request, error: ProblemError) -> JSONResponse:
        return _problem_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        malformed_json = any(item["type"] == "json_invalid" for item in error.errors())
        issues = [
            ProblemIssue(
                pointer=_json_pointer(tuple(item["loc"])),
                code="INVALID_VALUE",
            )
            for item in error.errors()
        ]
        public_error = ProblemError(
            status=400 if malformed_json else 422,
            code="MALFORMED_JSON" if malformed_json else "VALIDATION_ERROR",
            title="Malformed JSON" if malformed_json else "Request validation failed",
            detail=(
                "The request body could not be parsed."
                if malformed_json
                else "One or more request values are invalid."
            ),
            errors=issues,
        )
        return _problem_response(request, public_error)

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, error: HTTPException) -> JSONResponse:
        headers = {
            name: value
            for name, value in (error.headers or {}).items()
            if name.lower() in _SAFE_HTTP_ERROR_HEADERS
        }
        public_error = ProblemError(
            status=error.status_code,
            code=f"HTTP_{error.status_code}",
            title=_http_title(error.status_code),
            headers=headers,
        )
        return _problem_response(request, public_error)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        del error
        return _problem_response(
            request,
            ProblemError(
                status=500,
                code="INTERNAL_ERROR",
                title="Internal server error",
                detail="The request could not be completed.",
                retryable=False,
            ),
        )
