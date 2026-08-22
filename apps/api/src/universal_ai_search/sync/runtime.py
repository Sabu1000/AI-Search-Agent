"""One-page-at-a-time Gmail synchronization orchestration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import random
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from cryptography.exceptions import InvalidTag
from pydantic import SecretStr, ValidationError
from uas_connector_sdk import Credentials
from uas_connector_sdk.errors import (
    AuthenticationError,
    ConnectorError,
    CursorInvalidError,
    RateLimitError,
)

from universal_ai_search.connections.crypto import (
    EncryptedEnvelope,
    LocalEnvelopeEncryption,
    envelope_context,
)
from universal_ai_search.connections.gmail import HttpGmailClient
from universal_ai_search.indexing.repository import IndexRepository

from .repository import ClaimedSyncJob, GoogleSyncInput, GoogleSyncRepository


class GmailCredentialError(Exception):
    pass


class GmailSyncPayloadError(Exception):
    pass


class GmailSyncRuntime:
    def __init__(
        self,
        *,
        repository: GoogleSyncRepository,
        index_repository: IndexRepository,
        client: HttpGmailClient,
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
            credentials = self._credentials(claim, sync_input)
            fresh = asyncio.run(self._client.ensure_fresh(credentials))
            if fresh != credentials:
                self._repository.save_credentials(
                    claim, self._encrypted_credentials(claim, fresh)
                )
            access_token = fresh.access_token.get_secret_value()
            mode = sync_input.payload.get("mode")
            if mode == "full":
                self._run_full(claim, sync_input, access_token)
            elif mode == "incremental":
                self._run_incremental(claim, sync_input, access_token)
            else:
                raise GmailSyncPayloadError
        except CursorInvalidError:
            self._repository.recover_full(claim)
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
        except GmailCredentialError:
            self._repository.fail(
                claim,
                error_code="GMAIL_CREDENTIAL_INVALID",
                retryable=False,
                retry_delay_seconds=None,
            )
        except GmailSyncPayloadError:
            self._repository.fail(
                claim,
                error_code="GMAIL_SYNC_PAYLOAD_INVALID",
                retryable=False,
                retry_delay_seconds=None,
            )
        except Exception:
            self._repository.fail(
                claim,
                error_code="GMAIL_SYNC_INTERNAL_ERROR",
                retryable=True,
                retry_delay_seconds=self._retry_delay(claim),
            )
            raise
        return True

    def _run_full(
        self, claim: ClaimedSyncJob, sync_input: GoogleSyncInput, access_token: str
    ) -> None:
        progress = self._progress(claim, sync_input.payload)
        history_id = self._string_value(progress, "history_id")
        page_token = self._string_value(progress, "page_token")
        if progress is None:
            sync_marker = uuid5(claim.job_id, "gmail-full-reconciliation")
        else:
            sync_marker = self._uuid_value(progress, "sync_marker")
        if history_id is None:
            history_id = asyncio.run(self._client.history_id(access_token))
        page = asyncio.run(
            self._client.page(access_token=access_token, page_token=page_token)
        )
        for document in page.documents:
            self._index_repository.enqueue(
                claim.workspace_id, claim.connection_id, document
            )
        self._index_repository.mark_provider_sync_seen(
            claim.workspace_id,
            claim.connection_id,
            tuple(document.external_id for document in page.documents),
            sync_marker,
        )
        if page.next_page_token:
            self._advance(
                claim,
                mode="full",
                page_token=page.next_page_token,
                progress={
                    "history_id": history_id,
                    "page_token": page.next_page_token,
                    "sync_marker": str(sync_marker),
                },
            )
        else:
            self._index_repository.reconcile_gmail_full_sync(
                claim.workspace_id, claim.connection_id, sync_marker
            )
            self._repository.complete(claim, history_id=history_id, mode="full")

    def _run_incremental(
        self, claim: ClaimedSyncJob, sync_input: GoogleSyncInput, access_token: str
    ) -> None:
        progress = self._progress(claim, sync_input.payload)
        start_history_id = self._string_value(progress, "start_history_id")
        page_token = self._string_value(progress, "page_token")
        if start_history_id is None:
            start_history_id = sync_input.history_id
        if not start_history_id:
            raise GmailSyncPayloadError
        page = asyncio.run(
            self._client.history_page(
                access_token=access_token,
                start_history_id=start_history_id,
                page_token=page_token,
            )
        )
        for document in page.documents:
            self._index_repository.enqueue(
                claim.workspace_id, claim.connection_id, document
            )
        for external_id in page.deleted_external_ids:
            self._index_repository.tombstone_gmail_message(
                claim.workspace_id, claim.connection_id, external_id
            )
        if page.next_page_token:
            self._advance(
                claim,
                mode="incremental",
                page_token=page.next_page_token,
                progress={
                    "start_history_id": start_history_id,
                    "page_token": page.next_page_token,
                },
            )
        else:
            self._repository.complete(
                claim, history_id=page.history_id, mode="incremental"
            )

    def _advance(
        self,
        claim: ClaimedSyncJob,
        *,
        mode: str,
        page_token: str,
        progress: dict[str, str],
    ) -> None:
        token_fingerprint = hashlib.sha256(
            f"{claim.job_id}:{page_token}".encode()
        ).hexdigest()
        next_job_id = uuid5(claim.job_id, f"gmail-{mode}-page:{token_fingerprint}")
        self._repository.advance(
            claim,
            mode=mode,
            next_job_id=next_job_id,
            token_fingerprint=token_fingerprint,
            encrypted_progress=self._encrypted_progress(
                claim, next_job_id=next_job_id, progress=progress
            ),
        )

    def _progress(
        self, claim: ClaimedSyncJob, payload: dict[str, object]
    ) -> dict[str, object] | None:
        encoded = payload.get("gmail_progress")
        if encoded is None:
            return None
        if not isinstance(encoded, dict):
            raise GmailSyncPayloadError
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
                purpose="gmail-sync-progress",
            )
            progress = json.loads(self._encryption.decrypt(envelope, context=context))
            if not isinstance(progress, dict):
                raise ValueError
            return progress
        except (
            InvalidTag,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as error:
            raise GmailSyncPayloadError from error

    def _encrypted_progress(
        self,
        claim: ClaimedSyncJob,
        *,
        next_job_id: UUID,
        progress: dict[str, str],
    ) -> EncryptedEnvelope:
        context = envelope_context(
            provider="google",
            workspace_id=str(claim.workspace_id),
            record_id=str(next_job_id),
            purpose="gmail-sync-progress",
        )
        payload = json.dumps(
            progress,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self._encryption.encrypt(payload, context=context)

    @staticmethod
    def _string_value(progress: dict[str, object] | None, key: str) -> str | None:
        if progress is None:
            return None
        value = progress.get(key)
        if not isinstance(value, str) or not value:
            raise GmailSyncPayloadError
        return value

    @staticmethod
    def _uuid_value(progress: dict[str, object], key: str) -> UUID:
        value = progress.get(key)
        if not isinstance(value, str):
            raise GmailSyncPayloadError
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise GmailSyncPayloadError from error
        if str(parsed) != value:
            raise GmailSyncPayloadError
        return parsed

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
            if frozenset(scopes) != sync_input.scopes:
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
            raise GmailCredentialError from error

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
