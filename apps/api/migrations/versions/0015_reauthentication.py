"""Purpose- and session-bound one-time reauthentication proofs."""

from alembic import op

revision = "0015_reauthentication"
down_revision = "0014_github_scope_fences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.one_time_tokens
          DROP CONSTRAINT ck_one_time_tokens_purpose,
          ADD CONSTRAINT ck_one_time_tokens_purpose CHECK (
            purpose IN ('verify_email', 'reset_password',
                        'change_email', 'reauthenticate')
          ),
          ADD COLUMN reauth_session_id UUID,
          ADD COLUMN reauth_workspace_id UUID,
          ADD COLUMN reauth_purpose TEXT,
          ADD COLUMN reauth_user_version BIGINT,
          ADD COLUMN reauth_workspace_version BIGINT,
          ADD CONSTRAINT fk_one_time_tokens_reauth_session
            FOREIGN KEY (reauth_session_id) REFERENCES app.sessions(id)
            ON DELETE CASCADE,
          ADD CONSTRAINT fk_one_time_tokens_reauth_membership
            FOREIGN KEY (reauth_workspace_id, user_id)
            REFERENCES app.workspace_members(workspace_id, user_id) ON DELETE CASCADE,
          ADD CONSTRAINT ck_one_time_tokens_reauth_scope CHECK (
            (purpose <> 'reauthenticate'
              AND reauth_session_id IS NULL AND reauth_workspace_id IS NULL
              AND reauth_purpose IS NULL AND reauth_user_version IS NULL
              AND reauth_workspace_version IS NULL)
            OR
            (purpose = 'reauthenticate'
              AND reauth_session_id IS NOT NULL AND reauth_workspace_id IS NOT NULL
              AND reauth_purpose IS NOT NULL
              AND reauth_purpose IN ('disconnect', 'export', 'delete_account')
              AND reauth_user_version IS NOT NULL AND reauth_user_version > 0
              AND reauth_workspace_version IS NOT NULL AND reauth_workspace_version > 0
              AND octet_length(token_hash) = 32
              AND expires_at <= created_at + INTERVAL '5 minutes')
          )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_one_time_tokens_reauth_session
            ON app.one_time_tokens (reauth_session_id)
            WHERE reauth_session_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_one_time_tokens_reauth_membership
            ON app.one_time_tokens (reauth_workspace_id, user_id)
            WHERE reauth_workspace_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE POLICY api_reauth_workspace_guard ON app.one_time_tokens
          AS RESTRICTIVE TO app_api
          USING (
            purpose <> 'reauthenticate'
            OR (reauth_workspace_id = app.current_workspace_id()
                AND app.has_active_workspace_membership(reauth_workspace_id))
          )
          WITH CHECK (
            purpose <> 'reauthenticate'
            OR (reauth_workspace_id = app.current_workspace_id()
                AND app.has_active_workspace_membership(reauth_workspace_id))
          )
        """
    )


def downgrade() -> None:
    # Invalidate outstanding proofs rather than converting them to weaker tokens.
    op.execute("DELETE FROM app.one_time_tokens WHERE purpose = 'reauthenticate'")
    op.execute("DROP POLICY api_reauth_workspace_guard ON app.one_time_tokens")
    op.execute(
        """
        ALTER TABLE app.one_time_tokens
          DROP CONSTRAINT ck_one_time_tokens_reauth_scope,
          DROP CONSTRAINT fk_one_time_tokens_reauth_session,
          DROP CONSTRAINT fk_one_time_tokens_reauth_membership,
          DROP CONSTRAINT ck_one_time_tokens_purpose,
          ADD CONSTRAINT ck_one_time_tokens_purpose
            CHECK (purpose IN ('verify_email', 'reset_password', 'change_email')),
          DROP COLUMN reauth_session_id,
          DROP COLUMN reauth_workspace_id,
          DROP COLUMN reauth_purpose,
          DROP COLUMN reauth_user_version,
          DROP COLUMN reauth_workspace_version
        """
    )
