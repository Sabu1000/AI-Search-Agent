"""Opt-in SigV4 PUT signer with a fail-closed, verified staging-policy gate."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urlsplit

from .manifests import MAX_FILE_BYTES
from .uploads import UPLOAD_GRANT_TTL, UploadGrant, UploadUnavailable

_PRIVATE_KEY_PATH = re.compile(
    r"device-inbox/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_SIGNED_HEADERS = "content-length;content-type;host;if-none-match;x-amz-checksum-sha256"
_REQUIRED_QUERY = {
    "X-Amz-Algorithm",
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-SignedHeaders",
    "X-Amz-Signature",
}


class S3PresignClient(Protocol):
    """Compatible with an explicitly configured AWS SDK S3 client."""

    def generate_presigned_url(
        self,
        ClientMethod: str,
        *,
        Params: dict[str, str | int],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str: ...


class StagingPolicyVerifier(Protocol):
    """Trusted, out-of-band verification for this exact signing identity.

    A real implementation must report safe only after establishing private
    staging, no overwrite except verified conditional PUT, no delete or
    lifecycle/delete-marker gap while grants are live, and tightly scoped
    signer credentials. Raw HTTP probes, an SDK URL, or a caller-supplied
    boolean do not prove these conditions. No implementation is enabled here.
    """

    def verified(self, *, origin: str, bucket: str, access_key_id: str) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class SignedUpload:
    url: str
    expires_at: datetime


class VerifiedS3UploadSigner:
    def __init__(
        self,
        client: S3PresignClient,
        staging: StagingPolicyVerifier,
        *,
        origin: str,
        bucket: str,
        access_key_id: str,
        region: str,
        addressing_style: Literal["path", "virtual"] = "path",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        try:
            endpoint = urlsplit(origin)
            if (
                not origin.isascii()
                or endpoint.scheme != "https"
                or not endpoint.hostname
                or not re.fullmatch(r"[a-z0-9.-]{1,253}", endpoint.hostname)
                or endpoint.username is not None
                or (endpoint.port is not None and not 1 <= endpoint.port <= 65535)
                or origin != f"https://{endpoint.netloc}"
                or _BUCKET.fullmatch(bucket) is None
                or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", access_key_id)
                or not re.fullmatch(r"[a-z0-9-]{3,40}", region)
                or addressing_style not in {"path", "virtual"}
                or (
                    addressing_style == "virtual"
                    and not endpoint.hostname.startswith(bucket + ".")
                )
            ):
                raise ValueError
        except ValueError:
            raise ValueError("invalid device upload signer configuration") from None
        self._client = client
        self._staging = staging
        self._origin = origin
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._region = region
        self._prefix = f"/{bucket}/" if addressing_style == "path" else "/"
        self._now = now

    def require_ready(self) -> None:
        # Run before any reservation. Deployment must inject a verifier; an
        # ordinary object storage endpoint or default admin keys are not proof.
        try:
            if (
                self._staging.verified(
                    origin=self._origin,
                    bucket=self._bucket,
                    access_key_id=self._access_key_id,
                )
                is True
            ):
                return
        except Exception:
            raise UploadUnavailable from None
        raise UploadUnavailable

    def sign(self, grant: UploadGrant) -> SignedUpload:
        self.require_ready()
        try:
            now = self._now()
            if (
                now.utcoffset() is None
                or grant.issued_at.utcoffset() is None
                or grant.expires_at.utcoffset() is None
                or grant.expires_at - grant.issued_at != UPLOAD_GRANT_TTL
                or grant.issued_at > now + timedelta(seconds=2)
                or _PRIVATE_KEY_PATH.fullmatch(grant.object_key) is None
                or not 0 <= grant.size_bytes <= MAX_FILE_BYTES
            ):
                raise ValueError
            seconds = min(600, math.floor((grant.expires_at - now).total_seconds()))
            if seconds < 1:
                raise ValueError
            signed = self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": grant.object_key,
                    "ContentLength": grant.size_bytes,
                    "ContentType": grant.mime_type,
                    "IfNoneMatch": "*",
                    "ChecksumSHA256": grant.required_headers()["x-amz-checksum-sha256"],
                },
                ExpiresIn=seconds,
                HttpMethod="PUT",
            )
            return self._verify_signed_url(signed, grant, now, seconds)
        except Exception:
            # Provider exceptions may include credential, full URL, or key.
            raise UploadUnavailable from None

    def _verify_signed_url(
        self, url: str, grant: UploadGrant, now: datetime, seconds: int
    ) -> SignedUpload:
        if not isinstance(url, str) or not url.isascii() or len(url) > 8192:
            raise ValueError
        target = urlsplit(url)
        if (
            f"{target.scheme}://{target.netloc}" != self._origin
            or target.path != self._prefix + grant.object_key
            or "#" in url
            or any(char.isspace() for char in url)
        ):
            raise ValueError
        fields = parse_qsl(
            target.query, keep_blank_values=False, strict_parsing=True, max_num_fields=7
        )
        query = dict(fields)
        timestamp_text = query.get("X-Amz-Date", "")
        if (
            len(query) != len(fields)
            or query.keys()
            not in (_REQUIRED_QUERY, _REQUIRED_QUERY | {"X-Amz-Security-Token"})
            or query.get("X-Amz-Algorithm") != "AWS4-HMAC-SHA256"
            or query.get("X-Amz-SignedHeaders") != _SIGNED_HEADERS
            or query.get("X-Amz-Expires") != str(seconds)
            or not re.fullmatch(r"[0-9a-f]{64}", query.get("X-Amz-Signature", ""))
            or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", timestamp_text)
            or query.get("X-Amz-Credential")
            != (
                f"{self._access_key_id}/{timestamp_text[:8]}/{self._region}/s3/aws4_request"
            )
        ):
            raise ValueError
        timestamp = datetime.strptime(timestamp_text, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC
        )
        expires = timestamp + timedelta(seconds=seconds)
        if (
            abs(timestamp - now) > timedelta(seconds=2)
            or expires > grant.expires_at
            or expires <= self._now()
        ):
            raise ValueError
        return SignedUpload(url, expires)
