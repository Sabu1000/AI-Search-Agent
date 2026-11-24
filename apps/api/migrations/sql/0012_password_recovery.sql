SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE FUNCTION app.auth_issue_password_reset(
    requested_email TEXT,
    requested_token_id UUID,
    requested_token_hash BYTEA,
    requested_expires_at TIMESTAMPTZ
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app, public
AS $$
DECLARE
    target_user UUID;
    target_email TEXT;
    issued_at TIMESTAMPTZ;
BEGIN
    IF requested_email IS NULL OR char_length(requested_email) > 320
       OR octet_length(requested_token_hash) IS DISTINCT FROM 32
       OR requested_expires_at IS NULL OR requested_token_id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT users.id, users.email::TEXT INTO target_user, target_email
    FROM app.users
    WHERE users.email = lower(btrim(requested_email))::public.citext
      AND users.status = 'active' AND users.email_verified_at IS NOT NULL
      AND users.password_hash IS NOT NULL
    FOR UPDATE OF users;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    issued_at := clock_timestamp();
    IF requested_expires_at <= issued_at THEN
        RETURN NULL;
    END IF;

    UPDATE app.one_time_tokens SET consumed_at = issued_at
    WHERE user_id = target_user AND purpose = 'reset_password'
      AND consumed_at IS NULL;
    INSERT INTO app.one_time_tokens (
        id, user_id, purpose, token_hash, expires_at, created_at
    ) VALUES (
        requested_token_id, target_user, 'reset_password', requested_token_hash,
        LEAST(requested_expires_at, issued_at + interval '30 minutes'), issued_at
    );
    RETURN target_email;
END;
$$;

CREATE FUNCTION app.auth_lookup_password_reset(requested_token_hash BYTEA)
RETURNS TABLE (user_id UUID, email TEXT, authorization_version INTEGER)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
    SELECT users.id, users.email::TEXT, users.lock_version
    FROM app.one_time_tokens AS token
    JOIN app.users ON users.id = token.user_id
    WHERE octet_length(requested_token_hash) = 32
      AND token.token_hash = requested_token_hash AND token.purpose = 'reset_password'
      AND token.consumed_at IS NULL AND token.expires_at > clock_timestamp()
      AND users.status = 'active' AND users.email_verified_at IS NOT NULL
      AND users.password_hash IS NOT NULL
$$;

CREATE FUNCTION app.auth_complete_password_reset(
    requested_token_hash BYTEA,
    requested_user_id UUID,
    requested_version INTEGER,
    requested_email TEXT,
    requested_password_hash TEXT,
    requested_audit_id UUID,
    requested_subject_hash BYTEA,
    requested_request_id UUID
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
DECLARE
    target_email TEXT;
    changed_at TIMESTAMPTZ;
BEGIN
    IF octet_length(requested_token_hash) IS DISTINCT FROM 32
       OR octet_length(requested_subject_hash) IS DISTINCT FROM 32
       OR requested_password_hash IS NULL
       OR char_length(requested_password_hash) > 1024
       OR requested_password_hash NOT LIKE '$argon2id$%'
       OR requested_audit_id IS NULL OR requested_request_id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Issuance and completion acquire the same user lock before changing tokens.
    -- Login creation and refresh take a shared user lock before session writes.
    SELECT users.email::TEXT INTO target_email FROM app.users
    WHERE users.id = requested_user_id AND users.lock_version = requested_version
      AND users.email::TEXT = requested_email
      AND users.status = 'active' AND users.email_verified_at IS NOT NULL
      AND users.password_hash IS NOT NULL
    FOR UPDATE OF users;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    changed_at := clock_timestamp();

    UPDATE app.one_time_tokens SET consumed_at = changed_at
    WHERE user_id = requested_user_id AND purpose = 'reset_password'
      AND token_hash = requested_token_hash AND consumed_at IS NULL
      AND expires_at > changed_at;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    UPDATE app.users SET password_hash = requested_password_hash,
      lock_version = lock_version + 1, updated_at = changed_at
    WHERE id = requested_user_id;
    UPDATE app.sessions SET revoked_at = changed_at
    WHERE user_id = requested_user_id AND revoked_at IS NULL;
    UPDATE app.one_time_tokens SET consumed_at = changed_at
    WHERE user_id = requested_user_id AND purpose = 'reset_password'
      AND consumed_at IS NULL;

    INSERT INTO app.audit_events (
        id, workspace_ref_hash, actor_user_id, action, target_type,
        outcome, target_ref_hash, request_id
    ) VALUES (
        requested_audit_id, requested_subject_hash, requested_user_id,
        'auth.password_reset', 'user', 'succeeded',
        requested_subject_hash, requested_request_id
    );
    RETURN target_email;
END;
$$;

-- Hold the account/version stable while validating and rotating a refresh
-- token. Lock in the same user → session order as password reset.
CREATE OR REPLACE FUNCTION app.auth_refresh_lookup(requested_token_hash BYTEA)
RETURNS TABLE (
    session_id UUID,
    user_id UUID,
    family_id UUID,
    expires_at TIMESTAMPTZ,
    rotated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    csrf_token_hash BYTEA,
    authorization_version BIGINT,
    user_status TEXT,
    current_user_version INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
DECLARE
    matched_user_id UUID;
BEGIN
    SELECT sessions.user_id INTO matched_user_id FROM app.sessions
    WHERE sessions.refresh_token_hash = requested_token_hash;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    PERFORM id FROM app.users WHERE id = matched_user_id FOR SHARE;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT sessions.id, sessions.user_id, sessions.family_id, sessions.expires_at,
           sessions.rotated_at, sessions.revoked_at, sessions.csrf_token_hash,
           sessions.authorization_version, users.status, users.lock_version
    FROM app.sessions
    JOIN app.users ON users.id = sessions.user_id
    WHERE sessions.refresh_token_hash = requested_token_hash
      AND sessions.user_id = matched_user_id
    FOR UPDATE OF sessions;
END;
$$;

ALTER FUNCTION app.auth_issue_password_reset(TEXT, UUID, BYTEA, TIMESTAMPTZ) OWNER TO app_migrator;
ALTER FUNCTION app.auth_lookup_password_reset(BYTEA) OWNER TO app_migrator;
ALTER FUNCTION app.auth_complete_password_reset(BYTEA, UUID, INTEGER, TEXT, TEXT, UUID, BYTEA, UUID) OWNER TO app_migrator;

REVOKE ALL ON FUNCTION app.auth_issue_password_reset(TEXT, UUID, BYTEA, TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.auth_lookup_password_reset(BYTEA) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.auth_complete_password_reset(BYTEA, UUID, INTEGER, TEXT, TEXT, UUID, BYTEA, UUID) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION app.auth_issue_password_reset(TEXT, UUID, BYTEA, TIMESTAMPTZ) TO app_api;
GRANT EXECUTE ON FUNCTION app.auth_lookup_password_reset(BYTEA) TO app_api;
GRANT EXECUTE ON FUNCTION app.auth_complete_password_reset(BYTEA, UUID, INTEGER, TEXT, TEXT, UUID, BYTEA, UUID) TO app_api;
