SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

ALTER TABLE app.sessions
    ADD COLUMN csrf_token_hash BYTEA NOT NULL,
    ADD COLUMN authorization_version BIGINT NOT NULL DEFAULT 1,
    ADD CONSTRAINT ck_sessions_authorization_version CHECK (authorization_version > 0);

ALTER TABLE app.users
    ADD COLUMN terms_version TEXT,
    ADD COLUMN locale TEXT,
    ADD CONSTRAINT ck_users_terms_version_length CHECK (
        terms_version IS NULL OR char_length(terms_version) BETWEEN 1 AND 50
    ),
    ADD CONSTRAINT ck_users_locale_length CHECK (
        locale IS NULL OR char_length(locale) BETWEEN 2 AND 35
    );

CREATE OR REPLACE FUNCTION app.auth_register_user(
    requested_user_id UUID,
    requested_email TEXT,
    requested_password_hash TEXT,
    requested_full_name TEXT,
    requested_terms_version TEXT,
    requested_locale TEXT,
    requested_token_id UUID,
    requested_token_hash BYTEA,
    requested_token_expiry TIMESTAMPTZ
)
RETURNS TABLE (created BOOLEAN, user_id UUID)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app, public
AS $$
BEGIN
    IF requested_token_expiry <= clock_timestamp()
       OR char_length(requested_email) > 320
       OR char_length(requested_password_hash) > 1024 THEN
        RAISE EXCEPTION 'invalid registration input';
    END IF;

    INSERT INTO app.users (
        id, email, password_hash, full_name, status, terms_version, locale
    )
    VALUES (
        requested_user_id,
        lower(btrim(requested_email))::public.citext,
        requested_password_hash,
        btrim(requested_full_name),
        'pending_verification', requested_terms_version, requested_locale
    )
    ON CONFLICT (email) DO NOTHING;

    IF FOUND THEN
        INSERT INTO app.auth_identities (
            id, user_id, issuer, subject, email_at_link_time
        ) VALUES (
            gen_random_uuid(), requested_user_id, 'password',
            lower(btrim(requested_email)), lower(btrim(requested_email))::public.citext
        );
        INSERT INTO app.one_time_tokens (
            id, user_id, purpose, token_hash, expires_at
        ) VALUES (
            requested_token_id, requested_user_id, 'verify_email',
            requested_token_hash, requested_token_expiry
        );
        RETURN QUERY SELECT true, requested_user_id;
    ELSE
        RETURN QUERY SELECT false, NULL::UUID;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION app.auth_login_lookup(requested_email TEXT)
RETURNS TABLE (
    user_id UUID,
    password_hash TEXT,
    full_name TEXT,
    status TEXT,
    email_verified BOOLEAN,
    authorization_version INTEGER
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, app, public
AS $$
    SELECT id, users.password_hash, users.full_name, users.status,
           users.email_verified_at IS NOT NULL, users.lock_version
    FROM app.users
    WHERE users.email = lower(btrim(requested_email))::public.citext
    LIMIT 1
$$;

CREATE OR REPLACE FUNCTION app.auth_verify_email(
    requested_token_hash BYTEA,
    requested_workspace_id UUID
)
RETURNS TABLE (
    user_id UUID,
    email TEXT,
    full_name TEXT,
    workspace_id UUID,
    authorization_version INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app, public
AS $$
DECLARE
    matched_user app.users%ROWTYPE;
BEGIN
    SELECT users.* INTO matched_user
    FROM app.one_time_tokens AS token
    JOIN app.users AS users ON users.id = token.user_id
    WHERE token.token_hash = requested_token_hash
      AND token.purpose = 'verify_email'
      AND token.consumed_at IS NULL
      AND token.expires_at > clock_timestamp()
    FOR UPDATE OF token, users;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE app.one_time_tokens
    SET consumed_at = clock_timestamp()
    WHERE token_hash = requested_token_hash;

    UPDATE app.users
    SET status = 'active', email_verified_at = COALESCE(email_verified_at, clock_timestamp()),
        updated_at = clock_timestamp(), lock_version = lock_version + 1
    WHERE id = matched_user.id
    RETURNING * INTO matched_user;

    INSERT INTO app.workspaces (id, name, plan, status)
    VALUES (requested_workspace_id, matched_user.full_name, 'free', 'active');
    INSERT INTO app.workspace_members (workspace_id, user_id, role, status)
    VALUES (requested_workspace_id, matched_user.id, 'owner', 'active');
    INSERT INTO app.workspace_usage (workspace_id) VALUES (requested_workspace_id);

    RETURN QUERY SELECT matched_user.id, matched_user.email::TEXT,
        matched_user.full_name, requested_workspace_id, matched_user.lock_version;
END
$$;

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
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
    SELECT sessions.id, sessions.user_id, sessions.family_id, sessions.expires_at,
           sessions.rotated_at, sessions.revoked_at, sessions.csrf_token_hash,
           sessions.authorization_version,
           users.status, users.lock_version
    FROM app.sessions
    JOIN app.users ON users.id = sessions.user_id
    WHERE sessions.refresh_token_hash = requested_token_hash
    FOR UPDATE OF sessions
$$;

CREATE OR REPLACE FUNCTION app.auth_memberships(requested_user_id UUID)
RETURNS TABLE (
    workspace_id UUID,
    name TEXT,
    role TEXT,
    status TEXT,
    authorization_version INTEGER
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
    SELECT members.workspace_id, workspaces.name, members.role, members.status,
           workspaces.authorization_version
    FROM app.workspace_members AS members
    JOIN app.workspaces ON workspaces.id = members.workspace_id
    WHERE requested_user_id = app.current_user_id()
      AND members.user_id = requested_user_id
      AND members.status = 'active'
      AND workspaces.status = 'active'
    ORDER BY members.created_at, members.workspace_id
$$;

ALTER FUNCTION app.auth_register_user(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BYTEA, TIMESTAMPTZ) OWNER TO app_migrator;
ALTER FUNCTION app.auth_login_lookup(TEXT) OWNER TO app_migrator;
ALTER FUNCTION app.auth_verify_email(BYTEA, UUID) OWNER TO app_migrator;
ALTER FUNCTION app.auth_refresh_lookup(BYTEA) OWNER TO app_migrator;
ALTER FUNCTION app.auth_memberships(UUID) OWNER TO app_migrator;

REVOKE ALL ON FUNCTION app.auth_register_user(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BYTEA, TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.auth_login_lookup(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.auth_verify_email(BYTEA, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.auth_refresh_lookup(BYTEA) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.auth_memberships(UUID) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION app.auth_register_user(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BYTEA, TIMESTAMPTZ) TO app_api;
GRANT EXECUTE ON FUNCTION app.auth_login_lookup(TEXT) TO app_api;
GRANT EXECUTE ON FUNCTION app.auth_verify_email(BYTEA, UUID) TO app_api;
GRANT EXECUTE ON FUNCTION app.auth_refresh_lookup(BYTEA) TO app_api;
GRANT EXECUTE ON FUNCTION app.auth_memberships(UUID) TO app_api;
