"""Persist verified GitHub webhook hints without exposing tenant lookups."""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.routes.github_webhooks import GitHubWebhookUnavailable

from .github_webhook import GitHubWebhookHint, GitHubWebhookWork


class SQLAlchemyGitHubWebhookService:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, hint: GitHubWebhookHint) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        """SELECT app.record_github_webhook_hint(
                           :installation, :repository, :delivery, :event,
                           :digest, :installation_wide, :repository_scope)"""
                    ),
                    {
                        "installation": hint.installation_id,
                        "repository": hint.repository_id,
                        "delivery": hint.delivery_id,
                        "event": hint.event_name,
                        "digest": hint.payload_hash,
                        "installation_wide": hint.work
                        is GitHubWebhookWork.INSTALLATION,
                        "repository_scope": hint.work
                        is GitHubWebhookWork.REPOSITORY_SCOPE,
                    },
                )
        except SQLAlchemyError:
            raise GitHubWebhookUnavailable from None
