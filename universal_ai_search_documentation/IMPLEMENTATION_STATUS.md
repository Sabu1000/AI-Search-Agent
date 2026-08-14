# Implementation Status

**Last audited:** 2026-08-13

**Audited baseline:** `5588076`; this ledger includes the current `B6`
checkpoint changes.

**Purpose:** Separate validated design specifications from working product
features and keep an evidence-based record of the next implementation step.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| Specification complete | The owning document has explicit, internally consistent, automatically validated contracts. It does not mean the described product exists. |
| Implemented | Production code exists for the complete stated scope, automated tests pass, and the code is integrated into the runnable application. |
| Partial | Some supporting or production code exists, but the user-visible acceptance criteria cannot yet pass end to end. |
| Not started | No production implementation of the stated feature exists. Designs, validators, shells, or test fakes do not count as the feature. |
| Blocked by design dependency | Safe implementation requires a later owning specification to be completed first. |

All counts in this file describe committed production code, not planned code.
Generated build output, test fakes, documentation validators, and application
shells are never counted as finished user features.

## Numbered-document audit

| Stage | Specification | Product implementation | Evidence and remaining work |
| --- | --- | --- | --- |
| `00_README.md` | Complete | Partial foundation | Monorepo, CI, Docker Compose, web/API/worker/desktop shells, shared packages, and quality gates work. Branch protection, a license decision, and the Phase 1 review remain open. |
| `01_PROJECT_SPEC.md` | Complete | MVP partial (`1/11`) | `AUTH-001` passes end to end; the other 10 MVP requirements remain partial or not started. See the requirement audit below. |
| `02_SYSTEM_ARCHITECTURE.md` | Complete | Partial | Process shells, local dependencies, schema migration ordering, revision-aware API readiness, a fail-closed HTTP boundary, database-backed authentication/authorization, durable indexing, and hybrid search exist. Real connector orchestration, model streaming, and remaining product services do not. |
| `03_SEARCH_ENGINE_DESIGN.md` | Complete | Local-index backend implemented; product phase partial | Exact/title, PostgreSQL FTS, pgvector, and trigram lanes run inside API-role RLS; typed filters, weighted RRF, deterministic reranking/deduplication, bounded context, extractive claims/citations, history persistence, safe empty answers, and `/v1/search` pass unit and live PostgreSQL tests. Production semantic/model adapters, relevance evaluation corpus, conflict evaluator, cache, and UI remain. |
| `04_DATABASE_SCHEMA.md` | Complete | Database runtime implemented; phase partial | Alembic creates `33/33` specified tables, runtime functions, constraints, indexes, roles, grants, and forced RLS. Compose migrates before API startup; readiness checks the revision; 17 PostgreSQL integration tests cover auth, indexing/search, catalog, lifecycle, and tenant isolation. Seed data, backup automation, and the Phase 3 review remain. |
| `05_API_SPECIFICATION.md` | Complete | API platform, auth, and search route implemented; product API partial | FastAPI has the OpenAPI 3.1 `/v1` boundary, request IDs, strict serialization, RFC 9457 problems, signed cursors, idempotency primitives, and working authentication/workspace backends. `7/49` catalogued product endpoints exist; rate limiting, SSE, remaining repositories, and other feature services remain. |
| `06_CONNECTOR_FRAMEWORK.md` | Complete | SDK implemented; provider ecosystem partial | The typed SDK, four change variants, registry, retry policy, runtime stream validator, fake connector, Docker gate, and CI job are implemented. Real Gmail, Drive, GitHub, and local-file connectors, OAuth persistence, scheduling, logging, and metrics are not. |
| `07_INDEXING_PIPELINE.md` | Complete | Normalized text/Markdown path implemented | Deterministic normalization, language selection, structural chunking, exact/near deduplication, local 1,536-dimensional embeddings, durable PostgreSQL leasing, atomic promotion, unchanged skips, and re-index supersession pass unit and PostgreSQL/pgvector tests. Provider-specific binary parsers and production embedding providers remain later integrations. |
| `08_SECURITY_AND_PRIVACY.md` | Complete | Partial controls | The threat model, data classification, tenant/cryptographic boundaries, browser/input/model defenses, audit/redaction policy, deletion/backup rules, and security release gates are implementation-ready. Existing RLS, strict API boundaries, secret validation, and containerized tests implement only part of the required controls. |
| `09_AUTH_AND_OAUTH.md` | Complete | Authentication subset implemented; provider/recovery work partial | Password registration, email proof, Argon2id, signed access tokens, rotating opaque refresh sessions, replay-family revocation, browser CSRF/origin checks, logout, account/membership lookup, workspace bootstrap, and minimal web UI work end to end. Password recovery, reauthentication, OAuth callbacks, provider credential encryption, abuse throttling, and the full security review remain. |

