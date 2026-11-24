SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

ALTER TABLE app.device_upload_changes
    ADD COLUMN index_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN index_attempt INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN index_lease_owner TEXT,
    ADD COLUMN index_lease_until TIMESTAMPTZ,
    ADD COLUMN index_available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN index_started_at TIMESTAMPTZ,
    ADD COLUMN index_finished_at TIMESTAMPTZ,
    ADD COLUMN index_error_code TEXT,
    ADD COLUMN index_job_id UUID,
    ADD CONSTRAINT ck_device_upload_index_state CHECK (
      index_status IN ('pending','leased','retry_wait','queued','failed')
      AND index_attempt BETWEEN 0 AND 3
      AND ((index_status = 'leased' AND index_lease_owner IS NOT NULL
             AND index_lease_until IS NOT NULL)
        OR (index_status <> 'leased' AND index_lease_owner IS NULL
             AND index_lease_until IS NULL))
      AND ((index_status = 'queued') = (index_job_id IS NOT NULL))
      AND ((index_status IN ('queued','failed')) = (index_finished_at IS NOT NULL))
      AND (index_status = 'pending' OR completed_at IS NOT NULL)
    ),
    ADD CONSTRAINT ck_device_upload_index_job FOREIGN KEY (workspace_id,index_job_id)
        REFERENCES app.jobs(workspace_id,id) ON DELETE CASCADE;
CREATE UNIQUE INDEX uq_device_upload_index_job ON app.device_upload_changes(index_job_id)
    WHERE index_job_id IS NOT NULL;
CREATE INDEX ix_device_upload_index_ready
    ON app.device_upload_changes(index_available_at,completed_at,id)
    WHERE completed_at IS NOT NULL AND index_status IN ('pending','leased','retry_wait');

CREATE FUNCTION app.claim_verified_device_upload(requested_worker TEXT)
RETURNS TABLE (operation_id UUID, workspace_id UUID, attempt INTEGER)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    checked TIMESTAMPTZ := clock_timestamp();
    selected UUID;
BEGIN
    IF requested_worker IS NULL OR char_length(requested_worker) NOT BETWEEN 1 AND 255
    THEN RETURN; END IF;
    -- A crashed final attempt is terminal; bounded per polling transaction.
    WITH expired AS (
        SELECT x.id FROM app.device_upload_changes x
        WHERE x.index_status = 'leased' AND x.index_attempt = 3
          AND x.index_lease_until <= checked
        ORDER BY x.index_lease_until,x.id LIMIT 25 FOR UPDATE SKIP LOCKED
    )
    UPDATE app.device_upload_changes x SET index_status = 'failed',
        index_lease_owner = NULL,index_lease_until = NULL,index_finished_at = checked,
        index_error_code = 'UPLOAD_INDEX_MAX_ATTEMPTS'
    WHERE x.id IN (SELECT id FROM expired);

    SELECT x.id INTO selected FROM app.device_upload_changes x
    WHERE x.completed_at IS NOT NULL AND x.index_attempt < 3
      AND ((x.index_status IN ('pending','retry_wait') AND x.index_available_at <= checked)
        OR (x.index_status = 'leased' AND x.index_lease_until <= checked))
    ORDER BY x.index_available_at,x.completed_at,x.id LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN; END IF;
    RETURN QUERY UPDATE app.device_upload_changes x
    SET index_status = 'leased', index_attempt = x.index_attempt + 1,
        index_lease_owner = requested_worker,index_lease_until = checked + INTERVAL '60 seconds',
        index_started_at = COALESCE(x.index_started_at,checked),index_error_code = NULL
    WHERE x.id = selected RETURNING x.id,x.workspace_id,x.index_attempt;
END
$$;

