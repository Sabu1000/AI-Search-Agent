SET LOCAL lock_timeout = '5s';

-- No provider paths, file names, credentials, or content are exposed by this
-- receipt. The original quota reservation remains: an object may still exist.
CREATE TABLE app.device_upload_changes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES app.workspaces(id) ON DELETE CASCADE,
    device_id UUID NOT NULL,
    batch_id UUID NOT NULL,
    upload_id UUID NOT NULL UNIQUE
        REFERENCES app.device_upload_reservations(upload_id) ON DELETE CASCADE,
    nonce_hash BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    verification_started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    immutable_version TEXT,
    UNIQUE (device_id,batch_id),
    CONSTRAINT ck_device_change_nonce CHECK (octet_length(nonce_hash) = 32),
    CONSTRAINT ck_device_change_result CHECK (
        (completed_at IS NULL AND immutable_version IS NULL)
        OR (completed_at IS NOT NULL AND completed_at >= verification_started_at
            AND immutable_version IS NOT NULL AND immutable_version <> 'null'
            AND length(immutable_version) BETWEEN 1 AND 256
            AND immutable_version ~ '^[A-Za-z0-9+=/_~-]+$')
    ),
    CONSTRAINT ck_device_change_timestamps CHECK (verification_started_at >= created_at)
);
ALTER TABLE app.device_upload_changes OWNER TO app_migrator;
ALTER TABLE app.device_upload_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.device_upload_changes FORCE ROW LEVEL SECURITY;
REVOKE ALL ON app.device_upload_changes FROM PUBLIC, app_api, app_worker, app_audit_reader;

CREATE FUNCTION app.device_upload_change(
    requested_device UUID, credential BYTEA, proved_key BYTEA, signed_at TIMESTAMPTZ,
    nonce_digest BYTEA, requested_batch UUID, requested_upload UUID,
    verified_version TEXT, verified_size BIGINT, verified_digest TEXT, verified_mime TEXT
)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    reserved app.device_upload_reservations%ROWTYPE;
    change app.device_upload_changes%ROWTYPE;
    checked TIMESTAMPTZ;
BEGIN
    IF requested_device IS NULL OR requested_batch IS NULL OR requested_upload IS NULL
        OR octet_length(credential) IS DISTINCT FROM 32
        OR octet_length(proved_key) IS DISTINCT FROM 32
        OR octet_length(nonce_digest) IS DISTINCT FROM 32
        OR signed_at IS NULL OR NOT isfinite(signed_at)
        OR (verified_version IS NULL AND
            (verified_size IS NOT NULL OR verified_digest IS NOT NULL OR verified_mime IS NOT NULL))
        OR (verified_version IS NOT NULL AND
            (verified_version = 'null' OR length(verified_version) NOT BETWEEN 1 AND 256
             OR verified_version !~ '^[A-Za-z0-9+=/_~-]+$'))
    THEN RETURN '{"status":"rejected"}'; END IF;

    SELECT r.* INTO reserved
    FROM app.device_upload_reservations r
    JOIN app.devices d ON d.id = requested_device AND d.workspace_id = r.workspace_id
                      AND d.id = r.device_id
    JOIN app.connections c ON c.workspace_id = d.workspace_id AND c.id = d.connection_id
                          AND c.owner_user_id = d.user_id
    JOIN app.workspaces w ON w.id = d.workspace_id
    JOIN app.workspace_members m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users u ON u.id = m.user_id
    JOIN app.device_folders f ON f.workspace_id = d.workspace_id AND f.device_id = d.id
                             AND f.id = r.folder_id
    WHERE r.upload_id = requested_upload
      AND d.credential_hash = credential AND d.public_key = proved_key
      AND d.status = 'active' AND d.revoked_at IS NULL
      AND c.provider = 'local_files' AND c.status = 'active' AND c.deleted_at IS NULL
      AND w.status = 'active' AND m.status = 'active' AND u.status = 'active'
      AND u.email_verified_at IS NOT NULL AND f.status = 'active'
    FOR UPDATE OF d, f FOR SHARE OF c, w, m, u, r;
    IF NOT FOUND THEN RETURN '{"status":"rejected"}'; END IF;

    PERFORM 1 FROM app.device_manifest_pages p
    CROSS JOIN LATERAL jsonb_array_elements(p.upload_intents) AS ue(entry)
    WHERE p.workspace_id = reserved.workspace_id AND p.device_id = requested_device
      AND p.manifest_id = reserved.manifest_id AND p.folder_id = reserved.folder_id
      AND entry->>'upload_id' = requested_upload::TEXT
      AND entry->>'external_id' = reserved.external_id
      AND entry->'size_bytes' = to_jsonb(reserved.size_bytes)
      AND entry->>'content_sha256' = reserved.content_sha256
      AND entry->>'mime_type' = reserved.mime_type;
    IF NOT FOUND THEN RETURN '{"status":"rejected"}'; END IF;

    checked := clock_timestamp();
    IF signed_at NOT BETWEEN checked - INTERVAL '5 minutes' AND checked + INTERVAL '5 minutes'
    THEN RETURN '{"status":"rejected"}'; END IF;
    SELECT * INTO change FROM app.device_upload_changes x
    WHERE x.device_id = requested_device AND x.batch_id = requested_batch
    FOR UPDATE;
    IF verified_version IS NULL THEN
        DELETE FROM app.device_request_nonces n
        WHERE n.device_id = requested_device AND n.expires_at < checked;
        IF EXISTS (SELECT FROM app.device_request_nonces n
                   WHERE n.device_id = requested_device AND n.nonce_hash = nonce_digest)
        THEN RETURN '{"status":"rejected"}'; END IF;
        IF (SELECT count(*) FROM app.device_request_nonces n
            WHERE n.device_id = requested_device) >= 120
        THEN RETURN '{"status":"rate_limited"}'; END IF;
        INSERT INTO app.device_request_nonces(workspace_id,device_id,nonce_hash,expires_at)
        VALUES (reserved.workspace_id,requested_device,nonce_digest,signed_at+INTERVAL '6 minutes')
        ON CONFLICT (device_id,nonce_hash) DO NOTHING;
        IF NOT FOUND THEN RETURN '{"status":"rejected"}'; END IF;

        IF change.id IS NOT NULL AND change.upload_id <> requested_upload
        THEN RETURN '{"status":"conflict"}'; END IF;
        IF change.completed_at IS NOT NULL THEN
            RETURN jsonb_build_object('status','verified','id',change.id,
                'created_at',change.created_at,'completed_at',change.completed_at);
        END IF;
        IF reserved.expires_at + INTERVAL '5 minutes' <= checked
        THEN RETURN '{"status":"expired"}'; END IF;
        IF change.id IS NULL THEN
            INSERT INTO app.device_upload_changes (
                workspace_id,device_id,batch_id,upload_id,nonce_hash,created_at,
                verification_started_at)
            VALUES (reserved.workspace_id,requested_device,requested_batch,requested_upload,
                   nonce_digest,checked,checked)
            ON CONFLICT DO NOTHING RETURNING * INTO change;
            IF NOT FOUND THEN RETURN '{"status":"conflict"}'; END IF;
        ELSIF change.verification_started_at > checked - INTERVAL '20 seconds' THEN
            RETURN '{"status":"busy"}';
        ELSE
            UPDATE app.device_upload_changes x SET
                verification_started_at = checked, nonce_hash = nonce_digest
            WHERE x.id = change.id;
        END IF;
        RETURN jsonb_build_object('status','check','upload_id',reserved.upload_id,
            'object_key',reserved.object_key,'size_bytes',reserved.size_bytes,
            'content_sha256',reserved.content_sha256,'mime_type',reserved.mime_type);
    END IF;

    IF change.id IS NULL OR change.upload_id <> requested_upload
        OR change.completed_at IS NOT NULL
        OR change.nonce_hash <> nonce_digest
        OR change.verification_started_at <= checked - INTERVAL '20 seconds'
        OR reserved.expires_at + INTERVAL '5 minutes' <= checked
        OR reserved.size_bytes IS DISTINCT FROM verified_size
        OR reserved.content_sha256 IS DISTINCT FROM verified_digest
        OR reserved.mime_type IS DISTINCT FROM verified_mime
    THEN RETURN '{"status":"rejected"}'; END IF;
    UPDATE app.device_upload_changes x SET completed_at = checked,
        immutable_version = verified_version
    WHERE x.id = change.id;
    RETURN jsonb_build_object('status','verified','id',change.id,
        'created_at',change.created_at,'completed_at',checked);
