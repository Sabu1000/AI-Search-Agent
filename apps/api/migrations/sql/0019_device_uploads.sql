SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- Reservations account for objects that may still exist. Expiry invalidates the
-- signing authorization only, not the reservation or storage obligation.
-- Do not cascade from mutable folder/manifest grants. A future trusted
-- reconciliation/completion path must settle the reserve after checking the
-- provider; no application roles bypass this narrow issuance boundary.
CREATE TABLE app.device_upload_reservations (
    upload_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES app.workspaces(id) ON DELETE CASCADE,
    device_id UUID NOT NULL,
    folder_id UUID NOT NULL,
    manifest_id UUID NOT NULL,
    external_id TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    size_bytes BIGINT NOT NULL,
    content_sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_device_upload_size CHECK (size_bytes BETWEEN 0 AND 100000000),
    CONSTRAINT ck_device_upload_ext CHECK (external_id ~ '^[A-Za-z0-9_-]{1,128}$'),
    CONSTRAINT ck_device_upload_digest CHECK (content_sha256 ~ '^[A-Za-z0-9_-]{43}$'),
    CONSTRAINT ck_device_upload_mime CHECK (mime_type IN ('text/markdown','text/plain','text/csv')),
    CONSTRAINT ck_device_upload_object_key CHECK (
        object_key ~ '^device-inbox/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    ),
    CONSTRAINT ck_device_upload_expiry CHECK (
        isfinite(issued_at) AND isfinite(expires_at) AND expires_at = issued_at + INTERVAL '10 minutes'
    )
);
CREATE INDEX ix_device_upload_reservations_workspace
    ON app.device_upload_reservations(workspace_id);
ALTER TABLE app.device_upload_reservations OWNER TO app_migrator;
ALTER TABLE app.device_upload_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.device_upload_reservations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON app.device_upload_reservations FROM PUBLIC, app_api, app_worker, app_audit_reader;

CREATE FUNCTION app.reserve_device_upload(
    requested_device UUID, credential BYTEA, proved_key BYTEA, signed_at TIMESTAMPTZ,
    nonce_digest BYTEA, requested_folder UUID, requested_manifest UUID, requested_upload UUID,
    requested_external_id TEXT, requested_size BIGINT, requested_digest TEXT, requested_mime TEXT
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    authority RECORD;
    usage RECORD;
    grant_row RECORD;
    claim_time TIMESTAMPTZ;
    reserved_bytes BIGINT;
    reserved_sources BIGINT;
    intent JSONB;
BEGIN
    IF requested_device IS NULL OR credential IS NULL OR octet_length(credential) <> 32
        OR proved_key IS NULL OR octet_length(proved_key) <> 32
        OR nonce_digest IS NULL OR octet_length(nonce_digest) <> 32
        OR requested_upload IS NULL OR requested_folder IS NULL OR requested_manifest IS NULL
        OR requested_external_id IS NULL OR requested_external_id !~ '^[A-Za-z0-9_-]{1,128}$'
        OR requested_digest IS NULL OR requested_digest !~ '^[A-Za-z0-9_-]{43}$'
        OR requested_size IS NULL OR requested_size NOT BETWEEN 0 AND 100000000
        OR requested_mime IS NULL OR requested_mime NOT IN ('text/markdown', 'text/plain', 'text/csv')
        OR signed_at IS NULL OR NOT isfinite(signed_at)
    THEN
        RETURN '{"status":"rejected"}';
    END IF;

    SELECT d.workspace_id INTO authority
    FROM app.devices AS d
    JOIN app.connections AS c
      ON c.workspace_id = d.workspace_id AND c.id = d.connection_id AND c.owner_user_id = d.user_id
    JOIN app.workspaces AS w ON w.id = d.workspace_id
    JOIN app.workspace_members AS m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users AS u ON u.id = m.user_id
    JOIN app.device_folders AS f ON f.workspace_id = d.workspace_id AND f.device_id = d.id
    WHERE d.id = requested_device AND d.credential_hash = credential AND d.public_key = proved_key
      AND d.status = 'active' AND d.revoked_at IS NULL
      AND c.provider = 'local_files' AND c.status = 'active' AND c.deleted_at IS NULL
      AND w.status = 'active' AND m.status = 'active'
      AND u.status = 'active' AND u.email_verified_at IS NOT NULL
      AND f.id = requested_folder AND f.status = 'active'
    FOR UPDATE OF d, f FOR SHARE OF c, w, m, u;
    IF NOT FOUND THEN
        RETURN '{"status":"rejected"}';
    END IF;

    SELECT entry INTO intent
    FROM app.device_manifest_pages AS p
    CROSS JOIN LATERAL jsonb_array_elements(p.upload_intents) AS ue(entry)
    WHERE p.workspace_id = authority.workspace_id AND p.device_id = requested_device
      AND p.manifest_id = requested_manifest AND p.folder_id = requested_folder
      AND entry->>'upload_id' = requested_upload::TEXT;
    IF NOT FOUND OR intent->>'external_id' IS DISTINCT FROM requested_external_id
        OR intent->'size_bytes' IS DISTINCT FROM to_jsonb(requested_size)
        OR intent->>'content_sha256' IS DISTINCT FROM requested_digest
        OR intent->>'mime_type' IS DISTINCT FROM requested_mime
    THEN
        RETURN '{"status":"rejected"}';
    END IF;

    -- Serializes quota decisions across different devices/folders in the same
    -- workspace and with index usage accounting. Required even on retries to
    -- obtain the trusted clock after all potentially blocking locks.
    SELECT q.indexed_source_count, q.extracted_bytes INTO usage
    FROM app.workspace_usage AS q WHERE q.workspace_id = authority.workspace_id
    FOR UPDATE OF q;
    IF NOT FOUND THEN
        RETURN '{"status":"unavailable"}';
    END IF;
    claim_time := clock_timestamp();
    IF signed_at NOT BETWEEN claim_time - INTERVAL '5 minutes'
                         AND claim_time + INTERVAL '5 minutes'
    THEN
        RETURN '{"status":"rejected"}';
    END IF;
    DELETE FROM app.device_request_nonces AS n
    WHERE n.device_id = requested_device AND n.expires_at < claim_time;
    IF EXISTS (
        SELECT 1 FROM app.device_request_nonces AS n
        WHERE n.device_id = requested_device AND n.nonce_hash = nonce_digest
    ) THEN
        RETURN '{"status":"rejected"}';
    END IF;
    IF (SELECT count(*) FROM app.device_request_nonces AS n
        WHERE n.device_id = requested_device) >= 120 THEN
        RETURN '{"status":"rate_limited"}';
    END IF;
    INSERT INTO app.device_request_nonces(workspace_id, device_id, nonce_hash, expires_at)
    VALUES (authority.workspace_id, requested_device, nonce_digest, signed_at + INTERVAL '6 minutes')
    ON CONFLICT (device_id, nonce_hash) DO NOTHING;
    IF NOT FOUND THEN
        RETURN '{"status":"rejected"}';
    END IF;

    SELECT r.* INTO grant_row FROM app.device_upload_reservations AS r
    WHERE r.upload_id = requested_upload;
    IF FOUND THEN
        IF grant_row.workspace_id IS DISTINCT FROM authority.workspace_id
            OR grant_row.device_id IS DISTINCT FROM requested_device
            OR grant_row.folder_id IS DISTINCT FROM requested_folder
            OR grant_row.manifest_id IS DISTINCT FROM requested_manifest
            OR grant_row.external_id IS DISTINCT FROM requested_external_id
            OR grant_row.size_bytes IS DISTINCT FROM requested_size
            OR grant_row.content_sha256 IS DISTINCT FROM requested_digest
            OR grant_row.mime_type IS DISTINCT FROM requested_mime
        THEN
            RETURN '{"status":"rejected"}';
        END IF;
        IF grant_row.expires_at <= claim_time THEN
            RETURN '{"status":"expired"}';
        END IF;
    ELSE
        SELECT count(*), COALESCE(sum(r.size_bytes), 0)
        INTO reserved_sources, reserved_bytes
        FROM app.device_upload_reservations AS r WHERE r.workspace_id = authority.workspace_id;
        IF usage.indexed_source_count + reserved_sources >= 25000
            OR usage.extracted_bytes + reserved_bytes + requested_size > 10737418240
        THEN
            RETURN '{"status":"quota"}';
        END IF;
        INSERT INTO app.device_upload_reservations (
            upload_id, workspace_id, device_id, folder_id, manifest_id, external_id,
            object_key, size_bytes, content_sha256, mime_type, issued_at, expires_at
        ) VALUES (
            requested_upload, authority.workspace_id, requested_device, requested_folder,
            requested_manifest, requested_external_id,
            'device-inbox/' || gen_random_uuid()::TEXT, requested_size, requested_digest,
            requested_mime, claim_time, claim_time + INTERVAL '10 minutes'
        )
        RETURNING * INTO grant_row;
    END IF;

    RETURN jsonb_build_object(
        'status', 'authorized', 'upload_id', grant_row.upload_id,
        'workspace_id', grant_row.workspace_id, 'object_key', grant_row.object_key,
        'size_bytes', grant_row.size_bytes, 'content_sha256', grant_row.content_sha256,
        'mime_type', grant_row.mime_type, 'issued_at', grant_row.issued_at,
        'expires_at', grant_row.expires_at
    );
END
$$;

ALTER FUNCTION app.reserve_device_upload(
    UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA, UUID, UUID, UUID, TEXT, BIGINT, TEXT, TEXT
) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.reserve_device_upload(
    UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA, UUID, UUID, UUID, TEXT, BIGINT, TEXT, TEXT
) FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.reserve_device_upload(
    UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA, UUID, UUID, UUID, TEXT, BIGINT, TEXT, TEXT
) TO app_api;