The next implementation slice is provider and desktop integration through
backfill `B7`. Stage 03 now consumes Stage 07 ready chunks and embeddings through
the authenticated API and records each search under forced tenant RLS.

## MVP requirement audit

| Requirement | Status | Implemented evidence | Missing acceptance path |
| --- | --- | --- | --- |
| `AUTH-001` | Implemented | A visitor can register, receive and submit a single-use email proof, receive an owner workspace, log in, inspect memberships, rotate a session, log out, and sign back in through the minimal web UI and six `/v1/auth` endpoints. Argon2id, generic failures, signed 15-minute JWT access tokens, hashed opaque refresh/CSRF tokens, exact-origin CSRF checks, non-superuser RLS, route/unit/database tests, and the Docker smoke test cover the acceptance path. | No missing MVP acceptance path. Password recovery, OAuth login, rate limiting, reauthentication, and the complete Phase 2 security review are later hardening/expansion tasks. |
| `DESKTOP-001` | Partial | Tauri React/Rust shell compiles and is container-checked. | Signed installers, folder selection, root authorization, scanner/watcher, device registration, offline queue, removal/deletion flow, and platform tests. |
| `GOOGLE-001` | Not started | Canonical SDK provider contracts only. | OAuth scopes/callbacks, encrypted credentials, Gmail and Drive clients, selections, full/incremental sync, revocation, UI, and provider contract tests. |
| `GITHUB-001` | Not started | Canonical SDK provider contract only. | GitHub App, installation/repository selection, API client, webhooks, reconciliation, access removal, UI, and provider contract tests. |
| `SYNC-001` | Partial | SDK defines deterministic changes and the indexing path has durable idempotent PostgreSQL jobs, worker leases/attempts, stale-lease recovery, bounded errors, and unchanged skips. | Durable provider cursors, provider scheduler/orchestration, status APIs/UI, complete replay tests, and operational metrics. |
| `SEARCH-001` | Partial | Authorized exact, FTS, vector, and trigram retrieval, typed provider/person/date/repository/folder/source/file filters, fusion, ranking, safe empty results, history, and API/database isolation tests work against indexed content. | Real connected providers, UI result/filter states, production semantic embeddings, and a labeled retrieval evaluation corpus. |
| `ANSWER-001` | Partial | Bounded authorized context produces extractive material claims with one-to-one citations and an explicit `no_authorized_results` insufficient-evidence response. | Production model gateway, synthesis/conflict evaluator, streaming UI, and measured grounding evaluation. |
| `CITATION-001` | Partial | The API derives excerpts and allowlisted HTTPS targets from authorized stored metadata, links claims to stable source/chunk IDs, and omits unavailable targets. | UI rendering/opening, target reauthorization endpoint, revoked-target end-to-end tests, and measured citation correctness. |
| `CONNECTION-001` | Partial | Connection, scope, cursor, deletion, job, and outbox storage exists; the SDK has deletion and permission-change events. | Credential services, revocation, sync cancellation, cascade workers, progress API/UI, 24-hour enforcement, and end-to-end tests. |
| `ACCOUNT-001` | Partial | Workspace state and durable deletion-request/job/outbox/audit storage exist. | Immediate access-termination service, cascade workers, receipt/status, export, backup-retention handling, and deadline alerts/tests. |
| `SAFETY-001` | Partial | Product and SDK are structurally read-only; strict API models reject unknown input; privacy-safe problems avoid echoing inputs and internal errors; database-backed auth/workspace dependencies fail closed; signed cursors and idempotency hashes bind principal/workspace context; identity and tenant tables use tested forced RLS. | Content sanitization boundaries, prompt-injection fixtures through retrieval/answering, log redaction, and full cross-user service tests. |