END
$$;
ALTER FUNCTION app.device_upload_change(
    UUID,BYTEA,BYTEA,TIMESTAMPTZ,BYTEA,UUID,UUID,TEXT,BIGINT,TEXT,TEXT) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.device_upload_change(
    UUID,BYTEA,BYTEA,TIMESTAMPTZ,BYTEA,UUID,UUID,TEXT,BIGINT,TEXT,TEXT)
    FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.device_upload_change(
    UUID,BYTEA,BYTEA,TIMESTAMPTZ,BYTEA,UUID,UUID,TEXT,BIGINT,TEXT,TEXT) TO app_api;

CREATE FUNCTION app.read_device_upload_change(
    operation_id UUID, selected_workspace UUID, actor UUID, selected_session UUID,
    user_version BIGINT, workspace_version BIGINT
)
RETURNS JSONB LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
    SELECT jsonb_build_object('status','verified','id',x.id,
        'created_at',x.created_at,'completed_at',x.completed_at)
    FROM app.device_upload_changes x
    JOIN app.devices d ON d.workspace_id = x.workspace_id AND d.id = x.device_id
    JOIN app.connections c ON c.workspace_id = d.workspace_id AND c.id = d.connection_id
                          AND c.owner_user_id = d.user_id
    JOIN app.workspaces w ON w.id = d.workspace_id
    JOIN app.workspace_members m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users u ON u.id = m.user_id
    JOIN app.sessions s ON s.user_id = u.id
    WHERE x.id = operation_id AND x.workspace_id = selected_workspace
      AND x.completed_at IS NOT NULL AND u.id = actor AND s.id = selected_session
      AND u.id = app.current_user_id() AND w.id = app.current_workspace_id()
      AND u.lock_version = user_version AND w.authorization_version = workspace_version
      AND u.status = 'active' AND u.email_verified_at IS NOT NULL AND m.status = 'active'
      AND w.status = 'active' AND d.status = 'active' AND d.revoked_at IS NULL
      AND c.provider = 'local_files' AND c.status = 'active' AND c.deleted_at IS NULL
      AND s.authorization_version = user_version AND s.revoked_at IS NULL
      AND s.rotated_at IS NULL AND s.expires_at > clock_timestamp()
$$;
ALTER FUNCTION app.read_device_upload_change(UUID,UUID,UUID,UUID,BIGINT,BIGINT)
    OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.read_device_upload_change(UUID,UUID,UUID,UUID,BIGINT,BIGINT)
    FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.read_device_upload_change(UUID,UUID,UUID,UUID,BIGINT,BIGINT)
    TO app_api;
