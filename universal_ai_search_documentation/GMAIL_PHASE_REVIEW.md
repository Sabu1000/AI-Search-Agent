# Gmail Phase Review

**Decision:** Pass for the tested all-mail backend scope (`P5-001` through
`P5-011`).

## Completed behavior

| Task | Evidence |
| --- | --- |
| OAuth and API client | Exact read-only scopes, token refresh, bounded list/history pages, strict response validation, and sanitized provider errors. |
| Initial and incremental sync | One bounded provider page per durable job, encrypted continuation state, idempotent indexing handoff, and cursor commit only after the terminal page. |
| Email and attachments | Conservative MIME parsing, inert normalized text, stable attachment identities, bounded external text-part hydration, and safe descriptors for unsupported content. |
| Metadata and normalization | Bounded allowlisted RFC metadata, structured people, canonical targets, Unicode normalization, control stripping, and deterministic content hashes. |
| Deletion | Incremental parent deletion and authoritative full-scan absence both purge attachment sources and all derived versions, chunks, embeddings, citations, people rows, and pending index work while retaining scrubbed tombstones. |
| Recovery | Bounded `Retry-After`, capped exponential full jitter, explicit reauthorization, permanent failure, dead-letter, and expired-cursor recovery states. |

## Safety conclusions

- A partial or failed full scan cannot delete unseen mail. Reconciliation occurs
  only after every page is durably processed under one scan marker.
- Replays are idempotent: stable source identities and job keys prevent duplicate
  searchable records, and cursor advancement never precedes content handoff.
- Deleted content is not merely filtered from search; its derived searchable data
  is removed. Retained tombstones contain no title, URL, people, content, or
  provider metadata.
- Provider bodies, page tokens, and credentials are excluded from durable errors.

## Automated certification

```sh
./scripts/test-backend.sh
./scripts/test-connector-sdk.sh
./scripts/test-database.sh
pnpm check
```

The certified local suite contains `122` backend tests at `91.47%` line
coverage, `25` Connector SDK tests at `99%`, and `19` live PostgreSQL tests,
including migration upgrade/downgrade, concurrent deletion, authoritative
absence, derived-data purge, retry timing, dead-letter exhaustion, and tenant
isolation.

## Explicitly outside this phase

Drive synchronization, configurable Gmail label-selection UI, PDF/Office binary
attachment parsers, and a live Google-project smoke test are later integrations.
They do not change the completed all-mail backend contract reviewed here.
