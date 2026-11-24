"""Fetch one complete selected-repository snapshot per durable worker claim."""

from __future__ import annotations

import asyncio
from typing import Protocol

from uas_connector_sdk import NormalizedDocument
from uas_connector_sdk.errors import ConnectorError

from universal_ai_search.connections.github import GitHubAuthorizationError
from universal_ai_search.connections.github_issues import GitHubDiscussionClient
from universal_ai_search.connections.github_repository import HttpGitHubRepositoryClient

from .github_repository import (
    GitHubSyncClaimError,
    GitHubSyncRepository,
    GitHubSyncTarget,
)

MAX_DOCUMENTS = 1000
MAX_TEXT_BYTES = 16 * 1024 * 1024


class SnapshotReader(Protocol):
    async def read(
        self, target: GitHubSyncTarget
    ) -> tuple[NormalizedDocument, ...]: ...


class GitHubSnapshotReader:
    def __init__(self, files: HttpGitHubRepositoryClient) -> None:
        self._files = files

    async def read(self, target: GitHubSyncTarget) -> tuple[NormalizedDocument, ...]:
        snapshot = await self._files.open_snapshot(
            installation_id=target.installation_id, repository_id=target.repository_id
        )
        collected: list[NormalizedDocument] = []
        total_bytes = 0

        def add(document: NormalizedDocument) -> None:
            nonlocal total_bytes
            total_bytes += len(document.content.encode())
            if len(collected) >= MAX_DOCUMENTS or total_bytes > MAX_TEXT_BYTES:
                raise GitHubSyncClaimError
            collected.append(document)

        async for document in self._files.iter_documents(snapshot):
            add(document)
        owner, repository = snapshot.full_name.split("/", maxsplit=1)
        for document in await GitHubDiscussionClient(
            repository_ids=frozenset({target.repository_id})
        ).scan(
            installation_id=target.installation_id,
            repository_id=target.repository_id,
            owner=owner,
            repository=repository,
            token=snapshot._token,
        ):
            add(document)
        await self._files.verify_snapshot_access(snapshot)
        return tuple(collected)


class GitHubSyncRuntime:
    def __init__(
        self,
        *,
        repository: GitHubSyncRepository,
        reader: SnapshotReader,
    ) -> None:
        self._repository = repository
        self._reader = reader

    def run_once(self, worker_id: str) -> bool:
        claim = self._repository.claim(worker_id)
        if claim is None:
            return False
        try:
            target = self._repository.load(claim)
            documents = asyncio.run(self._read(target))
            self._repository.commit(claim, target, documents)
        except (
            ConnectorError,
            GitHubAuthorizationError,
            GitHubSyncClaimError,
            TimeoutError,
        ):
            self._repository.fail(claim)
        return True

    async def _read(self, target: GitHubSyncTarget) -> tuple[NormalizedDocument, ...]:
        async with asyncio.timeout(120):
            return await self._reader.read(target)
