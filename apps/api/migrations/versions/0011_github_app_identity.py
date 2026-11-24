"""Bind future grants to the verified GitHub App identity."""

from alembic import op

revision = "0011_github_app_identity"
down_revision = "0010_github_webhook_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.connections ADD COLUMN github_app_id BIGINT")
    op.execute(
        """ALTER TABLE app.connections ADD CONSTRAINT ck_connections_github_app_id
           CHECK (github_app_id IS NULL OR (
               provider = 'github' AND github_installation_id IS NOT NULL
               AND github_app_id > 0
           ))"""
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.connections DROP CONSTRAINT ck_connections_github_app_id"
    )
    op.execute("ALTER TABLE app.connections DROP COLUMN github_app_id")
