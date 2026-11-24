"""Add repository-scoped GitHub reconciliation claims and visibility fences."""

from pathlib import Path

from alembic import op

revision = "0009_github_sync_runtime"
down_revision = "0008_github_app_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "sql" / "0009_github_sync_runtime.sql"
    sql = path.read_text(encoding="utf-8")
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.claim_github_sync_job(TEXT, INTEGER)")
    op.execute(
        "ALTER TABLE app.source_collections "
        "DROP CONSTRAINT ck_source_collections_github_verified"
    )
    op.execute("ALTER TABLE app.source_collections DROP COLUMN github_verified_at")
