SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE OR REPLACE FUNCTION app.claim_github_sync_job(
    requested_worker_id TEXT,
    requested_lease_seconds INTEGER DEFAULT 120
)
RETURNS TABLE (
    job_id UUID,
    workspace_id UUID,
    connection_id UUID,
    attempt_number INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
DECLARE
    selected_job app.jobs%ROWTYPE;
    claimed_at TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF requested_worker_id IS NULL
       OR char_length(requested_worker_id) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'invalid worker id' USING ERRCODE = '22023';
    END IF;
    IF requested_lease_seconds NOT BETWEEN 30 AND 900 THEN
        RAISE EXCEPTION 'invalid lease duration' USING ERRCODE = '22023';
    END IF;

    UPDATE app.job_attempts AS attempt
    SET status = 'permanent_failure', finished_at = claimed_at,
        error_code = 'MAX_ATTEMPTS_EXCEEDED'
    FROM app.jobs AS exhausted
    WHERE exhausted.id = attempt.job_id
      AND exhausted.job_type = 'sync'
      AND exhausted.queue = 'sync'
      AND exhausted.payload ->> 'mode' = 'github_repository'
      AND exhausted.status = 'leased'
      AND exhausted.lease_expires_at <= claimed_at
      AND exhausted.attempt_count >= exhausted.max_attempts
      AND attempt.attempt_number = exhausted.attempt_count
      AND attempt.status = 'running';

    UPDATE app.jobs AS exhausted
    SET status = 'dead_letter', error_code = 'MAX_ATTEMPTS_EXCEEDED',
        lease_owner = NULL, lease_expires_at = NULL,
        completed_at = claimed_at, updated_at = claimed_at
    WHERE exhausted.job_type = 'sync'
      AND exhausted.queue = 'sync'
      AND exhausted.payload ->> 'mode' = 'github_repository'
      AND exhausted.status = 'leased'
      AND exhausted.lease_expires_at <= claimed_at
      AND exhausted.attempt_count >= exhausted.max_attempts;

    SELECT candidate.* INTO selected_job
    FROM app.jobs AS candidate
    JOIN app.connections AS provider_connection
      ON provider_connection.id = candidate.connection_id
     AND provider_connection.workspace_id = candidate.workspace_id
    JOIN app.workspaces AS workspace ON workspace.id = candidate.workspace_id
      AND workspace.status = 'active'
    WHERE candidate.job_type = 'sync'
      AND candidate.queue = 'sync'
      AND candidate.payload ->> 'mode' = 'github_repository'
      AND provider_connection.provider = 'github'
      AND provider_connection.status IN ('pending', 'active')
      AND EXISTS (
          SELECT 1 FROM app.source_collections AS collection
          WHERE collection.workspace_id = candidate.workspace_id
            AND collection.connection_id = candidate.connection_id
            AND collection.id::TEXT = candidate.payload ->> 'collection_id'
            AND collection.kind = 'repository' AND collection.status = 'active'
            AND collection.selected
      )
      AND candidate.attempt_count < candidate.max_attempts
      AND (
          (candidate.status IN ('pending', 'retry_wait')
           AND candidate.available_at <= claimed_at)
          OR (candidate.status = 'leased' AND candidate.lease_expires_at <= claimed_at)
      )
    ORDER BY candidate.priority DESC, candidate.available_at, candidate.created_at
    FOR UPDATE OF candidate SKIP LOCKED
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF selected_job.status = 'leased' THEN
        UPDATE app.job_attempts
        SET status = 'lease_expired', finished_at = claimed_at,
            error_code = 'LEASE_EXPIRED'
        WHERE app.job_attempts.job_id = selected_job.id
          AND app.job_attempts.attempt_number = selected_job.attempt_count
          AND app.job_attempts.status = 'running';
    END IF;

    UPDATE app.jobs
    SET status = 'leased', attempt_count = attempt_count + 1,
        lease_owner = requested_worker_id,
        lease_expires_at = claimed_at + make_interval(secs => requested_lease_seconds),
        error_code = NULL, updated_at = claimed_at
    WHERE app.jobs.id = selected_job.id
    RETURNING * INTO selected_job;

    INSERT INTO app.job_attempts (
        id, workspace_id, job_id, attempt_number, worker_id, status, started_at
    ) VALUES (
        md5(selected_job.id::TEXT || ':' || selected_job.attempt_count::TEXT)::UUID,
        selected_job.workspace_id, selected_job.id, selected_job.attempt_count,
        requested_worker_id, 'running', claimed_at
    );

    job_id := selected_job.id;
    workspace_id := selected_job.workspace_id;
    connection_id := selected_job.connection_id;
    attempt_number := selected_job.attempt_count;
    RETURN NEXT;
END
$$;

CREATE OR REPLACE FUNCTION app.record_github_webhook_hint(
    installation BIGINT, repository BIGINT, delivery UUID, event TEXT,
    digest BYTEA, installation_wide BOOLEAN, repository_scope BOOLEAN
)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    account RECORD;
    selection RECORD;
    close_visibility BOOLEAN;
    event_id UUID;
    queue_id UUID;
    seen_at TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF installation IS NULL OR installation <= 0 OR delivery IS NULL
        OR event IS NULL OR event !~ '^[a-z_]{1,64}$'
        OR digest IS NULL OR octet_length(digest) <> 32
        OR installation_wide IS NULL OR repository_scope IS NULL
        OR (repository_scope AND (installation_wide OR event <> 'repository'))
        OR (NOT installation_wide AND (repository IS NULL OR repository <= 0))
    THEN
        RAISE EXCEPTION 'invalid GitHub webhook hint' USING ERRCODE = '22023';
    END IF;
    close_visibility := installation_wide OR repository_scope;

    FOR account IN
        SELECT c.id, c.workspace_id FROM app.connections AS c
        WHERE c.provider = 'github' AND c.github_installation_id = installation
          AND c.status IN ('pending', 'active')
        ORDER BY c.workspace_id, c.id
    LOOP
        -- Match the worker's lock order. The workspace row also linearizes
        -- search-generation changes with commits from in-flight scans.
        PERFORM 1 FROM app.workspaces AS w
        WHERE w.id = account.workspace_id FOR UPDATE;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        PERFORM 1 FROM app.connections AS c
        WHERE c.id = account.id AND c.workspace_id = account.workspace_id
          AND c.provider = 'github' AND c.github_installation_id = installation
          AND c.status IN ('pending', 'active')
        FOR UPDATE;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM app.source_collections AS s
            WHERE s.connection_id = account.id AND s.workspace_id = account.workspace_id
              AND s.kind = 'repository' AND s.selected AND s.status = 'active'
              AND (installation_wide OR s.provider_external_id = repository::TEXT)
        ) OR EXISTS (
            SELECT 1 FROM app.provider_events AS e
            WHERE e.connection_id = account.id AND e.event_type LIKE 'github.%'
              AND (e.provider_event_id = delivery::TEXT OR e.payload_hash = digest)
        ) THEN
            CONTINUE;
        END IF;

        -- Regular content events are hints, so suppress bursts and rely on polling.
        -- Scope changes must still fence in-flight snapshots before returning.
        IF NOT close_visibility AND EXISTS (
            SELECT 1 FROM app.provider_events AS e
            WHERE e.connection_id = account.id AND e.event_type LIKE 'github.%'
              AND e.received_at > seen_at - INTERVAL '30 seconds'
        ) THEN
            CONTINUE;
        END IF;

        event_id := md5(account.id::TEXT || ':github:' || delivery::TEXT)::UUID;
        INSERT INTO app.provider_events (
            id, workspace_id, connection_id, provider_event_id, event_type,
            payload_hash, signature_verified_at, status, received_at, processed_at
        ) VALUES (
            event_id, account.workspace_id, account.id, delivery::TEXT,
            'github.' || event, digest, seen_at, 'processed', seen_at, seen_at
        )
        ON CONFLICT (connection_id, provider_event_id) DO NOTHING;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        IF close_visibility THEN
            UPDATE app.workspaces AS w SET
                search_index_generation = w.search_index_generation + 1
            WHERE w.id = account.workspace_id;
        END IF;

        FOR selection IN
            SELECT s.id FROM app.source_collections AS s
            WHERE s.workspace_id = account.workspace_id AND s.connection_id = account.id
              AND s.kind = 'repository' AND s.selected AND s.status = 'active'
              AND (installation_wide OR s.provider_external_id = repository::TEXT)
            ORDER BY s.id FOR UPDATE
        LOOP
            IF close_visibility THEN
                UPDATE app.source_collections AS s SET
                    github_verified_at = NULL,
                    github_authorization_generation = s.github_authorization_generation + 1,
                    updated_at = seen_at WHERE s.id = selection.id;
            END IF;

            -- A recurring job exists after the first scan. Pull it forward rather
            -- than creating a new job for each delivery.
            UPDATE app.jobs AS j SET
                available_at = LEAST(j.available_at, seen_at), updated_at = seen_at
            WHERE j.workspace_id = account.workspace_id AND j.connection_id = account.id
              AND j.job_type = 'sync' AND j.queue = 'sync'
              AND j.payload ->> 'mode' = 'github_repository'
              AND j.payload ->> 'collection_id' = selection.id::TEXT
              AND j.status IN ('pending', 'retry_wait');

            IF NOT FOUND AND NOT EXISTS (
                SELECT 1 FROM app.jobs AS j
                WHERE j.workspace_id = account.workspace_id
                  AND j.connection_id = account.id AND j.job_type = 'sync'
                  AND j.payload ->> 'mode' = 'github_repository'
                  AND j.payload ->> 'collection_id' = selection.id::TEXT
                  AND j.status = 'leased' AND j.lease_expires_at > seen_at
            ) THEN
                queue_id := md5(event_id::TEXT || ':' || selection.id::TEXT)::UUID;
                INSERT INTO app.jobs (
                    id, workspace_id, connection_id, job_type, queue,
                    idempotency_key, status, available_at, payload
                ) VALUES (
                    queue_id, account.workspace_id, account.id, 'sync', 'sync',
                    'github-webhook:' || queue_id::TEXT, 'pending', seen_at,
                    jsonb_build_object('mode', 'github_repository',
                                       'collection_id', selection.id::TEXT)
                ) ON CONFLICT (workspace_id, job_type, idempotency_key) DO NOTHING;
            END IF;
        END LOOP;
    END LOOP;
END
$$;


ALTER FUNCTION app.record_github_webhook_hint(
    BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN, BOOLEAN
) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.record_github_webhook_hint(
    BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN, BOOLEAN
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.record_github_webhook_hint(
    BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN, BOOLEAN
) TO app_api;

-- The previous caller does not send an action. Keep it compatible, but take
-- the conservative repository-only fence for legacy repository events.
CREATE OR REPLACE FUNCTION app.record_github_webhook_hint(
    installation BIGINT, repository BIGINT, delivery UUID, event TEXT,
    digest BYTEA, installation_wide BOOLEAN
)
RETURNS VOID
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
    SELECT app.record_github_webhook_hint(
        installation, repository, delivery, event, digest, installation_wide,
        NOT installation_wide AND event = 'repository'
    )
$$;
