"""Isolated receipt for transactionally deleting a local-only account."""

from pathlib import Path

from alembic import op

revision = "0020_account_deletion"
down_revision = "0019_device_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0020_account_deletion.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION app.read_local_account_deletion(BYTEA)")
    op.execute(
        "DROP FUNCTION app.delete_local_account"
        "(UUID,UUID,UUID,BIGINT,BIGINT,BYTEA,UUID,BYTEA,BYTEA,BYTEA,BYTEA,UUID)"
    )
    op.execute("DROP TABLE app.local_account_deletion_receipts")
