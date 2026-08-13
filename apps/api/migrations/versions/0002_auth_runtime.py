"""Add fail-closed authentication runtime functions.

Revision ID: 0002_auth_runtime
Revises: 0001_initial_schema
Create Date: 2026-08-12
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002_auth_runtime"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def _execute_sql(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    _execute_sql("0002_auth_runtime.sql")


def downgrade() -> None:
    _execute_sql("0002_auth_runtime_down.sql")
