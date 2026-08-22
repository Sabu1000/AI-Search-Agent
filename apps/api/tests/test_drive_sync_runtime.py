from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID, uuid5

from uas_connector_sdk import Credentials
from uas_connector_sdk.errors import AuthenticationError, RateLimitError

from universal_ai_search.connections.crypto import (
    LocalEnvelopeEncryption,
    envelope_context,
)
from universal_ai_search.connections.drive import (
    DRIVE_FOLDER_MIME_TYPE,
    DRIVE_SHORTCUT_MIME_TYPE,
    DriveItem,
    DrivePage,
)
from universal_ai_search.connections.google import DRIVE_READONLY_SCOPE
from universal_ai_search.sync.drive_runtime import DriveSyncRuntime
from universal_ai_search.sync.repository import ClaimedSyncJob, GoogleSyncInput

WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000001")
JOB_ID = UUID("30000000-0000-4000-8000-000000000001")


def claim() -> ClaimedSyncJob:
    return ClaimedSyncJob(JOB_ID, WORKSPACE_ID, CONNECTION_ID, 1, "worker")


def item(
    item_id: str,
    name: str,
    mime_type: str,
    *,
    parent: str = "root",
) -> DriveItem:
    return DriveItem(
        id=item_id,
        name=name,
        mime_type=mime_type,
        modified_at=datetime(2026, 8, 20, tzinfo=UTC),
        parent_ids=(parent,),
        owners=(),
        web_view_link=None,
        size=None,
        drive_id=None,
        shortcut_target_id="target" if mime_type == DRIVE_SHORTCUT_MIME_TYPE else None,
        shortcut_target_mime_type=(
            "text/plain" if mime_type == DRIVE_SHORTCUT_MIME_TYPE else None
        ),
    )


def encrypted_input(
    encryption: LocalEnvelopeEncryption,
    *,
    payload: dict[str, object] | None = None,
) -> GoogleSyncInput:
    context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(CONNECTION_ID),
        purpose="provider-credential",
    )
    credentials = encryption.encrypt(
        json.dumps(
            {
                "access_token": "access",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "refresh_token": "refresh",
                "schema_version": 1,
                "scopes": [DRIVE_READONLY_SCOPE],
            }
        ).encode(),
        context=context,
    )
    return GoogleSyncInput(
        credentials,
        payload or {"mode": "full", "source_families": ["google_drive"]},
        frozenset({DRIVE_READONLY_SCOPE}),
        None,
    )


class FakeDriveClient:
    def __init__(self) -> None:
        self.page_result = DrivePage((), None)
        self.failure: Exception | None = None

    async def ensure_fresh(self, credentials: Credentials) -> Credentials:
        return credentials

    async def children_page(self, **values: object) -> DrivePage:
        assert values["access_token"] == "access"
        if self.failure:
            raise self.failure
        return self.page_result


def runtime(repository: Mock, client: FakeDriveClient) -> DriveSyncRuntime:
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    repository.claim.return_value = claim()
    repository.load.return_value = encrypted_input(encryption)
    return DriveSyncRuntime(
        repository=repository,
        index_repository=Mock(),
        client=client,  # type: ignore[arg-type]
        encryption=encryption,
        random_value=lambda: 0.5,
    )


def decrypt_job(
    encryption: LocalEnvelopeEncryption, scheduled: object
) -> dict[str, object]:
    job_id = scheduled.job_id  # type: ignore[attr-defined]
    envelope = scheduled.encrypted_progress  # type: ignore[attr-defined]
    context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(job_id),
        purpose="drive-sync-progress",
    )
    return json.loads(encryption.decrypt(envelope, context=context))  # type: ignore[no-any-return]


def test_empty_queue_disabled_and_empty_root_complete() -> None:
    repository = Mock()
    repository.claim.return_value = None
    sync = DriveSyncRuntime(
        repository=repository,
        index_repository=Mock(),
        client=FakeDriveClient(),  # type: ignore[arg-type]
        encryption=LocalEnvelopeEncryption(b"e" * 32),
    )
    assert sync.run_once("worker") is False
    sync._enabled = False  # noqa: SLF001
    assert sync.run_once("worker") is False

    repository = Mock()
    sync = runtime(repository, FakeDriveClient())
    assert sync.run_once("worker") is True
    assert repository.finish_page.call_args.args == (claim(),)
    assert isinstance(repository.finish_page.call_args.kwargs["sync_run_id"], UUID)
    assert repository.finish_page.call_args.kwargs["scheduled_jobs"] == ()


