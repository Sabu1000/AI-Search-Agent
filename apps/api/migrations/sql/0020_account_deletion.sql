SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- Record only the fact that an authorized, local-only deletion committed.
-- A normal account deletion_request would be removed with its workspace FK.
-- No user, session, workspace identifier or raw capability is stored here.
CREATE TABLE app.local_account_deletion_receipts (
    id UUID PRIMARY KEY,
    receipt_hash BYTEA NOT NULL UNIQUE,
    request_key_hash BYTEA NOT NULL UNIQUE,
    requested_at TIMESTAMPTZ NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_local_account_receipt_digest CHECK (octet_length(receipt_hash) = 32),
    CONSTRAINT ck_local_account_request_digest CHECK (octet_length(request_key_hash) = 32),
    CONSTRAINT ck_local_account_receipt_completion
        CHECK (requested_at <= completed_at AND completed_at <= deadline_at),
    CONSTRAINT ck_local_account_receipt_deadline
        CHECK (deadline_at <= requested_at + INTERVAL '24 hours'),
    CONSTRAINT ck_local_account_receipt_expiry
        CHECK (expires_at > completed_at AND expires_at <= completed_at + INTERVAL '30 days')
);
CREATE INDEX ix_local_account_deletion_receipts_expiry
    ON app.local_account_deletion_receipts(expires_at);
ALTER TABLE app.local_account_deletion_receipts OWNER TO app_migrator;
ALTER TABLE app.local_account_deletion_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.local_account_deletion_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON app.local_account_deletion_receipts
    FROM PUBLIC, app_api, app_worker, app_audit_reader;

