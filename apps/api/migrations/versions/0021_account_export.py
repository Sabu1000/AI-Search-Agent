"""Store only keyed snapshot fingerprints for restricted account exports."""

from pathlib import Path

from alembic import op

revision = "0021_account_export"
down_revision = "0020_account_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0021_account_export.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.local_account_exports")
    op.execute(
        "DROP FUNCTION app.authorize_local_account_export(UUID,UUID,UUID,BIGINT,BIGINT)"
    )
