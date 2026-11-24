from __future__ import annotations

import base64
import hashlib
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from test_device_manifest_runtime import _folder
from test_device_registration_runtime import _engine, _scope

from universal_ai_search.auth.reauth_store import ReauthScope
from universal_ai_search.devices.upload_index_operation import (
    read_native_upload_index_operation,
)
from universal_ai_search.devices.upload_index_runtime import (
    build_native_upload_index_runtime,
)
from universal_ai_search.indexing.pipeline import (
    IndexingError,
    IndexingPipeline,
    embed_text,
)
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.search.planner import SearchFilters, build_plan
from universal_ai_search.search.repository import SearchRepository

VERSION = "synthetic-immutable-version"
CONTENT = b"# Private synthetic example\n\nAlways preserve the immutable snapshot."
MIME = "text/markdown"


@dataclass(frozen=True)
class Inputs:
    scope: ReauthScope
    device: UUID
    folder: UUID
    connection_id: UUID


def _inputs(connection: psycopg.Connection[tuple[object, ...]]) -> Inputs:
    current = _scope(connection)
    workspace, owner = current.workspace_id, current.user_id
    device, account = uuid4(), uuid4()
    connection.execute(
        """INSERT INTO app.connections
             (id,workspace_id,owner_user_id,provider,display_label,status)
           VALUES (%s,%s,%s,'local_files','Local test','active')""",
        (account, workspace, owner),
    )
    connection.execute(
        """INSERT INTO app.devices
             (id,workspace_id,user_id,connection_id,name,platform,app_version,
              public_key,credential_hash,status)
           VALUES (%s,%s,%s,%s,'Synthetic Mac','macos','0.1.0',%s,%s,'active')""",
        (device, workspace, owner, account, uuid4().bytes * 2, uuid4().bytes * 2),
    )
    connection.execute(
        "INSERT INTO app.workspace_usage(workspace_id) VALUES (%s)", (workspace,)
    )
    folder = _folder(connection, workspace, device)
    return Inputs(
        ReauthScope(owner, current.session_id, workspace, 1, 1), device, folder, account
    )


def _upload(
    connection: psycopg.Connection[tuple[object, ...]],
    data: Inputs,
    sequence: int = 1,
    raw: bytes = CONTENT,
) -> tuple[UUID, str]:
    operation, manifest, upload = uuid4(), uuid4(), uuid4()
    key = f"device-inbox/{uuid4()}"
    digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode()[:-1]
    intent = {
        "upload_id": str(upload),
        "external_id": "same-file",
        "size_bytes": len(raw),
        "mime_type": MIME,
        "content_sha256": digest,
    }
    connection.execute(
        """INSERT INTO app.device_manifest_pages
              (workspace_id,device_id,folder_id,manifest_id,sequence,final,
               body_hash,upload_intents,response)
           VALUES (%s,%s,%s,%s,%s,true,%s,%s::jsonb,'{}')""",
        (
            data.scope.workspace_id,
            data.device,
            data.folder,
            manifest,
            sequence,
            bytes(32),
            json.dumps([intent]),
        ),
    )
    connection.execute(
        """INSERT INTO app.device_upload_reservations
             (upload_id,workspace_id,device_id,folder_id,manifest_id,external_id,
              object_key,size_bytes,content_sha256,mime_type,issued_at,expires_at)
           VALUES (%s,%s,%s,%s,%s,'same-file',%s,%s,%s,%s,transaction_timestamp(),
                   transaction_timestamp()+interval '10 minutes')""",
        (
            upload,
            data.scope.workspace_id,
            data.device,
            data.folder,
            manifest,
            key,
            len(raw),
            digest,
            MIME,
        ),
    )
    connection.execute(
        """INSERT INTO app.device_upload_changes
             (id,workspace_id,device_id,batch_id,upload_id,nonce_hash,created_at,
              verification_started_at,completed_at,immutable_version,index_available_at)
           VALUES (%s,%s,%s,%s,%s,%s,now(),now(),now(),%s,
                   now()-interval '1 day')""",
        (
            operation,
            data.scope.workspace_id,
            data.device,
            uuid4(),
            upload,
            bytes(32),
            VERSION,
        ),
    )
    connection.commit()
    return operation, key


class Storage:
    def __init__(
        self, content: bytes = CONTENT, callback: Callable[[], None] | None = None
    ) -> None:
        self.content = content
        self.callback = callback
        self.calls: list[dict[str, str]] = []

    def get_object(self, **kwargs: str) -> Mapping[str, object]:
        self.calls.append(kwargs)
        if self.callback is not None:
            self.callback()
        return {
            "ContentType": MIME,
            "ContentLength": len(self.content),
            "VersionId": VERSION,
            "ChecksumSHA256": base64.b64encode(
                hashlib.sha256(self.content).digest()
            ).decode(),
            "Body": io.BytesIO(self.content),
        }