Current end-to-end MVP acceptance: `1/11` requirements. Partial means useful
prerequisite code exists; it does not increase the end-to-end count.

## Implemented code inventory

### Foundation

- `apps/web`: tested Next.js production shell.
- `apps/api`: tested FastAPI liveness/readiness service and durable indexing worker.
- `apps/desktop`: tested Tauri/React shell and Rust compile configuration.
- `packages/shared-types` and `packages/ui`: tested starter packages.
- `compose.yaml`: PostgreSQL/pgvector, Redis, MinIO, Mailpit, API, and web local
  topology.
- `.github/workflows/ci.yml`: project, backend, Connector SDK, desktop Rust,
  Compose, and image-build gates.

Foundation master tasks complete: `27/30`. Open tasks are branch protection
(`P1-002`), license selection (`P1-004`), and Phase 1 review (`P1-030`). The
first two require repository-owner policy decisions; the review remains open
until those decisions are recorded.

### Connector SDK

- Strict immutable Pydantic models for credentials, sync context, normalized
  documents, access metadata, health, and provider-neutral changes.
- Canonical `gmail`, `google_drive`, `github`, and `local_files` identities.
- Stable canonical JSON and SHA-256 content, permission, and change hashes.
- Provider factory registry and protocol identity validation.
- Retry classification with exponential full jitter and bounded
  `Retry-After` handling.
- Stream validation for provider identity, duplicate change IDs, and exactly
  one terminal cursor.
- Deterministic fake connector plus `25 tests at 99% line coverage`.
- Python 3.12 Docker test target integrated into CI and the backend runtime
  image.

Connector SDK master tasks complete: `5/11`: `P4-001`, `P4-004`, `P4-006`,
`P4-007`, and `P4-008`. OAuth management, token storage, scheduling, production
logging, metrics, and the Phase 4 review remain open.

### Database runtime

- Alembic migration CLI and revision `0001_initial_schema` create and version
  all `33/33` application tables in the `app` schema.
- PostgreSQL `citext`, `pg_trgm`, and pgvector types support identity, lexical,
  trigram, and vector workloads.
- Named checks, composite tenant foreign keys, immutable-version lineage, queue
  indexes, generated `TSVECTOR`, and an HNSW `VECTOR(1536)` index are present.
- `app_api`, `app_worker`, and `app_audit_reader` are non-login,
  non-`BYPASSRLS` roles. Identity and tenant tables use forced, fail-closed RLS.
- Compose runs migrations to completion before starting the API and worker, and
  readiness rejects any revision other than `0003_indexing_runtime`.
- An ephemeral PostgreSQL 16/pgvector Docker suite runs Python quality gates
  and `15` authentication, indexing, catalog, migration-lifecycle,
  foreign-key, and tenant-isolation tests in CI.

Database master tasks complete: `12/15`: `P3-001` through `P3-012`. Seed data,
backup implementation, and the Phase 3 review remain open. These tables are
production schema, but they do not by themselves implement repositories or
user-visible workflows.

### API platform

- FastAPI publishes OpenAPI 3.1 and mounts a `/v1` router with six authentication
  operations plus hybrid search; the other 42 product operations stay closed
  until their services exist.
- Every response receives a canonical UUID request ID; valid caller IDs are
  normalized and invalid IDs are replaced.
- Strict request/response models enforce unknown-field rejection, canonical
  UUIDs, UTC `Z` timestamps, and optional-field omission.
- RFC 9457-compatible `application/problem+json` responses cover validation,
  framework HTTP errors, expected domain errors, and opaque unexpected errors
  without exposing rejected input or private exception details.
