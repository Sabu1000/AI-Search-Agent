# Incident Runbooks

## Provider outage
1. Confirm provider status and error pattern.
2. Pause aggressive retries.
3. Keep existing index available.
4. Display delayed-sync status.
5. Resume with jittered backoff.

## Database degradation
1. Freeze nonessential indexing.
2. Check connections, locks, disk, and replica lag.
3. Scale or fail over if needed.
4. Preserve read-only search where possible.

## Cross-tenant exposure suspicion
1. Disable search immediately.
2. Preserve logs and traces.
3. Rotate relevant credentials.
4. Identify affected requests and users.
5. Follow legal and incident-notification procedure.
6. Do not restore until isolation tests pass.

## OAuth credential leak
1. Revoke affected provider tokens.
2. Rotate KMS and application secrets where required.
3. Invalidate sessions.
4. Audit provider access.
5. Notify affected users according to policy.

## Model or embedding outage
- Fallback to keyword-only search
- Return search results without synthesized answer
- Queue reprocessing only when safe

## Failed deletion
- Alert when deletion exceeds 24 hours
- Retry idempotently
- Report exact remaining resources
- Escalate unresolved storage or backup issues
