"""One-job indexing orchestration with durable failure recording."""

from __future__ import annotations

from .pipeline import IndexingError, IndexingPipeline
from .repository import IndexRepository


class IndexingRuntime:
    def __init__(self, repository: IndexRepository, pipeline: IndexingPipeline) -> None:
        self._repository = repository
        self._pipeline = pipeline

    def run_once(self, worker_id: str) -> bool:
        claim = self._repository.claim(worker_id)
        if claim is None:
            return False
        try:
            pending = self._repository.load_pending(claim)
            prepared = self._pipeline.prepare(pending)
            self._repository.promote(claim, prepared)
        except IndexingError as error:
            self._repository.fail(claim, error.code, retryable=False)
        except Exception:
            self._repository.fail(claim, "INDEXING_INTERNAL_ERROR", retryable=True)
            raise
        return True
