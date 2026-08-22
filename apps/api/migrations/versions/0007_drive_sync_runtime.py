"""Add the bounded Google Drive sync worker claim function.

Revision ID: 0007_drive_sync_runtime
Revises: 0006_gmail_reconciliation
"""

from pathlib import Path

from alembic import op

revision = "0007_drive_sync_runtime"
down_revision = "0006_gmail_reconciliation"
branch_labels = None
depends_on = None


def _execute_sql(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    _execute_sql("0007_drive_sync_runtime.sql")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.claim_drive_sync_job(TEXT, INTEGER)")
