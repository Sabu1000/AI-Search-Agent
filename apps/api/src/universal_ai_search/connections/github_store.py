"""PostgreSQL persistence for GitHub App state and audited repository grants."""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.context import current_request_id

from .crypto import EncryptedEnvelope
from .github import READ_PERMISSIONS, GitHubAuthorizationError
from .store import OAuthTransaction, _set_context


class SQLAlchemyGitHubConnectionStore:
    def __init__(self, engine: AsyncEngine, *, audit_key: bytes) -> None:
        self._engine = engine
        self._audit_key = audit_key

    async def create(
        self,
        *,
        transaction_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        state_hash: bytes,
        nonce_hash: bytes,
        envelope: EncryptedEnvelope,
        return_path: str,
        expires_at: datetime,
    ) -> None:
        async with self._engine.begin() as conn:
            await _set_context(conn, workspace_id=workspace_id, user_id=user_id)
            await conn.execute(
                text(
                    """INSERT INTO app.oauth_transactions (
                    id, workspace_id, user_id, provider, state_hash, nonce_hash,
                    pkce_verifier_ciphertext, encrypted_data_key, key_version,
                    redirect_path, expires_at
                    ) VALUES (
                    :id, :workspace, :actor, 'github_app', :state, :nonce,
                    :ciphertext, :wrapped_key, :key_version, :path, :expires)"""
                ),
                {
                    "id": transaction_id,
                    "workspace": workspace_id,
                    "actor": user_id,
                    "state": state_hash,
                    "nonce": nonce_hash,
                    "ciphertext": envelope.ciphertext,
                    "wrapped_key": envelope.encrypted_data_key,
                    "key_version": envelope.key_version,
                    "path": return_path,
                    "expires": expires_at,
                },
            )

    async def consume(
        self, *, workspace_id: UUID, user_id: UUID, state_hash: bytes
    ) -> OAuthTransaction | None:
        async with self._engine.begin() as conn:
            await _set_context(conn, workspace_id=workspace_id, user_id=user_id)
            result = await conn.execute(
                text(
                    """UPDATE app.oauth_transactions SET consumed_at = clock_timestamp()
                    WHERE workspace_id = :workspace AND user_id = :actor
                      AND provider = 'github_app' AND state_hash = :state
                      AND consumed_at IS NULL AND expires_at > clock_timestamp()
                    RETURNING id, redirect_path, pkce_verifier_ciphertext,
                              encrypted_data_key, key_version"""
                ),
                {"workspace": workspace_id, "actor": user_id, "state": state_hash},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            await conn.execute(
                text(
                    """UPDATE app.oauth_transactions
                       SET pkce_verifier_ciphertext = NULL,
                         encrypted_data_key = NULL, key_version = NULL WHERE id = :id"""
                ),
                {"id": row["id"]},
            )
        return OAuthTransaction(
            id=row["id"],
            workspace_id=workspace_id,
            user_id=user_id,
            encrypted_payload=EncryptedEnvelope(
                row["pkce_verifier_ciphertext"],
                row["encrypted_data_key"],
                row["key_version"],
            ),
            redirect_path=row["redirect_path"],
        )

    async def save_grants(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        installation_id: int,
        github_app_id: int,
        repository_ids: frozenset[int],
    ) -> UUID:
        # Never replace a previous grant. Reconciliation and reauthorization
        # must first handle the resulting visibility and deletion transitions.
        connection_id = uuid4()
        params = {
            "workspace": workspace_id,
            "actor": user_id,
            "session": session_id,
            "id": connection_id,
            "installation": installation_id,
            "github_app": github_app_id,
        }
        async with self._engine.begin() as conn:
            await _set_context(conn, workspace_id=workspace_id, user_id=user_id)
            allowed = await conn.scalar(
                text(
                    """SELECT true FROM app.workspace_members m
                    JOIN app.workspaces w ON w.id = m.workspace_id
                    JOIN app.users u ON u.id = m.user_id
                    JOIN app.sessions s ON s.user_id = u.id
                    WHERE m.workspace_id = :workspace AND m.user_id = :actor
                      AND m.role IN ('owner', 'admin') AND m.status = 'active'
                      AND w.status = 'active' AND u.status = 'active'
                      AND u.email_verified_at IS NOT NULL AND s.id = :session
                      AND s.revoked_at IS NULL AND s.rotated_at IS NULL
                      AND s.expires_at > clock_timestamp()
                    FOR SHARE OF m, w, u, s"""
                ),
                params,
            )
            if allowed is not True:
                raise GitHubAuthorizationError
            created = await conn.scalar(
                text(
                    """INSERT INTO app.connections (
                         id, workspace_id, owner_user_id, provider,
                         github_installation_id, github_app_id, display_label, status)
                       VALUES (:id, :workspace, :actor, 'github',
                               :installation, :github_app, 'GitHub', 'pending')
                       ON CONFLICT DO NOTHING RETURNING id"""
                ),
                params,
            )
            if created is None:
                raise GitHubAuthorizationError
            await conn.execute(
                text(
                    """INSERT INTO app.connection_scopes
                       (workspace_id, connection_id, scope)
                       VALUES (:workspace, :id, :scope)"""
                ),
                [
                    {**params, "scope": f"{permission}:{level}"}
                    for permission, level in READ_PERMISSIONS.items()
                ],
            )
            collections = {repo_id: uuid4() for repo_id in sorted(repository_ids)}
            await conn.execute(
                text(
                    """INSERT INTO app.source_collections
                       (id, workspace_id, connection_id, provider_external_id,
                        kind, name, selected, status)
                       VALUES (:collection, :workspace, :id, :external,
                               'repository', :name, true, 'active')"""
                ),
                [
                    {
                        **params,
                        "collection": collections[repo_id],
                        "external": str(repo_id),
                        "name": f"GitHub repository {repo_id}",
                    }
                    for repo_id in collections
                ],
            )
            await conn.execute(
                text(
                    """INSERT INTO app.jobs
                       (id, workspace_id, connection_id, job_type, queue,
                        idempotency_key, status, payload)
                       VALUES (:job, :workspace, :id, 'sync', 'sync', :job_key,
                               'pending', CAST(:payload AS JSONB))"""
                ),
                [
                    {
                        **params,
                        "job": uuid5(collection_id, "github-initial-sync"),
                        "job_key": f"github-initial:{collection_id}",
                        "payload": json.dumps(
                            {
                                "mode": "github_repository",
                                "collection_id": str(collection_id),
                            }
                        ),
                    }
                    for collection_id in collections.values()
                ],
            )
            await conn.execute(
                text(
                    """INSERT INTO app.audit_events
                       (id, workspace_id, workspace_ref_hash, actor_user_id,
                        action, target_type, outcome, target_ref_hash, request_id)
                       VALUES (:event_id, :workspace, :workspace_hash, :actor,
                               'github.grants_saved', 'connection', 'succeeded',
                               :target_hash, :request_id)"""
                ),
                {
                    **params,
                    "event_id": uuid4(),
                    "workspace_hash": hmac.digest(
                        self._audit_key, b"workspace:" + workspace_id.bytes, "sha256"
                    ),
                    "target_hash": hmac.digest(
                        self._audit_key, b"github:" + connection_id.bytes, "sha256"
                    ),
                    "request_id": current_request_id(),
                },
            )
        return connection_id
