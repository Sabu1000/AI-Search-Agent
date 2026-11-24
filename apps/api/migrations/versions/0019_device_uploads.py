"""Authorize scoped, quota-bound reservations for native staged uploads."""

from pathlib import Path

from alembic import op

revision = "0019_device_uploads"
down_revision = "0018_email_resend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0019_device_uploads.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    # There is no provider yet in this migration. A future provider must add
    # a cleanup barrier before any downgrade could forget staged objects.
    op.execute(
        "DROP FUNCTION app.reserve_device_upload"
        "(UUID,BYTEA,BYTEA,TIMESTAMPTZ,BYTEA,UUID,UUID,UUID,TEXT,BIGINT,TEXT,TEXT)"
    )
    op.execute("DROP TABLE app.device_upload_reservations")
