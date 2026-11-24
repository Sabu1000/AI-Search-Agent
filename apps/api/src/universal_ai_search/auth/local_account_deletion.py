"""Remove eligible single-owner database-only accounts and issue blind receipts."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key

from .reauth import ReauthenticationFailed, ReauthenticationService
from .reauth_store import ReauthScope
from .security import random_token

_RECEIPT_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class LocalAccountDeletionUnsupported(Exception):
    """Resources or membership prevent safe local-only deletion."""


class LocalAccountDeletionReceiptInvalid(Exception):
    """Unknown, expired, or malformed receipt capability."""


@dataclass(frozen=True)
class LocalAccountDeletion:
    id: UUID
    requested_at: datetime
    deadline_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class IssuedLocalAccountDeletion:
    deletion: LocalAccountDeletion
    token: str = field(repr=False)


def _deletion(row: RowMapping) -> LocalAccountDeletion:
    return LocalAccountDeletion(
        row["deletion_id"], row["requested_at"], row["deadline_at"], row["completed_at"]
    )


class LocalAccountDeletionStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        reauth: ReauthenticationService,
        hash_key: bytes,
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("account deletion hash key must be at least 32 bytes")
        self._engine = engine
        self._reauth = reauth
        self._hash_key = hash_key

    def _hash(self, label: bytes, value: bytes) -> bytes:
        return hmac.digest(
            self._hash_key, b"account-deletion:" + label + b":" + value, "sha256"
        )

    async def delete(
        self,
        *,
        scope: ReauthScope,
        reauth_token: str,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> IssuedLocalAccountDeletion:
        key = validate_idempotency_key(idempotency_key)
        digest = self._reauth.proof_hash(reauth_token)
        receipt = random_token()
        async with self._engine.begin() as connection:
            # Conservative bound for this synchronous, database-local operation.
            await connection.execute(text("SET LOCAL lock_timeout = '3s'"))
            await connection.execute(text("SET LOCAL statement_timeout = '15s'"))
            await connection.execute(
                text(
                    """SELECT set_config('app.user_id', :actor, true),
                              set_config('app.workspace_id', :workspace, true)"""
                ),
                {"actor": str(scope.user_id), "workspace": str(scope.workspace_id)},
            )
            result = await connection.execute(
                text(
                    """SELECT * FROM app.delete_local_account(
                        :actor, :workspace, :session, :user_version, :workspace_version,
                        :reauth, :deletion, :receipt, :key, :workspace_hash,
                        :subject_hash, :correlation)"""
                ),
                {
                    "actor": scope.user_id,
                    "workspace": scope.workspace_id,
                    "session": scope.session_id,
                    "user_version": scope.user_version,
                    "workspace_version": scope.workspace_version,
                    "reauth": digest,
                    "deletion": uuid4(),
                    "receipt": self._hash(b"receipt", receipt.encode()),
                    "key": self._hash(
                        b"key",
                        scope.user_id.bytes + scope.workspace_id.bytes + key.encode(),
                    ),
                    # Match the workspace reference prefix used by other
                    # privacy operations for content-free audit correlation.
                    "workspace_hash": hmac.digest(
                        self._hash_key,
                        b"workspace:" + scope.workspace_id.bytes,
                        "sha256",
                    ),
                    "subject_hash": self._hash(b"audit-user", scope.user_id.bytes),
                    "correlation": correlation_id,
                },
            )
            row = result.mappings().one()
            if row["outcome"] == "unsupported":
                raise LocalAccountDeletionUnsupported
            if row["outcome"] != "completed":
                raise ReauthenticationFailed
            deletion = _deletion(row)
        # Never return a capability for a transaction that did not commit.
        return IssuedLocalAccountDeletion(deletion, receipt)

    async def read_receipt(self, token: str) -> LocalAccountDeletion:
        if not _RECEIPT_TOKEN.fullmatch(token):
            raise LocalAccountDeletionReceiptInvalid
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("SELECT * FROM app.read_local_account_deletion(:receipt)"),
                {"receipt": self._hash(b"receipt", token.encode())},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise LocalAccountDeletionReceiptInvalid
            return _deletion(row)
