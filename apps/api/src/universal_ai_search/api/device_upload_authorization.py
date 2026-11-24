"""Unwired upload authorization entry point; there is no object-store signer.

No route may expose a reservation or reserve storage merely to return a dummy
success. The eventual handler must confirm a verified signer is available
before calling this boundary, and then issue only a conditionally signed PUT.
"""

from fastapi import Request

from universal_ai_search.api.device_envelope import read_device_envelope
from universal_ai_search.api.models import CanonicalUUID
from universal_ai_search.devices.uploads import (
    MAX_UPLOAD_SIGN_BODY_BYTES,
    DeviceUploadAuthorization,
    UploadGrant,
)


async def authorize_device_upload(
    device_id: CanonicalUUID, request: Request, service: DeviceUploadAuthorization
) -> UploadGrant:
    envelope = await read_device_envelope(
        request, max_body_bytes=MAX_UPLOAD_SIGN_BODY_BYTES
    )
    return await service.reserve(
        device_id=device_id,
        credential=envelope.credential,
        raw_target=envelope.target,
        exact_body=envelope.body,
        headers=envelope.headers,
    )
