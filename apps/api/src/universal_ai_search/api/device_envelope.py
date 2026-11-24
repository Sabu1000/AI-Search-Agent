"""Transport checks common to exact-byte, device-signed requests."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from starlette.requests import ClientDisconnect

from universal_ai_search.devices.security import DeviceProofHeaders

_REQUIRED = {
    b"authorization": 50,
    b"x-device-timestamp": 20,
    b"x-device-nonce": 43,
    b"content-digest": 54,
    b"x-device-signature": 86,
    b"x-request-id": 36,
}
_FORBIDDEN = {
    b"cookie",
    b"origin",
    b"content-encoding",
    b"x-workspace-id",
    b"transfer-encoding",
    b"x-http-method-override",
    b"x-original-url",
    b"x-rewrite-url",
}


class DeviceEnvelopeError(Exception):
    """Ambiguous or unauthenticated transport."""


class DeviceBodyTooLarge(DeviceEnvelopeError):
    """An original or streamed body exceeded the operation limit."""


@dataclass(frozen=True, slots=True, repr=False)
class DeviceEnvelope:
    target: bytes
    credential: str
    headers: DeviceProofHeaders
    body: bytes


async def read_device_envelope(
    request: Request, *, max_body_bytes: int, allow_empty: bool = False
) -> DeviceEnvelope:
    # Trusted ingress must preserve unnormalized original request path and
    # headers and obtain TLS state only from trusted peer metadata.
    target = request.scope.get("raw_path")
    if (
        not isinstance(target, bytes)
        or request.scope.get("scheme") != "https"
        or request.scope.get("query_string") != b""
        or request.method != "POST"
    ):
        raise DeviceEnvelopeError
    signed: dict[bytes, str] = {}
    size: int | None = None
    content_type: bytes | None = None
    for raw_name, value in request.scope["headers"]:
        name = raw_name.lower()
        if name in _FORBIDDEN or name.startswith(b"sec-fetch-"):
            raise DeviceEnvelopeError
        if name == b"content-length":
            if size is not None or not value.isdigit() or len(value) > 8:
                raise DeviceEnvelopeError
            size = int(value)
            if str(size).encode() != value:
                raise DeviceEnvelopeError
            if size > max_body_bytes:
                raise DeviceBodyTooLarge
        if name == b"content-type":
            if (
                max_body_bytes == 0
                or content_type is not None
                or value.lower()
                not in (b"application/json", b"application/json; charset=utf-8")
            ):
                raise DeviceEnvelopeError
            content_type = value
        if name in _REQUIRED:
            if (
                name in signed
                or not 1 <= len(value) <= _REQUIRED[name]
                or not value.isascii()
            ):
                raise DeviceEnvelopeError
            signed[name] = value.decode("ascii")
    if signed.keys() != _REQUIRED.keys() or (
        max_body_bytes > 0 and content_type is None and not allow_empty
    ):
        raise DeviceEnvelopeError
    scheme, _, credential = signed[b"authorization"].partition(" ")
    if scheme.lower() != "device":
        raise DeviceEnvelopeError
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_body_bytes:
                raise DeviceBodyTooLarge
            body.extend(chunk)
    except ClientDisconnect:
        raise DeviceEnvelopeError from None
    if (size is not None and size != len(body)) or (
        allow_empty and bool(body) != bool(content_type)
    ):
        raise DeviceEnvelopeError
    return DeviceEnvelope(
        target,
        credential,
        DeviceProofHeaders(
            timestamp=signed[b"x-device-timestamp"],
            nonce=signed[b"x-device-nonce"],
            content_digest=signed[b"content-digest"],
            signature=signed[b"x-device-signature"],
            request_id=signed[b"x-request-id"],
        ),
        bytes(body),
    )