CREATE FUNCTION app.delete_local_account(
    subject_id UUID, selected_workspace UUID, session_id UUID,
    user_version BIGINT, workspace_version BIGINT, reauth_digest BYTEA,
    request_id UUID, receipt_digest BYTEA, request_key_digest BYTEA,
    workspace_digest BYTEA, subject_digest BYTEA, correlation_id UUID
)
RETURNS TABLE (
    outcome TEXT, deletion_id UUID, requested_at TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    started TIMESTAMPTZ := clock_timestamp();
    finished TIMESTAMPTZ;
    proof_id UUID;
BEGIN
    IF subject_id IS DISTINCT FROM app.current_user_id()
        OR selected_workspace IS DISTINCT FROM app.current_workspace_id()
        OR subject_id IS NULL OR selected_workspace IS NULL OR session_id IS NULL
        OR user_version IS NULL OR workspace_version IS NULL
        OR request_id IS NULL OR correlation_id IS NULL
        OR octet_length(reauth_digest) IS DISTINCT FROM 32
        OR octet_length(receipt_digest) IS DISTINCT FROM 32
        OR octet_length(request_key_digest) IS DISTINCT FROM 32
        OR octet_length(workspace_digest) IS DISTINCT FROM 32
        OR octet_length(subject_digest) IS DISTINCT FROM 32
    THEN
        RETURN QUERY SELECT 'denied'::TEXT, NULL::UUID, NULL::TIMESTAMPTZ,
                            NULL::TIMESTAMPTZ, NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    -- Match current server-verified scope and lock in user/workspace/session
    -- order. Parent UPDATE locks also fence foreign-key creation in other
    -- transactions; membership UPDATE lock fences membership child rows.
    PERFORM u.id FROM app.users u
      WHERE u.id = subject_id AND u.lock_version = user_version
        AND u.status = 'active' AND u.email_verified_at IS NOT NULL
        AND u.password_hash IS NOT NULL
      FOR UPDATE;
    IF FOUND THEN
        PERFORM w.id FROM app.workspaces w
          WHERE w.id = selected_workspace AND w.status = 'active'
            AND w.authorization_version = workspace_version
          FOR UPDATE;
    END IF;
    IF FOUND THEN
        PERFORM m.user_id FROM app.workspace_members m
          WHERE m.workspace_id = selected_workspace AND m.user_id = subject_id
            AND m.status = 'active' AND m.role = 'owner'
          FOR UPDATE;
    END IF;
    IF FOUND THEN
        PERFORM s.id FROM app.sessions s
          WHERE s.id = session_id AND s.user_id = subject_id
            AND s.authorization_version = user_version
            AND s.revoked_at IS NULL AND s.rotated_at IS NULL
            AND s.expires_at > clock_timestamp()
          FOR UPDATE;
    END IF;
    IF FOUND THEN
        SELECT t.id INTO proof_id FROM app.one_time_tokens t
          WHERE t.user_id = subject_id AND t.purpose = 'reauthenticate'
            AND t.reauth_purpose = 'delete_account' AND t.token_hash = reauth_digest
            AND t.reauth_session_id = session_id
            AND t.reauth_workspace_id = selected_workspace
            AND t.reauth_user_version = user_version
            AND t.reauth_workspace_version = workspace_version
            AND t.consumed_at IS NULL AND t.expires_at > clock_timestamp()
          FOR UPDATE;
    END IF;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'denied'::TEXT, NULL::UUID, NULL::TIMESTAMPTZ,
                            NULL::TIMESTAMPTZ, NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    -- The MVP supports only an exclusively owned database-only account.
    -- Include retired/suspended rows and old usage evidence; they may still
    -- impose external cleanup/ownership obligations. Never delete overlapping
    -- workspaces, provider auth, external objects, or pending connector jobs.
    IF EXISTS (SELECT 1 FROM app.workspace_members m
               WHERE m.workspace_id = selected_workspace AND m.user_id <> subject_id)
        OR EXISTS (SELECT 1 FROM app.workspace_members m
                   WHERE m.user_id = subject_id AND m.workspace_id <> selected_workspace)
        OR EXISTS (SELECT 1 FROM app.auth_identities i
                   WHERE i.user_id = subject_id AND i.issuer <> 'password')
        OR EXISTS (SELECT 1 FROM app.workspaces w
                   WHERE w.id = selected_workspace AND w.plan <> 'free')
        OR EXISTS (SELECT 1 FROM app.oauth_transactions o
                   WHERE o.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.connections c
                   WHERE c.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.devices d
                   WHERE d.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.sources s
                   WHERE s.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.device_upload_reservations d
                   WHERE d.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.jobs j WHERE j.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.outbox_events e
                   WHERE e.workspace_id = selected_workspace)
        OR EXISTS (SELECT 1 FROM app.deletion_requests d
                   WHERE d.workspace_id = selected_workspace
                     AND d.target_type <> 'conversation')
        OR EXISTS (SELECT 1 FROM app.workspace_usage s
                   WHERE s.workspace_id = selected_workspace
                     AND (s.indexed_source_count <> 0 OR s.extracted_bytes <> 0))
        OR EXISTS (SELECT 1 FROM app.audit_events a
                   WHERE (a.workspace_id = selected_workspace OR a.actor_user_id = subject_id
                          OR a.workspace_ref_hash = workspace_digest)
                     AND a.target_type IN ('connection','device','source','export','upload'))
    THEN
        RETURN QUERY SELECT 'unsupported'::TEXT, NULL::UUID, NULL::TIMESTAMPTZ,
                            NULL::TIMESTAMPTZ, NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    -- Check the proof again after potentially waiting on the safety checks.
    UPDATE app.one_time_tokens t SET consumed_at = clock_timestamp()
      WHERE t.id = proof_id AND t.expires_at > clock_timestamp();
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'denied'::TEXT, NULL::UUID, NULL::TIMESTAMPTZ,
                            NULL::TIMESTAMPTZ, NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    -- All sessions (including refresh credentials), identities, memberships,
    -- verification/recovery proofs, conversations and search records cascade.
    -- Existing FKs NULL the live IDs on retained pseudonymous audit evidence.
    DELETE FROM app.workspaces w WHERE w.id = selected_workspace;
    DELETE FROM app.users u WHERE u.id = subject_id;

    finished := clock_timestamp();
    INSERT INTO app.local_account_deletion_receipts (
        id, receipt_hash, request_key_hash, requested_at, deadline_at, completed_at, expires_at
    ) VALUES (
        request_id, receipt_digest, request_key_digest, started, started + INTERVAL '24 hours',
        finished, finished + INTERVAL '30 days'
    );
    INSERT INTO app.audit_events (
        id, workspace_ref_hash, action, target_type, outcome, target_ref_hash, request_id
    ) VALUES (
        gen_random_uuid(), workspace_digest, 'account.delete_local', 'user',
        'succeeded', subject_digest, correlation_id
    );
    RETURN QUERY SELECT 'completed'::TEXT, request_id, started,
                        started + INTERVAL '24 hours', finished;
END
$$;

CREATE FUNCTION app.read_local_account_deletion(provided_digest BYTEA)
RETURNS TABLE (
    deletion_id UUID, requested_at TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
    SELECT r.id, r.requested_at, r.deadline_at, r.completed_at
    FROM app.local_account_deletion_receipts r
    WHERE octet_length(provided_digest) = 32 AND r.receipt_hash = provided_digest
      AND r.expires_at > statement_timestamp()
$$;

ALTER FUNCTION app.delete_local_account(
    UUID,UUID,UUID,BIGINT,BIGINT,BYTEA,UUID,BYTEA,BYTEA,BYTEA,BYTEA,UUID
) OWNER TO app_migrator;
ALTER FUNCTION app.read_local_account_deletion(BYTEA) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.delete_local_account(
    UUID,UUID,UUID,BIGINT,BIGINT,BYTEA,UUID,BYTEA,BYTEA,BYTEA,BYTEA,UUID
) FROM PUBLIC, app_worker, app_audit_reader;
REVOKE ALL ON FUNCTION app.read_local_account_deletion(BYTEA)
    FROM PUBLIC, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.delete_local_account(
    UUID,UUID,UUID,BIGINT,BIGINT,BYTEA,UUID,BYTEA,BYTEA,BYTEA,BYTEA,UUID
) TO app_api;
GRANT EXECUTE ON FUNCTION app.read_local_account_deletion(BYTEA) TO app_api;
