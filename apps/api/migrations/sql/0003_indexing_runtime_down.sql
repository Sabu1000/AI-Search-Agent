SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DROP FUNCTION IF EXISTS app.claim_index_job(TEXT, INTEGER);

DELETE FROM app.embedding_profiles
WHERE provider = 'local' AND model = 'deterministic-sha256-v1';
