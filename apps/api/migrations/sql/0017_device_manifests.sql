SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- One immutable request/result per accepted page. No raw body, original path,
-- file content or URL is persisted. Folder/device erasure cascades to receipts.
ALTER TABLE app.device_folders
    ADD CONSTRAINT uq_device_folders_workspace_device_id UNIQUE (workspace_id, device_id, id);
CREATE TABLE app.device_manifest_pages (
    workspace_id UUID NOT NULL,
    device_id UUID NOT NULL,
    folder_id UUID NOT NULL,
    manifest_id UUID NOT NULL,
    sequence BIGINT NOT NULL,
    final BOOLEAN NOT NULL,
    body_hash BYTEA NOT NULL,
    upload_intents JSONB NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, manifest_id),
    UNIQUE (folder_id, sequence),
    FOREIGN KEY (workspace_id, device_id, folder_id)
        REFERENCES app.device_folders (workspace_id, device_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_device_manifest_pages_sequence CHECK (sequence BETWEEN 1 AND 9007199254740990),
    CONSTRAINT ck_device_manifest_pages_body_hash CHECK (octet_length(body_hash) = 32),
    CONSTRAINT ck_device_manifest_pages_intents CHECK (
        jsonb_typeof(upload_intents) = 'array' AND jsonb_array_length(upload_intents) <= 1000
    ),
    CONSTRAINT ck_device_manifest_pages_response CHECK (jsonb_typeof(response) = 'object')
);
ALTER TABLE app.device_manifest_pages OWNER TO app_migrator;
ALTER TABLE app.device_manifest_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.device_manifest_pages FORCE ROW LEVEL SECURITY;
REVOKE ALL ON app.device_manifest_pages FROM PUBLIC, app_api, app_worker, app_audit_reader;

CREATE FUNCTION app.lookup_device_manifest(requested_device UUID, credential BYTEA)
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
      AND c.provider = 'local_files' AND c.status = 'active' AND c.deleted_at IS NULL
      AND w.status = 'active' AND m.status = 'active'
      AND u.status = 'active' AND u.email_verified_at IS NOT NULL;
$$;

CREATE FUNCTION app.commit_device_manifest(
    requested_device UUID, credential BYTEA, proved_key BYTEA, signed_at TIMESTAMPTZ,
    nonce_digest BYTEA, exact_body_hash BYTEA, requested_folder UUID, requested_manifest UUID,
    requested_sequence BIGINT, is_final BOOLEAN, accepted_entries JSONB, skipped_entries JSONB
)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    authority RECORD;
    prior_page RECORD;
    claim_time TIMESTAMPTZ;
    expected BIGINT;
    intents JSONB;
    uploads JSONB;
    result JSONB;
BEGIN
    IF requested_device IS NULL OR credential IS NULL OR octet_length(credential) <> 32
        OR proved_key IS NULL OR octet_length(proved_key) <> 32
        OR nonce_digest IS NULL OR octet_length(nonce_digest) <> 32
        OR exact_body_hash IS NULL OR octet_length(exact_body_hash) <> 32
        OR requested_manifest IS NULL OR requested_folder IS NULL
        OR requested_sequence IS NULL OR requested_sequence NOT BETWEEN 1 AND 9007199254740990
        OR signed_at IS NULL OR NOT isfinite(signed_at) OR is_final IS NULL
        OR jsonb_typeof(accepted_entries) IS DISTINCT FROM 'array'
        OR jsonb_typeof(skipped_entries) IS DISTINCT FROM 'array'
    THEN
        RETURN '{"status":"rejected"}';
    END IF;
    IF jsonb_array_length(accepted_entries) + jsonb_array_length(skipped_entries) > 1000
        OR octet_length(accepted_entries::TEXT) > 2000000
        OR octet_length(skipped_entries::TEXT) > 2000000
    THEN
        RETURN '{"status":"rejected"}';
    END IF;

    SELECT d.workspace_id INTO authority
    FROM app.devices AS d
    JOIN app.connections AS c
      ON c.workspace_id = d.workspace_id AND c.id = d.connection_id
      AND c.owner_user_id = d.user_id
    JOIN app.workspaces AS w ON w.id = d.workspace_id
    JOIN app.workspace_members AS m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users AS u ON u.id = m.user_id
    JOIN app.device_folders AS f
      ON f.workspace_id = d.workspace_id AND f.device_id = d.id
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
    -- A shared durable per-device cap limits authenticated replay-state growth.
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

    SELECT COALESCE(MAX(p.sequence), 0) + 1 INTO expected
    FROM app.device_manifest_pages AS p WHERE p.folder_id = requested_folder;
    SELECT p.folder_id, p.body_hash, p.response INTO prior_page
    FROM app.device_manifest_pages AS p
    WHERE p.device_id = requested_device AND p.manifest_id = requested_manifest;
    IF FOUND THEN
        IF prior_page.folder_id = requested_folder AND prior_page.body_hash = exact_body_hash THEN
            RETURN jsonb_build_object('status', 'accepted', 'page', prior_page.response);
        END IF;
        RETURN jsonb_build_object('status', 'conflict', 'next_sequence', expected);
    END IF;
    IF requested_sequence <> expected THEN
        RETURN jsonb_build_object('status', 'conflict', 'next_sequence', expected);
    END IF;

    SELECT COALESCE(jsonb_agg(e.data || jsonb_build_object('upload_id', gen_random_uuid())
                              ORDER BY e.ordinal), '[]'::JSONB)
    INTO intents FROM jsonb_array_elements(accepted_entries) WITH ORDINALITY AS e(data, ordinal);
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'external_id', e.data->'external_id', 'upload_id', e.data->'upload_id'
           ) ORDER BY e.ordinal), '[]'::JSONB)
    INTO uploads FROM jsonb_array_elements(intents) WITH ORDINALITY AS e(data, ordinal);

    -- Final is an informational page marker, not permission to infer deletion.
    result := jsonb_build_object(
        'requested_uploads', uploads, 'acknowledged_deletions', '[]'::JSONB,
        'skipped_entries', skipped_entries, 'next_sequence', expected + 1
    );
    INSERT INTO app.device_manifest_pages(
        workspace_id, device_id, folder_id, manifest_id, sequence, final,
        body_hash, upload_intents, response
    ) VALUES (
        authority.workspace_id, requested_device, requested_folder, requested_manifest,
        requested_sequence, is_final, exact_body_hash, intents, result
    );
    RETURN jsonb_build_object('status', 'accepted', 'page', result);
END
$$;
ALTER FUNCTION app.lookup_device_manifest(UUID, BYTEA) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.lookup_device_manifest(UUID, BYTEA)
    FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.lookup_device_manifest(UUID, BYTEA) TO app_api;
ALTER FUNCTION app.commit_device_manifest(
    UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA, BYTEA, UUID, UUID, BIGINT, BOOLEAN, JSONB, JSONB
) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.commit_device_manifest(
    UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA, BYTEA, UUID, UUID, BIGINT, BOOLEAN, JSONB, JSONB
) FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.commit_device_manifest(
    UUID, BYTEA, BYTEA, TIMESTAMPTZ, BYTEA, BYTEA, UUID, UUID, BIGINT, BOOLEAN, JSONB, JSONB
) TO app_api;
