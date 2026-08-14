from __future__ import annotations

from unittest.mock import Mock
from uuid import UUID

import pytest

from universal_ai_search.indexing.pipeline import IndexingError
from universal_ai_search.indexing.repository import ClaimedJob
from universal_ai_search.indexing.runtime import IndexingRuntime


def claim() -> ClaimedJob:
    return ClaimedJob(
        job_id=UUID("10000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("20000000-0000-0000-0000-000000000002"),
        source_id=UUID("30000000-0000-0000-0000-000000000003"),
        document_version_id=UUID("40000000-0000-0000-0000-000000000004"),
        attempt_number=1,
        worker_id="worker-1",
    )


def test_empty_queue_returns_false() -> None:
    repository = Mock()
    repository.claim.return_value = None

    assert IndexingRuntime(repository, Mock()).run_once("worker-1") is False


def test_successful_job_is_promoted() -> None:
    repository = Mock()
    pipeline = Mock()
    claimed = claim()
    repository.claim.return_value = claimed
    repository.load_pending.return_value = pending = Mock()
    pipeline.prepare.return_value = prepared = Mock()

    assert IndexingRuntime(repository, pipeline).run_once("worker-1") is True
    pipeline.prepare.assert_called_once_with(pending)
    repository.promote.assert_called_once_with(claimed, prepared)
    repository.fail.assert_not_called()


def test_safe_pipeline_failure_is_persisted() -> None:
    repository = Mock()
    pipeline = Mock()
    claimed = claim()
    repository.claim.return_value = claimed
    pipeline.prepare.side_effect = IndexingError("NO_INDEXABLE_TEXT")

    assert IndexingRuntime(repository, pipeline).run_once("worker-1") is True
    repository.fail.assert_called_once_with(
        claimed, "NO_INDEXABLE_TEXT", retryable=False
    )


def test_unexpected_failure_is_retryable_and_propagated() -> None:
    repository = Mock()
    pipeline = Mock()
    claimed = claim()
    repository.claim.return_value = claimed
    pipeline.prepare.side_effect = RuntimeError("private diagnostic")

    with pytest.raises(RuntimeError, match="private diagnostic"):
        IndexingRuntime(repository, pipeline).run_once("worker-1")
    repository.fail.assert_called_once_with(
        claimed, "INDEXING_INTERNAL_ERROR", retryable=True
    )
