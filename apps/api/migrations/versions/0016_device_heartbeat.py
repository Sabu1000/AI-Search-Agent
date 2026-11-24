"""Persist bounded native replay receipts and heartbeat liveness."""

from pathlib import Path

from alembic import op

revision = "0016_device_heartbeat"
down_revision = "0015_reauthentication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0016_device_heartbeat.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION app.claim_device_heartbeat"
        "(UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA)"
    )
    op.execute("DROP FUNCTION app.lookup_device_heartbeat(UUID, BYTEA)")
    op.execute("DROP TABLE app.device_request_nonces")
