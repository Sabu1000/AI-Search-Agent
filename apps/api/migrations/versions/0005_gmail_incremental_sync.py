"""Allow workers to claim incremental Gmail history jobs.

Revision ID: 0005_gmail_incremental_sync
Revises: 0004_gmail_sync_runtime
Create Date: 2026-08-19
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0005_gmail_incremental_sync"
down_revision = "0004_gmail_sync_runtime"
branch_labels = None
depends_on = None


def _execute_sql(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    _execute_sql("0005_gmail_incremental_sync.sql")


def downgrade() -> None:
    _execute_sql("0004_gmail_sync_runtime.sql")
