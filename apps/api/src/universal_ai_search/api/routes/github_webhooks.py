"""Bounded GitHub webhook ingress; durable handling is supplied by the application."""

from __future__ import annotations

from typing import Protocol, cast

from fastapi import APIRouter, Request, Response
from starlette.requests import ClientDisconnect

from universal_ai_search.api.problems import ProblemError
from universal_ai_search.connections.github_webhook import (
    MAX_GITHUB_WEBHOOK_BYTES,
    GitHubWebhookError,
    GitHubWebhookHint,
    verify_github_webhook,
)

router = APIRouter(prefix="/webhooks/github", tags=["webhooks"])


class GitHubWebhookUnavailable(Exception):
    """A configured store could not durably handle a delivery."""


class GitHubWebhookService(Protocol):
    async def record(self, hint: GitHubWebhookHint) -> None:
        """Atomically record eligible deliveries and schedule read-only work."""


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="GITHUB_WEBHOOK_UNAVAILABLE",
        title="GitHub webhook temporarily unavailable",
        retryable=True,
    )


def _invalid() -> ProblemError:
    return ProblemError(
        status=400,
        code="GITHUB_WEBHOOK_INVALID",
        title="Invalid GitHub webhook",
    )


def _too_large() -> ProblemError:
    return ProblemError(
        status=413,
        code="GITHUB_WEBHOOK_TOO_LARGE",
        title="GitHub webhook body is too large",
    )


def _required_header(request: Request, name: str, max_length: int) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > max_length:
        raise _invalid()
    return values[0]


async def _read_body(request: Request) -> bytes:
    lengths = request.headers.getlist("content-length")
    if lengths:
        length = lengths[0]
        if len(lengths) != 1 or not (length.isascii() and length.isdecimal()):
            raise _invalid()
        if len(length) > 19 or int(length) > MAX_GITHUB_WEBHOOK_BYTES:
            raise _too_large()

    buffer = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > MAX_GITHUB_WEBHOOK_BYTES - len(buffer):
                raise _too_large()
            buffer.extend(chunk)
    except ClientDisconnect:
        raise _invalid() from None
    if not buffer or (lengths and int(lengths[0]) != len(buffer)):
        raise _invalid()
    return bytes(buffer)


@router.post("", status_code=202, response_class=Response)
async def github_webhook(request: Request) -> Response:
    state = request.app.state
    secret = getattr(state, "github_webhook_secret", None)
    service = cast(
        GitHubWebhookService | None,
        getattr(state, "github_webhook_service", None),
    )
    if not isinstance(secret, bytes) or len(secret) < 32 or service is None:
        raise _unavailable()

    signature = _required_header(request, "x-hub-signature-256", 71)
    delivery = _required_header(request, "x-github-delivery", 36)
    event = _required_header(request, "x-github-event", 64)
    content_type = request.headers.getlist("content-type")
    encodings = request.headers.getlist("content-encoding")
    if (
        len(content_type) != 1
        or content_type[0].partition(";")[0].strip().lower() != "application/json"
        or encodings not in ([], ["identity"])
    ):
        raise ProblemError(
            status=415,
            code="GITHUB_WEBHOOK_MEDIA_UNSUPPORTED",
            title="GitHub webhook requires uncompressed JSON",
        )

    raw_body = await _read_body(request)
    try:
        hint = verify_github_webhook(
            raw_body=raw_body,
            secret=secret,
            signature=signature,
            delivery_id=delivery,
            event_name=event,
        )
    except GitHubWebhookError as error:
        if str(error) == "GITHUB_WEBHOOK_SIGNATURE_INVALID":
            raise ProblemError(
                status=401,
                code="GITHUB_WEBHOOK_AUTHENTICATION_FAILED",
                title="GitHub webhook authentication failed",
            ) from None
        raise _invalid() from None

    if hint is not None:
        try:
            await service.record(hint)
        except GitHubWebhookUnavailable:
            raise _unavailable() from None
    # Authenticated deliveries have the same status whether irrelevant, unknown,
    # unselected, or duplicate; storage must not return repository permissions.
    return Response(status_code=202, headers={"Cache-Control": "no-store"})