- HMAC-authenticated, expiring keyset cursors bind endpoint, workspace,
  principal, filters, sorting, and position.
- Idempotency primitives validate and HMAC-hash keys, canonically hash the
  request context, define atomic reservation/replay storage contracts, and
  prevent credential, content, and signed-URL persistence in replay payloads.
- Immutable principal/workspace models and dependency protocols are wired to
  database-backed authentication and membership checks; missing or invalid
  authority is rejected.
- Compose passes independent cursor/idempotency secrets; settings reject short,
  shared, or local-only production values.
- The combined backend gate reports its current test and coverage totals below;
  Black, Ruff, and strict mypy run in the same Docker target.

### Authentication vertical slice

- Argon2id password hashing uses 64 MiB, three iterations, parallelism one,
  dummy-hash verification for unknown users, identity/common-password checks,
  and opportunistic rehashing.
- Generic registration stores terms/locale and sends a 24-hour, single-use
  verification URL to SMTP; verification atomically activates the user and
  creates a personal owner workspace.
- HMAC-signed HS256 JWT access tokens carry only identity/session/version
  claims and expire after 15 minutes. Opaque refresh and CSRF values are stored
  only as keyed hashes; refresh rotates the session and replay revokes its
  family.
- Browser transport uses exact `__Host-` Secure cookie attributes plus
  double-submit, session-bound CSRF and exact-origin checks. Native transport
  returns access/refresh credentials only on login or refresh. Mixed cookie and
  bearer authentication is rejected.
- The API runs with effective role `app_api`; scoped security-definer bootstrap
  functions and forced RLS protect account, session, and membership reads.
- Six catalogued endpoints implement register, email verification, login,
  refresh, logout, and `me`; the Next.js page provides their minimal UI.
- `83 backend tests at 94.23% line coverage`, `17 PostgreSQL integration tests`,
  web checks, production builds, Docker health checks, and
  `scripts/smoke-auth.py` cover the slice.

Authentication master tasks complete: `12/18`: `P2-001` through `P2-007`,
`P2-010`, and `P2-013` through `P2-016`. Password recovery, Google/GitHub login,
the full security review, and the Phase 2 review remain open.

### Indexing pipeline

- Normalized text and Markdown are Unicode-normalized, language-classified,
  structurally chunked with deterministic IDs, and exact/near duplicates are
  removed within safe structural scope.
- A deterministic local 1,536-dimensional embedding provider makes development
  and CI repeatable without sending content to an external model service.
- PostgreSQL stores idempotent jobs, leases, attempt history, bounded failure
  codes, immutable pending/ready/superseded versions, chunks, pgvector rows,
  usage, generation changes, and identifier-only outbox events.
- The `app_worker` role remains `NOBYPASSRLS`; a narrowly granted
  security-definer claim function chooses the authoritative job workspace
  before subsequent transactions set tenant context.
- Promotion is atomic, unchanged input is skipped, and changed content creates
  a new ready version only after every chunk and vector is valid. The previous
  version stays ready until that transaction commits.
- `72 backend tests at 93.11% line coverage` and `15 PostgreSQL integration
  tests` include the fake connector → queue → worker → ready index path,
  authoritative claiming, unchanged ingestion, and safe re-indexing.

Indexing master tasks complete: `10/10`, `P8-001` through `P8-010`, for the
explicitly supported normalized text/Markdown scope. Binary/provider-specific
parsers and production semantic embeddings are expansions, not claims of this
checkpoint.

### Hybrid search backend

- Deterministic Unicode query planning and typed filters feed four bounded
  PostgreSQL lanes: exact/title, full-text, pgvector cosine, and title trigram.
- Every lane selects only active sources whose current immutable version is
  ready, while API-role forced RLS binds the authenticated user and workspace.
- Weighted reciprocal-rank fusion, normalized feature reranking, hash
  deduplication, stable tie-breaking, and a three-chunk-per-source cap shape
  results without model nondeterminism.
