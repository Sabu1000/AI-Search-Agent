from __future__ import annotations

import hashlib
import math
from uuid import UUID

import pytest

from universal_ai_search.indexing.pipeline import (
    EMBEDDING_DIMENSIONS,
    IndexingError,
    IndexingPipeline,
    PendingDocument,
    document_version_id,
    normalize_text,
    version_key,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("20000000-0000-0000-0000-000000000002")
VERSION_ID = UUID("30000000-0000-0000-0000-000000000003")


def pending(content: str, mime_type: str = "text/markdown") -> PendingDocument:
    return PendingDocument(
        workspace_id=WORKSPACE_ID,
        source_id=SOURCE_ID,
        document_version_id=VERSION_ID,
        title="Example",
        content=content,
        mime_type=mime_type,
        content_hash=hashlib.sha256(content.encode()).digest(),
        permissions_hash=hashlib.sha256(b"private").digest(),
        embedding_profile_id=1,
    )


def test_normalization_is_stable_and_removes_control_characters() -> None:
    assert normalize_text(" Cafe\u0301 \r\n\r\n\r\nbody\x00  \t") == "Café\n\nbody"


def test_prepare_chunks_deduplicates_and_builds_finite_embeddings() -> None:
    prepared = IndexingPipeline().prepare(
        pending("# Guide\n\nAlpha beta gamma.\n\nAlpha beta gamma.\n\n# Next\n\nDelta.")
    )

    assert prepared.normalized_text.startswith("# Guide")
    assert prepared.language == "en"
    assert prepared.token_count > 0
    assert [chunk.index for chunk in prepared.chunks] == list(
        range(len(prepared.chunks))
    )
    assert len({chunk.content_hash for chunk in prepared.chunks}) == len(
        prepared.chunks
    )
    for chunk in prepared.chunks:
        assert len(chunk.embedding) == EMBEDDING_DIMENSIONS
        assert all(math.isfinite(value) for value in chunk.embedding)
        assert math.isclose(
            math.sqrt(sum(value * value for value in chunk.embedding)), 1.0
        )


def test_prepare_is_deterministic_and_splits_large_blocks() -> None:
    document = pending(" ".join(f"word{index}" for index in range(1_700)))
    first = IndexingPipeline().prepare(document)
    second = IndexingPipeline().prepare(document)

    assert first == second
    assert len(first.chunks) == 3
    assert all(chunk.token_count <= 800 for chunk in first.chunks)


def test_version_identity_changes_with_permissions_and_is_repeatable() -> None:
    first = version_key(
        source_id=SOURCE_ID,
        content_hash=b"content",
        permissions_hash=b"private",
        profile_id=1,
    )
    second = version_key(
        source_id=SOURCE_ID,
        content_hash=b"content",
        permissions_hash=b"shared",
        profile_id=1,
    )

    assert first != second
    assert document_version_id(SOURCE_ID, first) == document_version_id(
        SOURCE_ID, first
    )


@pytest.mark.parametrize(
    ("content", "mime_type", "code"),
    [
        ("", "text/plain", "NO_INDEXABLE_TEXT"),
        ("hello", "application/pdf", "UNSUPPORTED_MEDIA_TYPE"),
    ],
)
def test_prepare_rejects_unindexable_inputs(
    content: str, mime_type: str, code: str
) -> None:
    with pytest.raises(IndexingError, match=code):
        IndexingPipeline().prepare(pending(content, mime_type))
