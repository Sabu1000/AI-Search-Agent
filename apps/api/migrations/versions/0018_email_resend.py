"""Safely issue another email proof for a pending account."""

from alembic import op

revision = "0018_email_resend"
down_revision = "0017_device_manifests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.auth_resend_verification(
            recipient TEXT, next_id UUID, next_hash BYTEA
        )
        RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, app, public AS $$
        DECLARE
            pending_user UUID;
            sent_at TIMESTAMPTZ;
        BEGIN
            IF recipient IS NULL OR char_length(recipient) NOT BETWEEN 3 AND 320
                OR next_id IS NULL OR next_hash IS NULL OR octet_length(next_hash) <> 32
            THEN
                RETURN false;
            END IF;
            SELECT u.id INTO pending_user FROM app.users AS u
            WHERE u.email = lower(btrim(recipient))::public.citext
              AND u.status = 'pending_verification' AND u.email_verified_at IS NULL
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            sent_at := clock_timestamp();
            UPDATE app.one_time_tokens AS token
                SET consumed_at = sent_at
            WHERE token.user_id = pending_user AND token.purpose = 'verify_email'
                AND token.consumed_at IS NULL;
            INSERT INTO app.one_time_tokens (id,user_id,purpose,token_hash,expires_at)
            VALUES (next_id,pending_user,'verify_email',next_hash,sent_at+INTERVAL '24 hours');
            RETURN true;
        END
        $$;
        ALTER FUNCTION app.auth_resend_verification(TEXT, UUID, BYTEA) OWNER TO app_migrator;
        REVOKE ALL ON FUNCTION app.auth_resend_verification(TEXT, UUID, BYTEA) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.auth_resend_verification(TEXT, UUID, BYTEA) TO app_api;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION app.auth_resend_verification(TEXT, UUID, BYTEA)")