- Answer mode selects token-, source-, and section-bounded context and returns
  extractive claims whose citation IDs point to server-hydrated excerpts and
  allowlisted stored targets. Empty evidence is reported honestly.
- `POST /v1/search` validates authentication, workspace, filters, limits, and
  answer idempotency-key syntax; completed and insufficient searches are
  retained for 30 days in `search_requests`.
- Unit tests cover planning, fusion, caps, snippets, API contracts, citations,
  and empty answers. Live pgvector/PostgreSQL tests cover fake connector → index
  → hybrid search → history plus filter narrowing and cross-tenant denial.

Search-engine master tasks complete: `9/9`, `P9-001` through `P9-009`, for the
local-index backend scope. Production model synthesis, labeled quality gates,
and user-facing search/chat screens remain later phases.

## Explicitly unimplemented inventory

The following do not exist in production code yet:

- General SQLAlchemy models/repositories, transaction services, or seed data
  outside the implemented authentication store.
- Password recovery, reauthentication, OAuth, encrypted provider credential
  storage, authentication abuse throttling, and account profile editing.
- Any real provider client or provider data synchronization.
- Binary PDF/Office/email parsers, OCR, production semantic embeddings, and
  provider-specific extraction policies.
- Conversations, production model calls, streamed answers, or conflict-aware
  synthesis.
- The other 42 product `/v1` endpoints, web dashboard, search/chat UI,
  connection UI, or source browser.
- Desktop folder selection, scanner, watcher, manifest sync, or offline queue.
- Disconnect/account deletion workflows, billing, production deployment, or
  operational dashboards.

## Backfill sequence

Backfill follows executable dependencies, while numbered documents continue to
define design ownership:

| Order | Backfill slice | Definition of done |
| --- | --- | --- |
| `B0` | Truthful status tracking | This ledger, corrected README/workflow language, automated status validation, clean tests, committed and pushed. |
| `B1` | Stage 04 database runtime | Migration toolchain; all 33 specified tables/types/constraints/indexes; tenant RLS; upgrade/downgrade and schema-contract tests against PostgreSQL/pgvector; documented rollback. |
| `B2` | Stage 05 API platform primitives | Versioned router, RFC 9457 problem details, request IDs, serialization, cursor helpers, idempotency primitives, auth/workspace dependency interfaces, and contract tests. Product endpoints remain closed until their services exist. |
| `B3` | Stage 08/09 security and auth design checkpoint | Complete the owning security/auth specifications before implementing credentials, sessions, authorization middleware, and OAuth. **Complete:** both documents now define implementation-ready controls and test matrices enforced by `validate-security-auth.sh`. |
| `B4` | Authentication vertical slice | **Complete:** users, password/session security, register/verify/login/refresh/logout/me endpoints, workspace bootstrap, minimal UI, PostgreSQL tests, and a live Docker smoke test. |
| `B5` | Stage 07 indexing design and implementation | **Complete:** deterministic text/Markdown extraction, chunking, deduplication, local embeddings, durable jobs, authoritative worker claims, atomic promotion, and fake-connector-to-index PostgreSQL tests. |
| `B6` | Stage 03 search implementation | **Complete:** tenant-safe exact/FTS/vector/trigram retrieval, filters, fusion/ranking, bounded context, extractive citations, search API/history, and unit/PostgreSQL tests. |
| `B7` | Real provider and desktop slices | Google, GitHub, then local desktop behavior, each certified by the Connector SDK suite and integrated end to end. |

The active backfill item is always the first unfinished row. Backfills `B0`
through `B6` are complete; `B7` is next. No later slice may be reported
complete because a model, interface, fake, document, or application shell
exists.

## Evidence commands

The audit baseline is reproducible with:

```sh
pnpm check
pnpm build
./scripts/test-backend.sh
./scripts/test-connector-sdk.sh
./scripts/test-database.sh
./scripts/test-desktop-rust.sh
docker compose build api web
python3 scripts/smoke-auth.py
```

Every backfill commit must update this ledger, run the checks relevant to its
changed risk surface, push to `origin/main`, and verify the remote commit before
the next slice begins.
