from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from uas_connector_sdk import (
    AccessMetadata,
    Credentials,
    CursorAdvanced,
    NormalizedDocument,
    Provider,
    SyncContext,
    UpsertSource,
    canonical_json,
    make_change_id,
    stable_hash,
)


def document(**overrides: object) -> NormalizedDocument:
    values: dict[str, object] = {
        "external_id": "file:1",
        "provider": Provider.GOOGLE_DRIVE,
        "source_type": "document",
        "title": "Plan",
        "content": "A search plan",
        "canonical_url": "https://docs.example.test/file/1",
        "mime_type": "text/plain",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "modified_at": datetime(2026, 1, 2, tzinfo=UTC),
        "access_metadata": AccessMetadata(user_ids=("u1",)),
        "provider_metadata": {"z": 2, "a": 1},
    }
    values.update(overrides)
    return NormalizedDocument.model_validate(values)


def test_hashes_are_canonical_and_repeatable() -> None:
    assert canonical_json({"z": 2, "a": 1}) == '{"a":1,"z":2}'
    assert stable_hash({"z": 2, "a": 1}) == stable_hash({"a": 1, "z": 2})
    assert make_change_id(Provider.GITHUB, "repo:1", "v1") == make_change_id(
        Provider.GITHUB, "repo:1", "v1"
    )
    assert len(stable_hash("value")) == 64
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"invalid": float("nan")})


def test_document_hashes_content_and_permissions() -> None:
    item = document()
    assert item.content_hash == stable_hash("A search plan")
    assert item.permissions_hash == stable_hash(item.access_metadata.model_dump(mode="json"))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/file",
        "https://user:password@example.test/file",
        "file:///tmp/private.txt",
        "not-a-url",
    ],
)
def test_document_rejects_unsafe_canonical_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS URL"):
        document(canonical_url=url)


def test_document_rejects_invalid_dates_and_large_metadata() -> None:
    with pytest.raises(ValidationError, match="cannot precede"):
        document(modified_at=datetime(2025, 1, 1, tzinfo=UTC))
    with pytest.raises(ValidationError, match="64 KiB"):
        document(provider_metadata={"value": "x" * 70_000})
    with pytest.raises(ValidationError):
        document(provider_metadata={"value": {1, 2}})
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        document(created_at=datetime(2026, 1, 1))
    non_utc = timezone(timedelta(hours=-6))
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        document(created_at=datetime(2026, 1, 1, tzinfo=non_utc))


def test_change_requires_matching_provider() -> None:
    with pytest.raises(ValidationError, match="must match"):
        UpsertSource(provider=Provider.GITHUB, change_id="1", document=document())


def test_cursor_is_bounded_and_nonempty() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        CursorAdvanced(provider=Provider.GITHUB, change_id="1", cursor={})
    with pytest.raises(ValidationError, match="16 KiB"):
        CursorAdvanced(provider=Provider.GITHUB, change_id="1", cursor={"x": "a" * 17_000})


def test_credentials_and_context_require_utc() -> None:
    credentials = Credentials(access_token="secret", expires_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert "secret" not in repr(credentials)
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        Credentials(access_token="secret", expires_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        SyncContext(
            workspace_id=uuid4(),
            connection_id=uuid4(),
            sync_job_id=uuid4(),
            requested_at=datetime(2026, 1, 1),
        )
