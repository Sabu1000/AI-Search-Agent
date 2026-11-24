"""Persist verified GitHub installation identifiers and isolate setup state."""

from alembic import op

revision = "0008_github_app_authorization"
down_revision = "0007_drive_sync_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.oauth_transactions
            DROP CONSTRAINT ck_oauth_transactions_provider;
        ALTER TABLE app.oauth_transactions ADD CONSTRAINT ck_oauth_transactions_provider
            CHECK (provider IN ('google', 'github_login', 'github_app'));
        ALTER TABLE app.connections ADD COLUMN github_installation_id BIGINT;
        ALTER TABLE app.connections ADD CONSTRAINT ck_connections_github_installation
            CHECK (github_installation_id IS NULL
                OR (provider = 'github' AND github_installation_id > 0));
        CREATE UNIQUE INDEX uq_connections_github_installation
            ON app.connections (workspace_id, github_installation_id)
            WHERE github_installation_id IS NOT NULL AND status <> 'deleted';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM app.oauth_transactions WHERE provider = 'github_app';
        DROP INDEX app.uq_connections_github_installation;
        ALTER TABLE app.connections DROP CONSTRAINT ck_connections_github_installation;
        ALTER TABLE app.connections DROP COLUMN github_installation_id;
        ALTER TABLE app.oauth_transactions
            DROP CONSTRAINT ck_oauth_transactions_provider;
        ALTER TABLE app.oauth_transactions ADD CONSTRAINT ck_oauth_transactions_provider
            CHECK (provider IN ('google', 'github_login'));
        """
    )
