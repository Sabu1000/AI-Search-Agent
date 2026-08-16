SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DROP FUNCTION IF EXISTS app.claim_gmail_sync_job(TEXT, INTEGER);