def test_page_indexes_files_schedules_folders_and_never_follows_shortcuts() -> None:
    repository = Mock()
    client = FakeDriveClient()
    client.page_result = DrivePage(
        (
            item("file_1", "Plan.txt", "text/plain"),
            item("folder_1", "Subfolder", DRIVE_FOLDER_MIME_TYPE),
            item("shortcut_1", "Outside", DRIVE_SHORTCUT_MIME_TYPE),
        ),
        "page_2",
    )
    sync = runtime(repository, client)

    assert sync.run_once("worker") is True
    assert sync._index_repository.enqueue.call_count == 2  # noqa: SLF001
    documents = [
        call.args[2]
        for call in sync._index_repository.enqueue.call_args_list  # noqa: SLF001
    ]
    assert [document.external_id for document in documents] == ["file_1", "shortcut_1"]
    assert documents[0].provider_metadata["logical_path"] == "My Drive/Plan.txt"

    scheduled = repository.finish_page.call_args.kwargs["scheduled_jobs"]
    sync_run_id = repository.finish_page.call_args.kwargs["sync_run_id"]
    assert isinstance(sync_run_id, UUID)
    assert len(scheduled) == 2
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    folder_progress = decrypt_job(encryption, scheduled[0])
    page_progress = decrypt_job(encryption, scheduled[1])
    assert folder_progress == {
        "folder_id": "folder_1",
        "logical_path": ["My Drive", "Subfolder"],
        "sync_run_id": str(sync_run_id),
    }
    assert page_progress == {
        "folder_id": "root",
        "logical_path": ["My Drive"],
        "page_token": "page_2",
        "sync_run_id": str(sync_run_id),
    }
    assert scheduled[0].job_id == uuid5(sync_run_id, "drive-folder:folder_1")


def test_encrypted_progress_continues_selected_shared_drive_folder() -> None:
    repository = Mock()
    client = FakeDriveClient()
    sync = runtime(repository, client)
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    progress = {
        "drive_id": "shared_1",
        "folder_id": "folder_1",
        "logical_path": ["Shared", "Folder"],
        "page_token": "page_2",
        "sync_run_id": str(JOB_ID),
    }
    context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(JOB_ID),
        purpose="drive-sync-progress",
    )
    envelope = encryption.encrypt(json.dumps(progress).encode(), context=context)
    repository.load.return_value = encrypted_input(
        encryption,
        payload={
            "drive_progress": {
                "ciphertext": base64.b64encode(envelope.ciphertext).decode(),
                "encrypted_data_key": base64.b64encode(
                    envelope.encrypted_data_key
                ).decode(),
                "key_version": envelope.key_version,
            },
            "mode": "full",
            "source_families": ["google_drive"],
        },
    )

    assert sync.run_once("worker") is True
    assert client.page_result == DrivePage((), None)
    repository.finish_page.assert_called_once_with(
        claim(), sync_run_id=JOB_ID, scheduled_jobs=()
    )


def test_provider_and_payload_failures_are_classified() -> None:
    for failure, code, retryable, reauthorize, delay in (
        (AuthenticationError(), "AUTHENTICATION_FAILED", False, True, None),
        (RateLimitError(90), "RATE_LIMITED", True, False, 30.0),
    ):
        repository = Mock()
        client = FakeDriveClient()
        client.failure = failure
        sync = runtime(repository, client)
        assert sync.run_once("worker") is True
        repository.fail.assert_called_once_with(
            claim(),
            error_code=code,
            retryable=retryable,
            reauthorization_required=reauthorize,
            retry_delay_seconds=delay,
        )

    repository = Mock()
    sync = runtime(repository, FakeDriveClient())
    repository.load.return_value = encrypted_input(
        LocalEnvelopeEncryption(b"e" * 32), payload={"mode": "incremental"}
    )
    assert sync.run_once("worker") is True
    repository.fail.assert_called_once_with(
        claim(),
        error_code="DRIVE_SYNC_PAYLOAD_INVALID",
        retryable=False,
        retry_delay_seconds=None,
    )


def test_selected_root_and_parent_escape_validation_fail_closed() -> None:
    repository = Mock()
    client = FakeDriveClient()
    client.page_result = DrivePage(
        (item("file_1", "Plan", "text/plain", parent="x"),), None
    )
    sync = runtime(repository, client)
    repository.load.return_value = encrypted_input(
        LocalEnvelopeEncryption(b"e" * 32),
        payload={
            "drive_root": {"id": "selected", "name": "Selected"},
            "mode": "full",
            "source_families": ["google_drive"],
        },
    )

    assert sync.run_once("worker") is True
    repository.fail.assert_called_once_with(
        claim(),
        error_code="MALFORMED_ITEM",
        retryable=False,
        reauthorization_required=False,
        retry_delay_seconds=None,
    )
