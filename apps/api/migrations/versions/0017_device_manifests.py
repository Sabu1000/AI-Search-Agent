"""Persist scoped metadata-only device manifest receipts."""

from pathlib import Path

from alembic import op

revision = "0017_device_manifests"
down_revision = "0016_device_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0017_device_manifests.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION app.commit_device_manifest"
        "(UUID,BYTEA,BYTEA,TIMESTAMPTZ,BYTEA,BYTEA,UUID,UUID,BIGINT,BOOLEAN,JSONB,JSONB)"
    )
    op.execute("DROP FUNCTION app.lookup_device_manifest(UUID,BYTEA)")
    op.execute("DROP TABLE app.device_manifest_pages")
    op.execute(
        "ALTER TABLE app.device_folders "
        "DROP CONSTRAINT uq_device_folders_workspace_device_id"
    )
