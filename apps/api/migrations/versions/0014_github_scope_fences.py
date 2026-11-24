"""Gate GitHub sync on workspace state and fence targeted scope changes."""

from pathlib import Path

from alembic import op

revision = "0014_github_scope_fences"
down_revision = "0013_device_registration"
branch_labels = None
depends_on = None


def _run(file_name: str) -> None:
    path = Path(__file__).resolve().parents[1] / "sql" / file_name
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        path.read_text(encoding="utf-8")
    )


def upgrade() -> None:
    _run("0014_github_scope_fences.sql")


def downgrade() -> None:
    _run("0014_github_scope_fences_down.sql")
