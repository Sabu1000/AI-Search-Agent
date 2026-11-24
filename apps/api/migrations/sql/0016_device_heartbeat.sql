SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TABLE app.device_request_nonces (
    workspace_id UUID NOT NULL,
    device_id UUID NOT NULL,
    nonce_hash BYTEA NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (device_id, nonce_hash),
    FOREIGN KEY (workspace_id, device_id)
        REFERENCES app.devices (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_device_request_nonces_hash_length CHECK (octet_length(nonce_hash) = 32),
    CONSTRAINT ck_device_request_nonces_expiry_finite CHECK (isfinite(expires_at))
);

CREATE INDEX ix_device_request_nonces_device_expiry
    ON app.device_request_nonces (device_id, expires_at);

ALTER TABLE app.device_request_nonces OWNER TO app_migrator;
ALTER TABLE app.device_request_nonces ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.device_request_nonces FORCE ROW LEVEL SECURITY;
REVOKE ALL ON app.device_request_nonces FROM PUBLIC, app_api, app_worker, app_audit_reader;

-- Neither caller has a user session or tenant GUC. Narrow, schema-qualified
-- SECURITY DEFINER functions are the only API entry point, scoped by both
-- device UUID and domain-separated credential hash. A pending connection can
-- prove reachability only in this unprivileged route, never be promoted here.
CREATE FUNCTION app.lookup_device_heartbeat(requested_device UUID, credential BYTEA)
RETURNS TABLE (public_key BYTEA, checked_at TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
    SELECT d.public_key, clock_timestamp()
    FROM app.devices AS d
    JOIN app.connections AS c
      ON c.workspace_id = d.workspace_id AND c.id = d.connection_id
     AND c.owner_user_id = d.user_id
    JOIN app.workspaces AS w ON w.id = d.workspace_id
    JOIN app.workspace_members AS m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users AS u ON u.id = m.user_id
    WHERE d.id = requested_device AND d.credential_hash = credential
      AND d.status = 'active' AND d.revoked_at IS NULL
      AND c.provider = 'local_files' AND c.status IN ('pending', 'active') AND c.deleted_at IS NULL
      AND w.status = 'active' AND m.status = 'active'
      AND u.status = 'active' AND u.email_verified_at IS NOT NULL;
$$;

CREATE FUNCTION app.claim_device_heartbeat(
    requested_device UUID, credential BYTEA, proved_key BYTEA,
    signed_at TIMESTAMPTZ, nonce_digest BYTEA
)
RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    authority RECORD;
    claim_time TIMESTAMPTZ;
BEGIN
    IF requested_device IS NULL OR credential IS NULL OR octet_length(credential) <> 32
        OR proved_key IS NULL OR octet_length(proved_key) <> 32
        OR nonce_digest IS NULL OR octet_length(nonce_digest) <> 32
        OR signed_at IS NULL OR NOT isfinite(signed_at)
    THEN
        RETURN 'rejected';
    END IF;

    SELECT d.workspace_id, d.last_seen_at INTO authority
    FROM app.devices AS d
    JOIN app.connections AS c
      ON c.workspace_id = d.workspace_id AND c.id = d.connection_id
     AND c.owner_user_id = d.user_id
    JOIN app.workspaces AS w ON w.id = d.workspace_id
    JOIN app.workspace_members AS m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users AS u ON u.id = m.user_id
    WHERE d.id = requested_device AND d.credential_hash = credential
      AND d.public_key = proved_key
      AND d.status = 'active' AND d.revoked_at IS NULL
      AND c.provider = 'local_files' AND c.status IN ('pending', 'active') AND c.deleted_at IS NULL
      AND w.status = 'active' AND m.status = 'active'
      AND u.status = 'active' AND u.email_verified_at IS NOT NULL
    FOR UPDATE OF d FOR SHARE OF c, w, m, u;
    IF NOT FOUND THEN
        RETURN 'rejected';
    END IF;

    -- Take the authoritative clock after obtaining the locks. TTL and durable
    -- nonce claim MUST happen before updating last_seen_at.
    claim_time := clock_timestamp();
    IF signed_at < claim_time - INTERVAL '5 minutes'
        OR signed_at > claim_time + INTERVAL '5 minutes'
    THEN
        RETURN 'rejected';
    END IF;
    IF EXISTS (
        SELECT 1 FROM app.device_request_nonces AS n
        WHERE n.device_id = requested_device AND n.nonce_hash = nonce_digest
          AND n.expires_at >= claim_time
    ) THEN
        RETURN 'rejected';
    END IF;
    IF authority.last_seen_at > claim_time - INTERVAL '10 seconds' THEN
        RETURN 'rate_limited';
    END IF;

    -- At most 67 live nonce rows per heartbeat-only device: accepted every
    -- 10 seconds with a maximum 11-minute retention, including clock margin.
    -- Inactive devices retain at most that many rows, all cascade on deletion.
    DELETE FROM app.device_request_nonces AS n
    WHERE n.device_id = requested_device AND n.expires_at < claim_time;
    INSERT INTO app.device_request_nonces (workspace_id, device_id, nonce_hash, expires_at)
    VALUES (
        authority.workspace_id, requested_device, nonce_digest,
        signed_at + INTERVAL '6 minutes'
    )
    ON CONFLICT (device_id, nonce_hash) DO NOTHING;
    IF NOT FOUND THEN
        RETURN 'rejected';
    END IF;
    UPDATE app.devices AS d SET last_seen_at = claim_time
    WHERE d.id = requested_device AND d.workspace_id = authority.workspace_id;
    RETURN 'accepted';
END
$$;

ALTER FUNCTION app.lookup_device_heartbeat(UUID, BYTEA) OWNER TO app_migrator;
ALTER FUNCTION app.claim_device_heartbeat(UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA)
    OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.lookup_device_heartbeat(UUID, BYTEA)
    FROM PUBLIC, app_worker, app_audit_reader;
REVOKE ALL ON FUNCTION app.claim_device_heartbeat(UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA)
    FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.lookup_device_heartbeat(UUID, BYTEA) TO app_api;
GRANT EXECUTE ON FUNCTION app.claim_device_heartbeat(UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA)
    TO app_api;
