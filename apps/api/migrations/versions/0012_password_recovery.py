"""Add hashed password-reset tokens and atomic account recovery."""

from pathlib import Path

from alembic import op

revision = "0012_password_recovery"
down_revision = "0011_github_app_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "sql" / "0012_password_recovery.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        path.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION app.auth_complete_password_reset("
        "BYTEA, UUID, INTEGER, TEXT, TEXT, UUID, BYTEA, UUID)"
    )
    op.execute("DROP FUNCTION app.auth_lookup_password_reset(BYTEA)")
    op.execute(
        "DROP FUNCTION app.auth_issue_password_reset(TEXT, UUID, BYTEA, TIMESTAMPTZ)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.auth_refresh_lookup(requested_token_hash BYTEA)
        RETURNS TABLE (
            session_id UUID, user_id UUID, family_id UUID, expires_at TIMESTAMPTZ,
            rotated_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, csrf_token_hash BYTEA,
            authorization_version BIGINT, user_status TEXT, current_user_version INTEGER
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, app, public
        AS $$
            SELECT sessions.id, sessions.user_id, sessions.family_id,
                   sessions.expires_at, sessions.rotated_at, sessions.revoked_at,
                   sessions.csrf_token_hash, sessions.authorization_version,
                   users.status, users.lock_version
            FROM app.sessions
            JOIN app.users ON users.id = sessions.user_id
            WHERE sessions.refresh_token_hash = requested_token_hash
            FOR UPDATE OF sessions
        $$;
        """
    )
    # Irreversible session revocation is retained on downgrade; invalidate
    # any outstanding reset links so re-upgrading cannot resurrect them.
    op.execute("DELETE FROM app.one_time_tokens WHERE purpose = 'reset_password'")
