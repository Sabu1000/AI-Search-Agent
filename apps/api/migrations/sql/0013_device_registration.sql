SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TABLE app.device_registration_challenges (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES app.sessions (id) ON DELETE CASCADE,
    user_authorization_version BIGINT NOT NULL,
    workspace_authorization_version BIGINT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    FOREIGN KEY (workspace_id, user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE CASCADE,
    UNIQUE (workspace_id, session_id),
    CONSTRAINT ck_device_registration_user_version CHECK (user_authorization_version > 0),
    CONSTRAINT ck_device_registration_workspace_version CHECK (workspace_authorization_version > 0),
    CONSTRAINT ck_device_registration_expiry CHECK (expires_at = issued_at + INTERVAL '60 seconds'),
    CONSTRAINT ck_device_registration_consume_time CHECK (
        consumed_at IS NULL OR consumed_at BETWEEN issued_at AND expires_at)
);

CREATE INDEX ix_device_registration_challenge_session
    ON app.device_registration_challenges (session_id);

ALTER TABLE app.device_registration_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.device_registration_challenges FORCE ROW LEVEL SECURITY;
CREATE POLICY api_device_registration_isolation
    ON app.device_registration_challenges TO app_api
    USING (
        workspace_id = app.current_workspace_id()
        AND user_id = app.current_user_id()
        AND app.has_active_workspace_membership(workspace_id)
    )
    WITH CHECK (
        workspace_id = app.current_workspace_id()
        AND user_id = app.current_user_id()
        AND app.has_active_workspace_membership(workspace_id)
    );
REVOKE ALL ON app.device_registration_challenges FROM PUBLIC, app_worker, app_audit_reader;
GRANT SELECT, INSERT, UPDATE ON app.device_registration_challenges TO app_api;

ALTER TABLE app.device_registration_challenges OWNER TO app_migrator;
