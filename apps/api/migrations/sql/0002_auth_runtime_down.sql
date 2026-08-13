SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DROP FUNCTION IF EXISTS app.auth_refresh_lookup(BYTEA);
DROP FUNCTION IF EXISTS app.auth_memberships(UUID);
DROP FUNCTION IF EXISTS app.auth_verify_email(BYTEA, UUID);
DROP FUNCTION IF EXISTS app.auth_login_lookup(TEXT);
DROP FUNCTION IF EXISTS app.auth_register_user(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BYTEA, TIMESTAMPTZ);

ALTER TABLE app.sessions
    DROP CONSTRAINT IF EXISTS ck_sessions_authorization_version,
    DROP COLUMN IF EXISTS authorization_version,
    DROP COLUMN IF EXISTS csrf_token_hash;

ALTER TABLE app.users
    DROP CONSTRAINT IF EXISTS ck_users_locale_length,
    DROP CONSTRAINT IF EXISTS ck_users_terms_version_length,
    DROP COLUMN IF EXISTS locale,
    DROP COLUMN IF EXISTS terms_version;
