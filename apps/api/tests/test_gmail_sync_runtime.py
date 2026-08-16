from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID, uuid5

from uas_connector_sdk import Credentials
from uas_connector_sdk.errors import AuthenticationError, ProviderUnavailableError

from universal_ai_search.connections.crypto import (
    LocalEnvelopeEncryption,
    envelope_context,
)
from universal_ai_search.connections.gmail import GmailPage
from universal_ai_search.connections.google import GMAIL_READONLY_SCOPE
from universal_ai_search.sync.repository import ClaimedSyncJob, GoogleSyncInput
from universal_ai_search.sync.runtime import GmailSyncRuntime

WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000001")
JOB_ID = UUID("30000000-0000-4000-8000-000000000001")


def claim() -> ClaimedSyncJob:
    return ClaimedSyncJob(JOB_ID, WORKSPACE_ID, CONNECTION_ID, 1, "worker")


def encrypted_input(
    encryption: LocalEnvelopeEncryption, *, payload: dict[str, object] | None = None
) -> GoogleSyncInput:
    context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(CONNECTION_ID),
        purpose="provider-credential",
    )
    raw = json.dumps(
        {
            "access_token": "access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "refresh_token": "refresh",
            "schema_version": 1,
            "scopes": [GMAIL_READONLY_SCOPE],
        }
    ).encode()
    return GoogleSyncInput(
        encryption.encrypt(raw, context=context),
        payload or {"mode": "full", "source_families": ["gmail"]},
        frozenset({GMAIL_READONLY_SCOPE}),
    )


def encrypted_progress(
    encryption: LocalEnvelopeEncryption,
    *,
    job_id: UUID,
    history_id: str,
    page_token: str,
) -> dict[str, object]:
    context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(job_id),
        purpose="gmail-sync-progress",
    )
    envelope = encryption.encrypt(
        json.dumps(
            {"history_id": history_id, "page_token": page_token},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        context=context,
    )
    return {
        "ciphertext": base64.b64encode(envelope.ciphertext).decode(),
        "encrypted_data_key": base64.b64encode(envelope.encrypted_data_key).decode(),
        "key_version": envelope.key_version,
    }


class FakeClient:
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.page_result = GmailPage((), None)

    async def ensure_fresh(self, credentials: Credentials) -> Credentials:
        return credentials

    async def history_id(self, access_token: str) -> str:
        assert access_token == "access"
        if self.failure:
            raise self.failure
        return "history-1"

    async def page(self, **_: object) -> GmailPage:
        return self.page_result


def runtime(repository: Mock, client: FakeClient) -> GmailSyncRuntime:
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    repository.claim.return_value = claim()
    repository.load.return_value = encrypted_input(encryption)
    return GmailSyncRuntime(
        repository=repository,
        index_repository=Mock(),
        client=client,  # type: ignore[arg-type]
        encryption=encryption,
    )


def test_empty_queue_and_completed_page() -> None:
    repository = Mock()
    repository.claim.return_value = None
    sync = GmailSyncRuntime(
        repository=repository,
        index_repository=Mock(),
        client=FakeClient(),  # type: ignore[arg-type]
        encryption=LocalEnvelopeEncryption(b"e" * 32),
    )
    assert sync.run_once("worker") is False

    disabled = GmailSyncRuntime(
        repository=repository,
        index_repository=Mock(),
        client=FakeClient(),  # type: ignore[arg-type]
        encryption=LocalEnvelopeEncryption(b"e" * 32),
        enabled=False,
    )
    assert disabled.run_once("worker") is False

    repository = Mock()
    sync = runtime(repository, FakeClient())
    assert sync.run_once("worker") is True
    repository.complete.assert_called_once_with(claim(), history_id="history-1")
    repository.fail.assert_not_called()


def test_next_page_is_durably_advanced_without_reloading_history() -> None:
    repository = Mock()
    client = FakeClient()
    client.page_result = GmailPage((), "page-2")
    sync = runtime(repository, client)
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    repository.load.return_value = encrypted_input(
        encryption,
        payload={
            "gmail_progress": encrypted_progress(
                encryption,
                job_id=JOB_ID,
                history_id="history-1",
                page_token="page-1",
            ),
            "mode": "full",
            "source_families": ["gmail"],
        },
    )

    assert sync.run_once("worker") is True
    token_fingerprint = hashlib.sha256(b"page-2").hexdigest()
    next_job_id = uuid5(
        CONNECTION_ID,
        f"gmail-full-page:{token_fingerprint}",
    )
    repository.advance.assert_called_once()
    assert repository.advance.call_args.args == (claim(),)
    values = repository.advance.call_args.kwargs
    assert values["next_job_id"] == next_job_id
    assert values["token_fingerprint"] == token_fingerprint
    context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(next_job_id),
        purpose="gmail-sync-progress",
    )
    assert json.loads(
        encryption.decrypt(values["encrypted_progress"], context=context)
    ) == {"history_id": "history-1", "page_token": "page-2"}


def test_provider_failures_are_safely_classified() -> None:
    for failure, retryable, reauthorize in (
        (AuthenticationError(), False, True),
        (ProviderUnavailableError(), True, False),
    ):
        repository = Mock()
        client = FakeClient()
        client.failure = failure
        sync = runtime(repository, client)

        assert sync.run_once("worker") is True
        repository.fail.assert_called_once_with(
            claim(),
            error_code=failure.code.upper(),
            retryable=retryable,
            reauthorization_required=reauthorize,
        )
