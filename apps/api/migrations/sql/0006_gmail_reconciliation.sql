SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

ALTER TABLE app.sources ADD COLUMN provider_sync_marker UUID;

CREATE INDEX ix_sources_connection_sync_marker
    ON app.sources (workspace_id, connection_id, provider_sync_marker)
    WHERE state = 'active';
