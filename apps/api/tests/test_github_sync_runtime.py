from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from uas_connector_sdk.errors import ProviderUnavailableError

from universal_ai_search.sync.github_repository import GitHubSyncTarget
from universal_ai_search.sync.github_runtime import GitHubSyncRuntime
from universal_ai_search.sync.repository import ClaimedSyncJob


def test_worker_does_not_fetch_without_claim() -> None:
    repository = Mock()
    repository.claim.return_value = None
    reader = SimpleNamespace(read=AsyncMock())
    assert (
        GitHubSyncRuntime(repository=repository, reader=reader).run_once("worker")
        is False
    )
    reader.read.assert_not_awaited()


def test_worker_commits_only_complete_snapshots() -> None:
    repository = Mock()
    claim = ClaimedSyncJob(uuid4(), uuid4(), uuid4(), 1, "worker")
    target = GitHubSyncTarget(uuid4(), 8, 15)
    repository.claim.return_value = claim
    repository.load.return_value = target
    reader = SimpleNamespace(read=AsyncMock(return_value=()))
    runtime = GitHubSyncRuntime(repository=repository, reader=reader)
    assert runtime.run_once("worker")
    repository.commit.assert_called_once_with(claim, target, ())
    reader.read.side_effect = ProviderUnavailableError
    assert runtime.run_once("worker")
    repository.commit.assert_called_once()
    repository.fail.assert_called_once_with(claim)
