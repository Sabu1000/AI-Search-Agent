SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

INSERT INTO app.embedding_profiles (
    provider, model, dimensions, distance_metric, status, activated_at
)
SELECT 'local', 'deterministic-sha256-v1', 1536, 'cosine', 'active', clock_timestamp()
WHERE NOT EXISTS (SELECT 1 FROM app.embedding_profiles WHERE status = 'active')
ON CONFLICT (provider, model) DO NOTHING;

CREATE OR REPLACE FUNCTION app.claim_index_job(
    requested_worker_id TEXT,
    requested_lease_seconds INTEGER DEFAULT 120
)
RETURNS TABLE (
    job_id UUID,
    workspace_id UUID,
    source_id UUID,
    document_version_id UUID,
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
      AND exhausted.job_type = 'index'
      AND exhausted.queue = 'index'
      AND exhausted.status = 'leased'
      AND exhausted.lease_expires_at <= claimed_at
      AND exhausted.attempt_count >= exhausted.max_attempts
      AND attempt.attempt_number = exhausted.attempt_count
      AND attempt.status = 'running';

    UPDATE app.jobs AS exhausted
    SET status = 'dead_letter', error_code = 'MAX_ATTEMPTS_EXCEEDED',
        lease_owner = NULL, lease_expires_at = NULL,
        completed_at = claimed_at, updated_at = claimed_at
    WHERE exhausted.job_type = 'index'
      AND exhausted.queue = 'index'
      AND exhausted.status = 'leased'
      AND exhausted.lease_expires_at <= claimed_at
      AND exhausted.attempt_count >= exhausted.max_attempts;

    SELECT candidate.* INTO selected_job
    FROM app.jobs AS candidate
    WHERE candidate.job_type = 'index'
      AND candidate.queue = 'index'
      AND candidate.attempt_count < candidate.max_attempts
      AND (
          (candidate.status IN ('pending', 'retry_wait')
           AND candidate.available_at <= claimed_at)
          OR (candidate.status = 'leased' AND candidate.lease_expires_at <= claimed_at)
      )
    ORDER BY candidate.priority DESC, candidate.available_at, candidate.created_at
    FOR UPDATE SKIP LOCKED
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
    source_id := selected_job.source_id;
    document_version_id := (selected_job.payload ->> 'document_version_id')::UUID;
    attempt_number := selected_job.attempt_count;
    RETURN NEXT;
END
$$;

ALTER FUNCTION app.claim_index_job(TEXT, INTEGER) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.claim_index_job(TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.claim_index_job(TEXT, INTEGER) TO app_worker;