CREATE FUNCTION app.load_verified_device_upload(
    operation UUID, workspace_id_arg UUID, requested_worker TEXT, requested_attempt INTEGER,
    publishing BOOLEAN
)
RETURNS TABLE (
    upload_id UUID, device_id UUID, folder_id UUID, connection_id UUID, owner_id UUID,
    external_id TEXT, object_key TEXT, immutable_version TEXT, size_bytes BIGINT,
    content_sha256 TEXT, mime_type TEXT, manifest_sequence BIGINT
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
BEGIN
    IF publishing IS NULL THEN RETURN; END IF;
    IF publishing THEN
        -- Linearize with revocation, search generation, and the real index
        -- worker. No publication lock is held through the versioned GET.
        PERFORM w.id FROM app.workspaces w
        WHERE w.id = workspace_id_arg AND w.status = 'active'
        FOR NO KEY UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;

    RETURN QUERY
    SELECT r.upload_id,d.id,f.id,c.id,u.id,
           r.external_id,r.object_key,x.immutable_version,r.size_bytes,
           r.content_sha256,r.mime_type,p.sequence
    FROM app.device_upload_changes x
    JOIN app.device_upload_reservations r ON r.upload_id = x.upload_id
      AND r.workspace_id = x.workspace_id AND r.device_id = x.device_id
    JOIN app.devices d ON d.id = x.device_id AND d.workspace_id = x.workspace_id
    JOIN app.connections c ON c.id = d.connection_id AND c.workspace_id = d.workspace_id
      AND c.owner_user_id = d.user_id
    JOIN app.workspaces w ON w.id = d.workspace_id
    JOIN app.workspace_members m ON m.workspace_id = d.workspace_id AND m.user_id = d.user_id
    JOIN app.users u ON u.id = m.user_id
    JOIN app.device_folders f ON f.id = r.folder_id AND f.workspace_id = r.workspace_id
      AND f.device_id = d.id
    JOIN app.device_manifest_pages p ON p.workspace_id = r.workspace_id
      AND p.device_id = d.id AND p.folder_id = f.id AND p.manifest_id = r.manifest_id
    WHERE x.id = operation AND x.workspace_id = workspace_id_arg
      AND x.completed_at IS NOT NULL AND x.immutable_version IS NOT NULL
      AND x.index_status = 'leased' AND x.index_attempt = requested_attempt
      AND x.index_lease_owner = requested_worker AND x.index_lease_until > clock_timestamp()
      AND w.status = 'active' AND u.status = 'active' AND u.email_verified_at IS NOT NULL
      AND m.status = 'active' AND d.status = 'active' AND d.revoked_at IS NULL
      AND c.provider = 'local_files' AND c.status = 'active' AND c.deleted_at IS NULL
      AND f.status = 'active'
      AND NOT EXISTS (
          SELECT 1 FROM app.devices other
          WHERE other.workspace_id = d.workspace_id AND other.connection_id = c.id
            AND other.id <> d.id
      )
      AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(p.upload_intents) AS ue(entry)
          WHERE entry->>'upload_id' = r.upload_id::TEXT
            AND entry->>'external_id' = r.external_id
            AND entry->'size_bytes' = to_jsonb(r.size_bytes)
            AND entry->>'content_sha256' = r.content_sha256
            AND entry->>'mime_type' = r.mime_type
      )
    FOR UPDATE OF x,d,f,c NOWAIT FOR SHARE OF w,m,u,r,p NOWAIT;
END
$$;

CREATE FUNCTION app.finish_verified_device_upload(
    operation UUID, workspace_id_arg UUID, requested_worker TEXT, requested_attempt INTEGER,
    confirmed_job UUID, failure TEXT, retryable BOOLEAN
)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    checked TIMESTAMPTZ := clock_timestamp();
    retry BOOLEAN := retryable AND requested_attempt < 3;
