SET LOCAL lock_timeout = '5s';

-- Receipts are not archives. The snapshot hash is HMACed by the application;
-- neither the JSON data nor any replayable download secret is written here.
CREATE TABLE app.local_account_exports (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES app.sessions(id) ON DELETE CASCADE,
    user_version BIGINT NOT NULL CONSTRAINT ck_export_user_version CHECK (user_version > 0),
    workspace_version BIGINT NOT NULL CONSTRAINT ck_export_workspace_version CHECK (workspace_version > 0),
    request_key_hash BYTEA NOT NULL CONSTRAINT ck_export_key_hash CHECK (octet_length(request_key_hash) = 32),
    snapshot_hash BYTEA NOT NULL CONSTRAINT ck_export_snapshot_hash CHECK (octet_length(snapshot_hash) = 32),
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    download_expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    UNIQUE (workspace_id,user_id,request_key_hash),
    FOREIGN KEY (workspace_id,user_id)
      REFERENCES app.workspace_members(workspace_id,user_id) ON DELETE CASCADE,
    CONSTRAINT ck_export_operation_timestamps CHECK (created_at <= completed_at AND completed_at < download_expires_at),
    CONSTRAINT ck_export_download_lifetime CHECK (download_expires_at <= completed_at + interval '5 minutes')
);
ALTER TABLE app.local_account_exports OWNER TO app_migrator;
ALTER TABLE app.local_account_exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.local_account_exports FORCE ROW LEVEL SECURITY;
REVOKE ALL ON app.local_account_exports FROM PUBLIC, app_api, app_worker, app_audit_reader;
GRANT SELECT, INSERT, DELETE ON app.local_account_exports TO app_api;
GRANT UPDATE (consumed_at) ON app.local_account_exports TO app_api;
CREATE POLICY api_private_exports ON app.local_account_exports TO app_api
USING (user_id = app.current_user_id() AND workspace_id = app.current_workspace_id()
       AND app.has_active_workspace_membership(workspace_id))
WITH CHECK (user_id = app.current_user_id() AND workspace_id = app.current_workspace_id()
            AND app.has_active_workspace_membership(workspace_id));

-- Scoped SECURITY DEFINER is needed to count *all* memberships (including
-- workspaces hidden by RLS). Fixed lock order is shared with account deletion.
CREATE FUNCTION app.authorize_local_account_export(
    subject_id UUID, selected_workspace UUID, selected_session UUID,
    user_version BIGINT, workspace_version BIGINT
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
BEGIN
    IF subject_id IS NULL OR selected_workspace IS NULL OR selected_session IS NULL
       OR subject_id IS DISTINCT FROM app.current_user_id()
       OR selected_workspace IS DISTINCT FROM app.current_workspace_id()
    THEN RETURN 'denied'; END IF;
    PERFORM u.id FROM app.users u
      WHERE u.id = subject_id AND u.lock_version = user_version
        AND u.status = 'active' AND u.email_verified_at IS NOT NULL
      FOR UPDATE;
    IF NOT FOUND THEN RETURN 'denied'; END IF;
    PERFORM w.id FROM app.workspaces w
      WHERE w.id = selected_workspace AND w.status = 'active'
        AND w.authorization_version = workspace_version
      FOR UPDATE;
    IF NOT FOUND THEN RETURN 'denied'; END IF;
    PERFORM m.user_id FROM app.workspace_members m
      WHERE m.workspace_id = selected_workspace AND m.user_id = subject_id
        AND m.status = 'active' AND m.role = 'owner'
      FOR SHARE;
    IF NOT FOUND THEN RETURN 'denied'; END IF;
    PERFORM s.id FROM app.sessions s
      WHERE s.id = selected_session AND s.user_id = subject_id
        AND s.authorization_version = user_version
        AND s.revoked_at IS NULL AND s.rotated_at IS NULL
        AND s.expires_at > clock_timestamp()
      FOR SHARE;
    IF NOT FOUND THEN RETURN 'denied'; END IF;
    IF EXISTS (
        SELECT FROM app.deletion_requests d
        WHERE d.workspace_id = selected_workspace AND d.target_type = 'account'
    ) THEN RETURN 'denied'; END IF;
    -- The local MVP cannot attest to portability of provider files, shared
    -- content, device bytes, paid services, or external identity metadata.
    IF EXISTS (SELECT FROM app.workspace_members m
               WHERE m.workspace_id = selected_workspace AND m.user_id <> subject_id)
       OR EXISTS (SELECT FROM app.workspace_members m
                  WHERE m.user_id = subject_id AND m.workspace_id <> selected_workspace)
       OR EXISTS (SELECT FROM app.auth_identities i
                  WHERE i.user_id = subject_id AND i.issuer <> 'password')
       OR EXISTS (SELECT FROM app.workspaces w
                  WHERE w.id = selected_workspace AND w.plan <> 'free')
       OR EXISTS (SELECT FROM app.connections c WHERE c.workspace_id = selected_workspace)
       OR EXISTS (SELECT FROM app.sources s WHERE s.workspace_id = selected_workspace)
       OR EXISTS (SELECT FROM app.devices d WHERE d.workspace_id = selected_workspace)
       OR EXISTS (SELECT FROM app.oauth_transactions o WHERE o.workspace_id = selected_workspace)
       OR EXISTS (SELECT FROM app.device_upload_reservations d
                  WHERE d.workspace_id = selected_workspace)
    THEN RETURN 'unsupported'; END IF;
    RETURN 'authorized';
END
$$;
ALTER FUNCTION app.authorize_local_account_export(UUID,UUID,UUID,BIGINT,BIGINT)
    OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.authorize_local_account_export(UUID,UUID,UUID,BIGINT,BIGINT)
    FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.authorize_local_account_export(UUID,UUID,UUID,BIGINT,BIGINT)
    TO app_api;
