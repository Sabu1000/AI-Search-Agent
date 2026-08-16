"""One-page-at-a-time Gmail synchronization orchestration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid5

from cryptography.exceptions import InvalidTag
from pydantic import SecretStr, ValidationError
from uas_connector_sdk import Credentials
from uas_connector_sdk.errors import AuthenticationError, ConnectorError

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
    ) -> None:
        self._repository = repository
        self._index_repository = index_repository
        self._client = client
        self._encryption = encryption
        self._enabled = enabled

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
            history_id, page_token = self._progress(claim, sync_input.payload)
            access_token = fresh.access_token.get_secret_value()
            if history_id is None:
                history_id = asyncio.run(self._client.history_id(access_token))
            page = asyncio.run(
                self._client.page(access_token=access_token, page_token=page_token)
            )
            for document in page.documents:
                self._index_repository.enqueue(
                    claim.workspace_id, claim.connection_id, document
                )
            if page.next_page_token:
                token_fingerprint = hashlib.sha256(
                    page.next_page_token.encode()
                ).hexdigest()
                next_job_id = uuid5(
                    claim.connection_id,
                    f"gmail-full-page:{token_fingerprint}",
                )
                self._repository.advance(
                    claim,
                    next_job_id=next_job_id,
                    token_fingerprint=token_fingerprint,
                    encrypted_progress=self._encrypted_progress(
                        claim,
                        next_job_id=next_job_id,
                        history_id=history_id,
                        page_token=page.next_page_token,
                    ),
                )
            else:
                self._repository.complete(claim, history_id=history_id)
        except ConnectorError as error:
            self._repository.fail(
                claim,
                error_code=error.code.upper(),
                retryable=error.retryable,
                reauthorization_required=isinstance(error, AuthenticationError),
            )
        except GmailCredentialError:
            self._repository.fail(
                claim, error_code="GMAIL_CREDENTIAL_INVALID", retryable=False
            )
        except GmailSyncPayloadError:
            self._repository.fail(
                claim, error_code="GMAIL_SYNC_PAYLOAD_INVALID", retryable=False
            )
        except Exception:
            self._repository.fail(
                claim, error_code="GMAIL_SYNC_INTERNAL_ERROR", retryable=True
            )
            raise
        return True

    def _progress(
        self, claim: ClaimedSyncJob, payload: dict[str, object]
    ) -> tuple[str | None, str | None]:
        encoded = payload.get("gmail_progress")
        if encoded is None:
            return None, None
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
            history_id, page_token = (
                progress["history_id"],
                progress["page_token"],
            )
            if not isinstance(history_id, str) or not isinstance(page_token, str):
                raise ValueError
            return history_id, page_token
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
        history_id: str,
        page_token: str,
    ) -> EncryptedEnvelope:
        context = envelope_context(
            provider="google",
            workspace_id=str(claim.workspace_id),
            record_id=str(next_job_id),
            purpose="gmail-sync-progress",
        )
        progress = json.dumps(
            {"history_id": history_id, "page_token": page_token},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self._encryption.encrypt(progress, context=context)

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