BEGIN
    IF retryable IS NULL OR (confirmed_job IS NULL) = (failure IS NULL)
       OR (confirmed_job IS NOT NULL AND retryable)
       OR (failure IS NOT NULL AND failure NOT IN (
           'UPLOAD_INDEX_UNSUPPORTED','UPLOAD_INDEX_DENIED','UPLOAD_INDEX_INVALID',
           'UPLOAD_INDEX_UNAVAILABLE','UPLOAD_INDEX_SUPERSEDED','UPLOAD_INDEX_SOURCE_REMOVED'
       ))
    THEN RETURN FALSE; END IF;

    IF confirmed_job IS NOT NULL THEN
        PERFORM 1 FROM app.device_upload_changes x
        JOIN app.device_upload_reservations r ON r.upload_id = x.upload_id
        JOIN app.devices d ON d.id = x.device_id AND d.workspace_id = x.workspace_id
        JOIN app.jobs j ON j.workspace_id = x.workspace_id AND j.id = confirmed_job
          AND j.connection_id = d.connection_id AND j.job_type = 'index' AND j.queue = 'index'
        JOIN app.sources s ON s.workspace_id = x.workspace_id AND s.id = j.source_id
          AND s.provider = 'local_files' AND s.connection_id = d.connection_id
          AND s.metadata->>'local_upload_id' = r.upload_id::text
          AND s.metadata->>'local_device_id' = d.id::text
          AND s.metadata->>'local_folder_id' = r.folder_id::text
        WHERE x.id = operation AND x.workspace_id = workspace_id_arg;
        IF NOT FOUND THEN RETURN FALSE; END IF;
    END IF;

    UPDATE app.device_upload_changes x SET
        index_status = CASE WHEN confirmed_job IS NOT NULL THEN 'queued'
                           WHEN retry THEN 'retry_wait' ELSE 'failed' END,
        index_job_id = confirmed_job, index_error_code = failure,
        index_lease_owner = NULL,index_lease_until = NULL,
        index_finished_at = CASE WHEN retry THEN NULL ELSE checked END,
        index_available_at = CASE WHEN retry THEN checked + INTERVAL '10 seconds'
                                  ELSE x.index_available_at END
    WHERE x.id = operation AND x.workspace_id = workspace_id_arg
      AND x.index_status = 'leased' AND x.index_attempt = requested_attempt
      AND x.index_lease_owner = requested_worker AND x.index_lease_until > checked;
    RETURN FOUND;
END
$$;

CREATE FUNCTION app.verify_staged_source_index_scope(
    selected_job UUID, workspace_id_arg UUID, publishing BOOLEAN
)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    target RECORD;
BEGIN
    IF publishing IS NULL THEN RETURN FALSE; END IF;
    SELECT j.id,s.metadata,j.payload,s.connection_id INTO target
    FROM app.jobs j JOIN app.sources s ON s.workspace_id = j.workspace_id AND s.id = j.source_id
    WHERE j.id = selected_job AND j.workspace_id = workspace_id_arg
      AND j.job_type = 'index' AND s.provider = 'local_files'
      AND s.metadata ? 'local_upload_id';
    -- Unrelated provider jobs still use their existing authorization paths.
    IF NOT FOUND THEN RETURN TRUE; END IF;
    IF publishing THEN
        PERFORM 1 FROM app.workspaces w WHERE w.id = workspace_id_arg AND w.status = 'active'
        FOR NO KEY UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN FALSE; END IF;
    END IF;
    PERFORM 1 FROM app.device_upload_changes x
    JOIN app.device_upload_reservations r ON r.upload_id = x.upload_id
      AND r.workspace_id = x.workspace_id AND r.device_id = x.device_id
    JOIN app.devices d ON d.id = x.device_id AND d.workspace_id = x.workspace_id
    JOIN app.device_folders f ON f.id = r.folder_id AND f.workspace_id = d.workspace_id
      AND f.device_id = d.id
    JOIN app.connections c ON c.id = d.connection_id AND c.workspace_id = d.workspace_id
      AND c.owner_user_id = d.user_id
    JOIN app.workspaces w ON w.id = x.workspace_id
    JOIN app.workspace_members m ON m.workspace_id = w.id AND m.user_id = d.user_id
    JOIN app.users u ON u.id = m.user_id
    JOIN app.device_manifest_pages p ON p.workspace_id = r.workspace_id
      AND p.device_id = d.id AND p.folder_id = f.id AND p.manifest_id = r.manifest_id
    WHERE x.workspace_id = workspace_id_arg AND x.index_status = 'queued'
      AND x.completed_at IS NOT NULL
      AND r.upload_id::text = target.metadata->>'local_upload_id'
      AND d.id::text = target.metadata->>'local_device_id'
      AND f.id::text = target.metadata->>'local_folder_id'
      AND p.sequence::text = target.metadata->>'local_manifest_sequence'
      AND (x.index_job_id = target.id OR target.payload ? 'source_reindex_actor')
      AND c.id = target.connection_id AND c.provider = 'local_files'
      AND c.status = 'active' AND c.deleted_at IS NULL
      AND w.status = 'active' AND u.status = 'active' AND u.email_verified_at IS NOT NULL
      AND m.status = 'active' AND d.status = 'active' AND d.revoked_at IS NULL
      AND f.status = 'active'
    FOR SHARE OF x,r,d,f,c,w,m,u,p NOWAIT;
    RETURN FOUND;