async def test_pinned_receipt_queues_real_document_then_ready_status_and_search(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    data = _inputs(connection)
    operation, key = _upload(connection, data)
    other = _scope(connection)
    client = Storage()
    worker = build_native_upload_index_runtime(database_dsn(), client, "fixtures-only")
    engine = _engine()
    try:
        reader = SearchRepository(engine)
        initial = await read_native_upload_index_operation(
            engine, scope=data.scope, operation_id=operation
        )
        assert initial is not None and initial.status == "pending"
        assert worker.run_once("bridge-fixture")
        assert client.calls == [
            {
                "Bucket": "fixtures-only",
                "Key": key,
                "VersionId": VERSION,
                "ChecksumMode": "ENABLED",
            }
        ]
        queued = await read_native_upload_index_operation(
            engine, scope=data.scope, operation_id=operation
        )
        assert queued is not None and queued.status == "pending"
        assert queued.started_at is not None and queued.completed_at is None
        assert IndexingRuntime(
            IndexRepository(database_dsn()), IndexingPipeline()
        ).run_once("real-index-worker")
        done = await read_native_upload_index_operation(
            engine, scope=data.scope, operation_id=operation
        )
        assert done is not None and done.status == "completed"
        assert (
            key not in done.model_dump_json() and VERSION not in done.model_dump_json()
        )
        assert (
            await read_native_upload_index_operation(
                engine,
                operation_id=operation,
                scope=ReauthScope(
                    other.user_id, other.session_id, other.workspace_id, 1, 1
                ),
            )
        ) is None
        assert (
            connection.execute(
                """SELECT s.title,v.object_storage_key,v.state FROM app.sources s
               JOIN app.document_versions v ON v.id = s.current_document_version_id
               WHERE s.connection_id=%s""",
                (data.connection_id,),
            ).fetchone()
            == ("Local file.md", key, "ready")
        )

        plan = build_plan(
            query="immutable snapshot",
            mode="results",
            filters=SearchFilters(providers=("local_files",)),
            limit=5,
        )

        async def retrieve():
            return await reader.retrieve(
                workspace_id=data.scope.workspace_id,
                user_id=data.scope.user_id,
                plan=plan,
                embedding=embed_text(plan.normalized_query),
            )

        assert any((await retrieve()).lanes.values())
        connection.execute(
            "UPDATE app.device_folders SET status='removed' WHERE id=%s", (data.folder,)
        )
        connection.commit()
        assert not any((await retrieve()).lanes.values())
        assert (
            await read_native_upload_index_operation(
                engine, scope=data.scope, operation_id=operation
            )
            is None
        )
    finally:
        await engine.dispose()


def test_folder_revocation_during_pinned_get_never_creates_indexing_job(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    data = _inputs(connection)
    operation, _ = _upload(connection, data)

    def revoke() -> None:
        connection.execute(
            "UPDATE app.device_folders SET status='removed' WHERE id=%s", (data.folder,)
        )
        connection.commit()

    worker = build_native_upload_index_runtime(
        database_dsn(), Storage(callback=revoke), "fixtures-only"
    )
    assert worker.run_once("revoke-mid-get")
    assert (
        connection.execute(
            """SELECT index_status,index_error_code,index_job_id
           FROM app.device_upload_changes WHERE id=%s""",
            (operation,),
        ).fetchone()
        == ("failed", "UPLOAD_INDEX_DENIED", None)
    )
    assert connection.execute(
        "SELECT count(*) FROM app.sources WHERE connection_id=%s",
        (data.connection_id,),
    ).fetchone() == (0,)


def test_revocation_after_index_load_blocks_promotion(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    data = _inputs(connection)
    _upload(connection, data)
    runtime = build_native_upload_index_runtime(
        database_dsn(), Storage(), "fixtures-only"
    )
    assert runtime.run_once("stage-fixture")
    index = IndexRepository(database_dsn())
    claim = index.claim("index-race")
    assert claim is not None
    pending = index.load_pending(claim)
    prepared = IndexingPipeline().prepare(pending)
    connection.execute(
        "UPDATE app.device_folders SET status='removed' WHERE id=%s", (data.folder,)
    )
    connection.commit()
    with pytest.raises(IndexingError, match="AUTHORIZATION_CHANGED"):
        index.promote(claim, prepared)
    index.fail(claim, "AUTHORIZATION_CHANGED", retryable=False)
    assert connection.execute(
        "SELECT count(*) FROM app.chunks WHERE workspace_id=%s",
        (data.scope.workspace_id,),
    ).fetchone() == (0,)


def test_older_receipt_cannot_replace_newer_ready_source(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    data = _inputs(connection)
    older, _ = _upload(connection, data)
    newer, newer_key = _upload(connection, data, sequence=2)
    connection.execute(
        """UPDATE app.device_upload_changes
           SET index_available_at=now()+interval '1 day'
           WHERE id=%s""",
        (older,),
    )
    connection.commit()
    worker = build_native_upload_index_runtime(
        database_dsn(), Storage(), "fixtures-only"
    )
    indexer = IndexingRuntime(IndexRepository(database_dsn()), IndexingPipeline())
    assert worker.run_once("newer-first")
    assert indexer.run_once("real-index-worker")
    connection.execute(
        """UPDATE app.device_upload_changes
           SET index_available_at=now()-interval '1 day'
           WHERE id=%s""",
        (older,),
    )
    connection.commit()
    assert worker.run_once("older-late")
    assert (
        connection.execute(
            """SELECT id,index_status,index_error_code FROM app.device_upload_changes
           WHERE workspace_id=%s ORDER BY index_status""",
            (data.scope.workspace_id,),
        ).fetchall()
        == [(older, "failed", "UPLOAD_INDEX_SUPERSEDED"), (newer, "queued", None)]
    )
    assert (
        connection.execute(
            """SELECT s.metadata->>'local_manifest_sequence',v.object_storage_key
           FROM app.sources s JOIN app.document_versions v
             ON v.id=s.current_document_version_id WHERE s.connection_id=%s""",
            (data.connection_id,),
        ).fetchone()
        == ("2", newer_key)
    )
