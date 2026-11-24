"""Add a scoped, single-use device registration challenge."""

from pathlib import Path

from alembic import op

revision = "0013_device_registration"
down_revision = "0012_password_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0013_device_registration.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.device_registration_challenges")
