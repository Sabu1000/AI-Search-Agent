"""Create the complete initial application schema.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-08-03
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _read_sql(filename: str) -> str:
    return (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )


def _execute_sql(filename: str) -> None:
    # PostgreSQL format() calls in the migration use server-side placeholders
    # such as %I. Skip DBAPI parameter handling so psycopg leaves them intact.
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        _read_sql(filename)
    )


def upgrade() -> None:
    _execute_sql("0001_initial_schema.sql")


def downgrade() -> None:
    _execute_sql("0001_initial_schema_down.sql")