END
$$;

CREATE FUNCTION app.read_device_upload_index_operation(
    operation_id UUID, selected_workspace UUID, actor UUID, selected_session UUID,
    user_version BIGINT, workspace_version BIGINT
)
RETURNS JSONB LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
    SELECT jsonb_build_object('id',x.id,'created_at',x.created_at,
        'started_at',x.index_started_at,
        'completed_at',CASE WHEN x.index_status = 'queued' THEN j.completed_at
                            WHEN x.index_status = 'failed' THEN x.index_finished_at END,
        'status',CASE WHEN x.index_status = 'queued' THEN
                 CASE j.status WHEN 'leased' THEN 'running' WHEN 'dead_letter' THEN 'failed'
                               ELSE j.status END
                     WHEN x.index_status = 'leased' THEN 'running' ELSE x.index_status END,
        'error_code',CASE WHEN x.index_status = 'failed' THEN x.index_error_code
                          WHEN j.status IN ('failed','dead_letter') THEN 'UPLOAD_INDEX_FAILED'
                          ELSE NULL END)
    FROM app.device_upload_changes x
    JOIN app.device_upload_reservations r ON r.upload_id = x.upload_id
      AND r.workspace_id = x.workspace_id AND r.device_id = x.device_id
    JOIN app.device_folders f ON f.id = r.folder_id AND f.workspace_id = r.workspace_id
      AND f.device_id = r.device_id AND f.status = 'active'
    LEFT JOIN app.jobs j ON j.id = x.index_job_id AND j.workspace_id = x.workspace_id
    WHERE x.id=operation_id AND x.workspace_id=selected_workspace
      AND app.read_device_upload_change(
          operation_id,selected_workspace,actor,selected_session,user_version,workspace_version
      ) IS NOT NULL
$$;

ALTER FUNCTION app.claim_verified_device_upload(TEXT) OWNER TO app_migrator;
ALTER FUNCTION app.load_verified_device_upload(UUID,UUID,TEXT,INTEGER,BOOLEAN) OWNER TO app_migrator;
ALTER FUNCTION app.finish_verified_device_upload(UUID,UUID,TEXT,INTEGER,UUID,TEXT,BOOLEAN) OWNER TO app_migrator;
ALTER FUNCTION app.verify_staged_source_index_scope(UUID,UUID,BOOLEAN) OWNER TO app_migrator;
ALTER FUNCTION app.read_device_upload_index_operation(UUID,UUID,UUID,UUID,BIGINT,BIGINT) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.claim_verified_device_upload(TEXT) FROM PUBLIC,app_api,app_audit_reader;
REVOKE ALL ON FUNCTION app.load_verified_device_upload(UUID,UUID,TEXT,INTEGER,BOOLEAN) FROM PUBLIC,app_api,app_audit_reader;
REVOKE ALL ON FUNCTION app.finish_verified_device_upload(UUID,UUID,TEXT,INTEGER,UUID,TEXT,BOOLEAN) FROM PUBLIC,app_api,app_audit_reader;
REVOKE ALL ON FUNCTION app.verify_staged_source_index_scope(UUID,UUID,BOOLEAN) FROM PUBLIC,app_api,app_audit_reader;
REVOKE ALL ON FUNCTION app.read_device_upload_index_operation(UUID,UUID,UUID,UUID,BIGINT,BIGINT) FROM PUBLIC,app_worker,app_audit_reader;
GRANT EXECUTE ON FUNCTION app.claim_verified_device_upload(TEXT) TO app_worker;
GRANT EXECUTE ON FUNCTION app.load_verified_device_upload(UUID,UUID,TEXT,INTEGER,BOOLEAN) TO app_worker;
GRANT EXECUTE ON FUNCTION app.finish_verified_device_upload(UUID,UUID,TEXT,INTEGER,UUID,TEXT,BOOLEAN) TO app_worker;
GRANT EXECUTE ON FUNCTION app.verify_staged_source_index_scope(UUID,UUID,BOOLEAN) TO app_worker;
GRANT EXECUTE ON FUNCTION app.read_device_upload_index_operation(UUID,UUID,UUID,UUID,BIGINT,BIGINT) TO app_api;
