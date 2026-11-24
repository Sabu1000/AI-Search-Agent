"""Durably deduplicate signed GitHub hints and coalesce reconciliation jobs."""

from pathlib import Path

from alembic import op

revision = "0010_github_webhook_runtime"
down_revision = "0009_github_sync_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "sql" / "0010_github_webhook_runtime.sql"
    sql = path.read_text(encoding="utf-8")
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "app.record_github_webhook_hint(BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN)"
    )
    op.execute("DROP INDEX app.ix_connections_github_webhook")
    op.execute("DROP INDEX app.ix_provider_events_github_hash")
    op.execute(
        "ALTER TABLE app.source_collections DROP CONSTRAINT "
        "ck_github_authorization_generation"
    )
    op.execute(
        "ALTER TABLE app.source_collections DROP COLUMN github_authorization_generation"
    )
