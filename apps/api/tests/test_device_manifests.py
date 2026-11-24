from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_device_heartbeat import _KEY, _NOW, MemoryStore
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.device_manifests import router
from universal_ai_search.devices.manifests import (
    DeviceManifestService,
    ManifestResponse,
    ManifestWrite,
    UploadRequest,
)


def _entries() -> list[dict[str, Any]]:
    return [
        {
            "external_id": "synthetic-file",
            "relative_path": "notes/test.md",
            "size_bytes": 12,
            "modified_at": "2030-09-30T05:00:00Z",
            "content_sha256": "A" * 43,
            "mime_type": "text/markdown",
        }
    ]


def _page(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "manifest_id": str(uuid4()),
        "folder_id": str(uuid4()),
        "sequence": 1,
        "final": True,
        "entries": _entries() if entries is None else entries,
    }


class ManifestMemoryStore(MemoryStore):
    received: ManifestWrite | None = None

    async def commit(self, data: ManifestWrite) -> ManifestResponse:
        self.received = data
        return ManifestResponse(
            requested_uploads=[
                UploadRequest(external_id=entry.external_id, upload_id=uuid4())
                for entry in data.accepted
            ],
            acknowledged_deletions=[],
            skipped_entries=list(data.skipped),
            next_sequence=data.page.sequence + 1,
        )


def _transport(store: ManifestMemoryStore) -> tuple[TestClient, str]:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    app.state.device_manifest_service = DeviceManifestService(store, hash_key=_KEY)
    return (
        TestClient(app, base_url="https://device.test"),
        f"/v1/devices/{store.device_id}/manifests",
    )


def _headers(
    store: ManifestMemoryStore, target: str, body: bytes
) -> list[tuple[str, str]]:
    timestamp = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = base64.urlsafe_b64encode(b"m" * 16).rstrip(b"=").decode()
    digest = (
        "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    )
    request_id = str(uuid4())
    transcript = "\n".join(
        ("uas-device-request-v1", "POST", target, timestamp, nonce, digest, request_id)
    )
    signature = (
        base64.urlsafe_b64encode(store.key.sign(transcript.encode()))
        .rstrip(b"=")
        .decode()
    )
    return [
        ("Authorization", f"Device {store.credential}"),
        ("X-Device-Timestamp", timestamp),
        ("X-Device-Nonce", nonce),
        ("Content-Digest", digest),
        ("X-Device-Signature", signature),
        ("X-Request-ID", request_id),
        ("Content-Type", "application/json"),
    ]


def test_exact_body_and_metadata_only_commit() -> None:
    store = ManifestMemoryStore()
    client, path = _transport(store)
    normal = _entries()[0]
    large = {
        **normal,
        "external_id": "larger",
        "relative_path": "large.md",
        "size_bytes": 100_000_001,
    }
    unsupported = {**normal, "external_id": "other", "relative_path": "diagram.jpg"}
    body = json.dumps(_page([normal, large, unsupported])).encode()
    headers = _headers(store, path, body)
    response = client.post(path, headers=headers, content=body)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert (
        store.received is not None
        and store.received.body_hash == hashlib.sha256(body).digest()
    )
    data = response.json()
    assert data["next_sequence"] == 2
    assert data["acknowledged_deletions"] == []
    assert data["requested_uploads"][0]["external_id"] == "synthetic-file"
    UUID(data["requested_uploads"][0]["upload_id"])
    assert [item["reason"] for item in data["skipped_entries"]] == [
        "FILE_TOO_LARGE",
        "UNSUPPORTED_FILE_TYPE",
    ]
    assert "notes/test.md" not in response.text
    assert client.post(path, headers=headers, content=body + b" ").status_code == 401
    assert (
        client.post(path, headers=headers + [headers[-1]], content=body).status_code
        == 401
    )
    assert client.post(path, headers=headers[:-1], content=body).status_code == 401


@pytest.mark.parametrize(
    "relative",
    [
        "/absolute.md",
        "../escape.md",
        "dir/../../escape.md",
        "dir//empty.md",
        "dir/./missing.md",
        "C:/drive.md",
        r"\\unc\share.md",
        "foo\x00.md",
        "foo\u200e.md",
        "dir/．．/confusable.md",
        "dir/NUL.md",
        "dir/LPT².md",
        "a.md ",
        "d\u0065\u0301composed.md",
        "a/／escaped.md",
        ".private/notes.md",
    ],
)
def test_signed_unsafe_path_is_rejected_without_commit(relative: str) -> None:
    store = ManifestMemoryStore()
    client, path = _transport(store)
    body = json.dumps(_page([{**_entries()[0], "relative_path": relative}])).encode()
    rejection = client.post(path, headers=_headers(store, path, body), content=body)
    assert rejection.status_code == 422
    assert store.received is None
    assert "synthetic-file" not in rejection.text


def test_strict_metadata_limits_and_duplicate_paths() -> None:
    store = ManifestMemoryStore()
    client, path = _transport(store)
    old = _entries()[0]
    conflicting = {**old, "external_id": "alias", "relative_path": "NOTES/TEST.MD"}
    wrong_digest = {**old, "content_sha256": "A" * 42 + "B"}
    bad_date = {**old, "modified_at": "2030-00-30T05:00:00Z"}
    pages = (
        _page([old, conflicting]),
        _page([old, old]),
        _page([wrong_digest]),
        _page([bad_date]),
        _page([old] * 1001),
        {**_page(), "sequence": True},
        {**_page(), "delete": True},
    )
    valid = json.dumps(_page())
    for text in [json.dumps(p) for p in pages] + [
        valid.replace('"sequence": 1', '"sequence": 1,"sequence": 3')
    ]:
        body = text.encode()
        assert (
            client.post(
                path, headers=_headers(store, path, body), content=body
            ).status_code
            == 422
        )
    oversize = b" " * 2_000_001
    assert (
        client.post(
            path, headers=_headers(store, path, oversize), content=oversize
        ).status_code
        == 413
    )
    assert store.received is None
