"""Persist scoped, one-object native verification operations."""

from pathlib import Path

from alembic import op

revision = "0022_device_upload_completion"
down_revision = "0021_account_export"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0022_device_upload_completion.sql"
    )
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION app.device_has_staged_upload(UUID)")
    op.execute(
        "DROP FUNCTION app.read_device_upload_change(UUID,UUID,UUID,UUID,BIGINT,BIGINT)"
    )
    op.execute(
        "DROP FUNCTION app.device_upload_change"
        "(UUID,BYTEA,BYTEA,TIMESTAMPTZ,BYTEA,UUID,UUID,TEXT,BIGINT,TEXT,TEXT)"
    )
    op.execute("DROP TABLE app.device_upload_changes")
