"""Track authoritative provider reconciliation generations.

Revision ID: 0006_gmail_reconciliation
Revises: 0005_gmail_incremental_sync
Create Date: 2026-08-19
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0006_gmail_reconciliation"
down_revision = "0005_gmail_incremental_sync"
branch_labels = None
depends_on = None


def _execute_sql(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    _execute_sql("0006_gmail_reconciliation.sql")


def downgrade() -> None:
    op.drop_index(
        "ix_sources_connection_sync_marker", table_name="sources", schema="app"
    )
    op.drop_column("sources", "provider_sync_marker", schema="app")
