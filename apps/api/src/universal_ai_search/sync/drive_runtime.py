"""One-provider-page-at-a-time Google Drive folder traversal."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import random
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4, uuid5

from cryptography.exceptions import InvalidTag
from pydantic import SecretStr, ValidationError
from uas_connector_sdk import Credentials, NormalizedDocument
from uas_connector_sdk.errors import (
    AuthenticationError,
    ConnectorError,
    MalformedItemError,
    RateLimitError,
)

from universal_ai_search.connections.crypto import (
    EncryptedEnvelope,
    LocalEnvelopeEncryption,
    envelope_context,
)
from universal_ai_search.connections.drive import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    DriveDownloadTooLargeError,
    DriveItem,
    HttpDriveClient,
    normalize_drive_item,
)
from universal_ai_search.connections.drive_pdf import (
    DRIVE_PDF_MIME_TYPE,
    PdfExtraction,
    extract_pdf,
    normalize_drive_pdf,
)
from universal_ai_search.connections.google import DRIVE_READONLY_SCOPE
from universal_ai_search.indexing.repository import IndexRepository

from .drive_repository import DriveSyncRepository, ScheduledDriveJob
from .repository import ClaimedSyncJob, GoogleSyncInput


class DriveCredentialError(Exception):
    pass


class DriveSyncPayloadError(Exception):
    pass


class DriveSyncRuntime:
    def __init__(
        self,
        *,
        repository: DriveSyncRepository,
        index_repository: IndexRepository,
        client: HttpDriveClient,
        encryption: LocalEnvelopeEncryption,
        enabled: bool = True,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._repository = repository
        self._index_repository = index_repository
        self._client = client
        self._encryption = encryption
        self._enabled = enabled
        self._random_value = random_value

    def run_once(self, worker_id: str) -> bool:
        if not self._enabled:
            return False
        claim = self._repository.claim(worker_id)
        if claim is None:
            return False
        try:
            sync_input = self._repository.load(claim)
            if sync_input.payload.get("mode") != "full":
                raise DriveSyncPayloadError
            credentials = self._credentials(claim, sync_input)
            fresh = asyncio.run(self._client.ensure_fresh(credentials))
            if fresh != credentials:
                self._repository.save_credentials(
                    claim, self._encrypted_credentials(claim, fresh)
                )
            self._run_page(
                claim,
                sync_input,
                fresh.access_token.get_secret_value(),
            )
        except ConnectorError as error:
            self._repository.fail(
                claim,
                error_code=error.code.upper(),
                retryable=error.retryable,
                reauthorization_required=isinstance(error, AuthenticationError),
                retry_delay_seconds=(
                    self._retry_delay(claim, error) if error.retryable else None
                ),
            )
        except DriveCredentialError:
            self._repository.fail(
                claim,
                error_code="DRIVE_CREDENTIAL_INVALID",
                retryable=False,
                retry_delay_seconds=None,
            )
        except DriveSyncPayloadError:
            self._repository.fail(
                claim,
                error_code="DRIVE_SYNC_PAYLOAD_INVALID",
                retryable=False,
                retry_delay_seconds=None,
            )
        except Exception:
            self._repository.fail(
                claim,
                error_code="DRIVE_SYNC_INTERNAL_ERROR",
                retryable=True,
                retry_delay_seconds=self._retry_delay(claim),
            )
            raise
        return True

    def _run_page(
        self, claim: ClaimedSyncJob, sync_input: GoogleSyncInput, access_token: str
    ) -> None:
        progress = self._progress(claim, sync_input.payload)
        if progress is None:
            sync_run_id = uuid4()
            folder_id, logical_path, drive_id = self._root(sync_input.payload)
            page_token = None
        else:
            sync_run_id = self._uuid_value(progress, "sync_run_id")
            folder_id = self._string_value(progress, "folder_id")
            logical_path = self._path_value(progress)
            drive_id = self._optional_string(progress, "drive_id")
            page_token = self._optional_string(progress, "page_token")
        page = asyncio.run(
            self._client.children_page(
                access_token=access_token,
                folder_id=folder_id,
                page_token=page_token,
                drive_id=drive_id,
            )
        )
        scheduled: list[ScheduledDriveJob] = []
        for item in page.items:
            if folder_id not in item.parent_ids:
                raise MalformedItemError("Drive child escaped its selected parent")
            if item.is_folder:
                scheduled.append(
                    self._folder_job(
                        claim,
                        sync_run_id=sync_run_id,
                        item=item,
                        logical_path=logical_path,
                        drive_id=drive_id,
                    )
                )
            else:
                self._index_repository.enqueue(
                    claim.workspace_id,
                    claim.connection_id,
                    self._document(item, logical_path, access_token),
                )
        if page.next_page_token:
            scheduled.append(
                self._page_job(
                    claim,
                    sync_run_id=sync_run_id,
                    folder_id=folder_id,
                    logical_path=logical_path,
                    drive_id=drive_id,
                    page_token=page.next_page_token,
                )
            )
        self._repository.finish_page(
            claim, sync_run_id=sync_run_id, scheduled_jobs=tuple(scheduled)
        )

    def _document(
        self, item: DriveItem, logical_path: tuple[str, ...], access_token: str
    ) -> NormalizedDocument:
        if item.mime_type != DRIVE_PDF_MIME_TYPE:
            return normalize_drive_item(item, logical_path=logical_path)
        if item.size is not None and item.size > MAX_DRIVE_DOWNLOAD_BYTES:
            extraction = PdfExtraction("too_large")
        else:
            try:
                data = asyncio.run(
                    self._client.download_file(
                        access_token=access_token, file_id=item.id
                    )
                )
                extraction = extract_pdf(data)
            except DriveDownloadTooLargeError:
                extraction = PdfExtraction("too_large")
        return normalize_drive_pdf(item, extraction, logical_path=logical_path)

    def _folder_job(
        self,
        claim: ClaimedSyncJob,
        *,
        sync_run_id: UUID,
        item: DriveItem,
        logical_path: tuple[str, ...],
        drive_id: str | None,
    ) -> ScheduledDriveJob:
        child_path = (*logical_path, item.name)
        if len(child_path) > 100 or sum(len(part) for part in child_path) > 8_000:
            raise MalformedItemError("Drive folder path exceeds traversal limits")
        job_id = uuid5(sync_run_id, f"drive-folder:{item.id}")
        return ScheduledDriveJob(
            job_id=job_id,
            idempotency_key=f"drive-folder:{sync_run_id}:{item.id}",
            encrypted_progress=self._encrypted_progress(
                claim,
                target_job_id=job_id,
                progress=self._progress_values(
                    sync_run_id=sync_run_id,
                    folder_id=item.id,
                    logical_path=child_path,
                    drive_id=drive_id or item.drive_id,
                ),
            ),
        )

    def _page_job(
        self,
        claim: ClaimedSyncJob,
        *,
        sync_run_id: UUID,
        folder_id: str,
        logical_path: tuple[str, ...],
        drive_id: str | None,
        page_token: str,
    ) -> ScheduledDriveJob:
        fingerprint = hashlib.sha256(
            f"{claim.job_id}:{page_token}".encode()
        ).hexdigest()
        job_id = uuid5(claim.job_id, f"drive-page:{fingerprint}")
        return ScheduledDriveJob(
            job_id=job_id,
            idempotency_key=f"drive-page:{claim.connection_id}:{fingerprint}",
            encrypted_progress=self._encrypted_progress(
                claim,
                target_job_id=job_id,
                progress=self._progress_values(
                    sync_run_id=sync_run_id,
                    folder_id=folder_id,
                    logical_path=logical_path,
                    drive_id=drive_id,
                    page_token=page_token,
                ),
            ),
        )

    @staticmethod
    def _root(payload: dict[str, object]) -> tuple[str, tuple[str, ...], str | None]:
        root = payload.get("drive_root")
        if root is None:
            return "root", ("My Drive",), None
        if not isinstance(root, dict):
            raise DriveSyncPayloadError
        folder_id = root.get("id")
        name = root.get("name")
        drive_id = root.get("drive_id")
        if (
            not isinstance(folder_id, str)
            or not folder_id
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 2_000
            or (
                drive_id is not None and (not isinstance(drive_id, str) or not drive_id)
            )
        ):
            raise DriveSyncPayloadError
        return folder_id, (name.strip(),), drive_id

    @staticmethod
    def _progress_values(
        *,
        sync_run_id: UUID,
        folder_id: str,
        logical_path: tuple[str, ...],
        drive_id: str | None,
        page_token: str | None = None,
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "folder_id": folder_id,
            "logical_path": list(logical_path),
            "sync_run_id": str(sync_run_id),
        }
        if drive_id is not None:
            values["drive_id"] = drive_id
        if page_token is not None:
            values["page_token"] = page_token
        return values

    def _progress(
        self, claim: ClaimedSyncJob, payload: dict[str, object]
    ) -> dict[str, object] | None:
        encoded = payload.get("drive_progress")
        if encoded is None:
            return None
        if not isinstance(encoded, dict):
            raise DriveSyncPayloadError
        try:
            envelope = EncryptedEnvelope(
                ciphertext=base64.b64decode(encoded["ciphertext"], validate=True),
                encrypted_data_key=base64.b64decode(
                    encoded["encrypted_data_key"], validate=True
                ),
                key_version=int(encoded["key_version"]),
            )
            context = envelope_context(
                provider="google",
                workspace_id=str(claim.workspace_id),
                record_id=str(claim.job_id),
                purpose="drive-sync-progress",
            )
            value = json.loads(self._encryption.decrypt(envelope, context=context))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (
            InvalidTag,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as error:
            raise DriveSyncPayloadError from error

    def _encrypted_progress(
        self,
        claim: ClaimedSyncJob,
        *,
        target_job_id: UUID,
        progress: dict[str, object],
    ) -> EncryptedEnvelope:
        context = envelope_context(
            provider="google",
            workspace_id=str(claim.workspace_id),
            record_id=str(target_job_id),
            purpose="drive-sync-progress",
        )
        return self._encryption.encrypt(
            json.dumps(progress, sort_keys=True, separators=(",", ":")).encode(),
            context=context,
        )

    @staticmethod
    def _string_value(progress: dict[str, object], key: str) -> str:
        value = progress.get(key)
        if not isinstance(value, str) or not value:
            raise DriveSyncPayloadError
        return value

    @classmethod
    def _optional_string(cls, progress: dict[str, object], key: str) -> str | None:
        if key not in progress:
            return None
        return cls._string_value(progress, key)

    @staticmethod
    def _uuid_value(progress: dict[str, object], key: str) -> UUID:
        value = progress.get(key)
        if not isinstance(value, str):
            raise DriveSyncPayloadError
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise DriveSyncPayloadError from error
        if str(parsed) != value:
            raise DriveSyncPayloadError
        return parsed

    @staticmethod
    def _path_value(progress: dict[str, object]) -> tuple[str, ...]:
        value = progress.get("logical_path")
        if (
            not isinstance(value, list)
            or not value
            or len(value) > 100
            or not all(isinstance(item, str) and item for item in value)
            or sum(len(item) for item in value) > 8_000
        ):
            raise DriveSyncPayloadError
        return tuple(value)

    def _retry_delay(
        self, claim: ClaimedSyncJob, error: ConnectorError | None = None
    ) -> float:
        if isinstance(error, RateLimitError):
            requested = error.retry_after_seconds
            if requested is not None and math.isfinite(requested):
                return min(30.0, max(0.0, requested))
        cap = min(30.0, 0.5 * (2 ** max(claim.attempt_number - 1, 0)))
        return float(cap * max(0.0, min(1.0, float(self._random_value()))))

    def _credentials(
        self, claim: ClaimedSyncJob, sync_input: GoogleSyncInput
    ) -> Credentials:
        context = envelope_context(
            provider="google",
            workspace_id=str(claim.workspace_id),
            record_id=str(claim.connection_id),
            purpose="provider-credential",
        )
        try:
            payload = json.loads(
                self._encryption.decrypt(sync_input.credentials, context=context)
            )
            scopes = tuple(payload["scopes"])
            if (
                frozenset(scopes) != sync_input.scopes
                or DRIVE_READONLY_SCOPE not in scopes
            ):
                raise ValueError
            return Credentials(
                access_token=SecretStr(payload["access_token"]),
                refresh_token=SecretStr(payload["refresh_token"]),
                expires_at=datetime.fromisoformat(payload["expires_at"]),
                scopes=scopes,
            )
        except (
            InvalidTag,
            ValidationError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as error:
            raise DriveCredentialError from error

    def _encrypted_credentials(
        self, claim: ClaimedSyncJob, credentials: Credentials
    ) -> EncryptedEnvelope:
        context = envelope_context(
            provider="google",
            workspace_id=str(claim.workspace_id),
            record_id=str(claim.connection_id),
            purpose="provider-credential",
        )
        payload = json.dumps(
            {
                "access_token": credentials.access_token.get_secret_value(),
                "expires_at": (
                    credentials.expires_at.isoformat()
                    if credentials.expires_at
                    else None
                ),
                "refresh_token": (
                    credentials.refresh_token.get_secret_value()
                    if credentials.refresh_token
                    else None
                ),
                "schema_version": 1,
                "scopes": sorted(credentials.scopes),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self._encryption.encrypt(payload, context=context)
