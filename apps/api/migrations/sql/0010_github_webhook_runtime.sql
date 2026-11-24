ALTER TABLE app.source_collections
    ADD COLUMN github_authorization_generation BIGINT NOT NULL DEFAULT 1,
    ADD CONSTRAINT ck_github_authorization_generation
        CHECK (github_authorization_generation > 0);

CREATE INDEX ix_connections_github_webhook
    ON app.connections (github_installation_id)
    WHERE provider = 'github' AND status IN ('pending', 'active');

CREATE INDEX ix_provider_events_github_hash
    ON app.provider_events (connection_id, payload_hash)
    WHERE event_type LIKE 'github.%';

CREATE FUNCTION app.record_github_webhook_hint(
    installation BIGINT, repository BIGINT, delivery UUID, event TEXT,
    digest BYTEA, installation_wide BOOLEAN
)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app
AS $$
DECLARE
    account RECORD;
    selection RECORD;
    event_id UUID;
    queue_id UUID;
    seen_at TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF installation IS NULL OR installation <= 0 OR delivery IS NULL
        OR event IS NULL OR event !~ '^[a-z_]{1,64}$'
        OR digest IS NULL OR octet_length(digest) <> 32
        OR installation_wide IS NULL
        OR (NOT installation_wide AND (repository IS NULL OR repository <= 0))
    THEN
        RAISE EXCEPTION 'invalid GitHub webhook hint' USING ERRCODE = '22023';
    END IF;
    FOR account IN
        SELECT c.id, c.workspace_id FROM app.connections AS c
        WHERE c.provider = 'github' AND c.github_installation_id = installation
          AND c.status IN ('pending', 'active')
        ORDER BY c.id FOR UPDATE
    LOOP
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
        IF NOT installation_wide AND EXISTS (
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

        IF installation_wide THEN
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
            IF installation_wide THEN
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

ALTER FUNCTION app.record_github_webhook_hint(BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN)
    OWNER TO app_migrator;
REVOKE ALL ON FUNCTION
    app.record_github_webhook_hint(BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app.record_github_webhook_hint(BIGINT, BIGINT, UUID, TEXT, BYTEA, BOOLEAN) TO app_api;
