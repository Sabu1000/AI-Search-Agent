# Implementation Status

**Last audited:** 2026-11-24

**Earlier fixture-backed milestone:** [`c8a644c`][feature-baseline], which
builds on the certified Drive backend checkpoint `64f4678`. This audit
distinguishes synthetic provider responses and local PostgreSQL verification
from live provider and release acceptance.

[feature-baseline]: https://github.com/Sabu1000/AI-Search-Agent/commit/c8a644cf4899cdd574f11a97817a8bdc7a64ff65

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
| `02_SYSTEM_ARCHITECTURE.md` | Complete | Partial | Local topology, schema ordering, fail-closed HTTP boundaries, workspace authentication/authorization, durable Gmail/Drive/selected-GitHub indexing, and hybrid search exist. Desktop sync, model streaming, provider management UI, and remaining product services are open. |
| `03_SEARCH_ENGINE_DESIGN.md` | Complete | Local-index backend implemented; product phase partial | PostgreSQL exact/FTS/pgvector/trigram retrieval, typed filters, RRF, deterministic ranking, bounded extractive claims/citations, history, and safe empty answers pass unit and local PostgreSQL tests. Signed-in web results support workspace and provider filters. Production semantic/model adapters, full filter and answer UI, evaluation, and cache remain. |
| `04_DATABASE_SCHEMA.md` | Complete | Database runtime implemented; phase partial | Alembic creates `33/33` specified tables. The runtime adds GitHub authorization and sync migrations to the prior index/Gmail/Drive claim functions and forced RLS. Compose migrates before API startup; readiness checks revision `0009_github_sync_runtime`. Seed data, backup automation, and Phase 3 review remain. |
| `05_API_SPECIFICATION.md` | Complete | API platform and selected product routes implemented | FastAPI provides OpenAPI 3.1, strict serialization, RFC 9457 problems, signed cursors, idempotency primitives, and workspace authentication. `24/49` catalogued product operations include application and recovery auth, Google/GitHub authorization, signed GitHub webhook ingress, search, safe connection reads, private conversation management, and opt-in native device registration. Rate limiting, SSE, and remaining operations are open. |
| `06_CONNECTOR_FRAMEWORK.md` | Complete | Gmail and selected-root Drive backend phases complete; provider ecosystem partial | The GitHub backend now verifies selected-repository grants, ingests bounded repository and discussion snapshots, reconciles absence after success, and closes visibility on failed verification. Desktop has a bounded scanner core. Live provider checks, native desktop sync, Drive change polling, and shared operational controls remain. |
| `07_INDEXING_PIPELINE.md` | Complete | Normalized text/Markdown and bounded provider extraction paths implemented | Deterministic chunking/deduplication, local embeddings, durable leases, atomic promotion, unchanged skips and deletion purging pass unit and local PostgreSQL tests. Drive PDF/DOCX/XLSX/PPTX and bounded GitHub repository/discussion data feed the index. Desktop indexing handoff and production embedding providers remain open. |
| `08_SECURITY_AND_PRIVACY.md` | Complete | Partial controls | The threat model, data classification, tenant/cryptographic boundaries, browser/input/model defenses, audit/redaction policy, deletion/backup rules, and security release gates are implementation-ready. Existing RLS, strict API boundaries, secret validation, and containerized tests implement only part of the required controls. |
| `09_AUTH_AND_OAUTH.md` | Complete | Application authentication and provider authorization slices implemented | Google uses single-use callbacks, PKCE S256, exact read-only scopes, envelope-encrypted credentials, and worker-side refresh. The GitHub App path verifies workspace/session state, installation and repository access, and persists grants without provider tokens. Reauthentication, Google login, live GitHub authorization, revocation, production KMS, throttling, and full security review remain. |

The active implementation slice is provider and desktop integration through
backfill `B7`. The tested Gmail backend phase is complete, including Google
authorization, full/incremental sync, extraction, normalization, reconciliation,
derived-data deletion, and error recovery. Bounded Drive folder traversal, PDF,
DOCX, native Docs/Sheets/Slides extraction, authoritative deletion handling, and
the selected-root backend phase review are complete. GitHub App authorization
and bounded repository/discussion polling now feed the index. The remaining
integration work includes provider revocation, management UI, live
provider acceptance, and native desktop sync.

## MVP requirement audit

| Requirement | Status | Implemented evidence | Missing acceptance path |
| --- | --- | --- | --- |
| `AUTH-001` | Implemented | A visitor can register, receive and submit a single-use email proof, receive an owner workspace, log in, inspect memberships, rotate a session, log out, and sign back in through the minimal web UI and six `/v1/auth` endpoints. Argon2id, generic failures, signed 15-minute JWT access tokens, hashed opaque refresh/CSRF tokens, exact-origin CSRF checks, non-superuser RLS, route/unit/database tests, and the Docker smoke test cover the acceptance path. | No missing MVP acceptance path. Password recovery, OAuth login, rate limiting, reauthentication, and the complete Phase 2 security review are later hardening/expansion tasks. |
| `DESKTOP-001` | Partial | Tauri React/Rust shell and bounded Unix scanner core exist. The scanner accepts a selected root, rejects symlinks and cross-filesystem traversal, excludes unsupported/unsafe files, and emits relative-path metadata and hashes without content payloads. | Native picker and root authorization, registration, persistence and watchers, offline queue and upload, removal/deletion, signed installers, and cross-platform testing including Windows. |
| `GOOGLE-001` | Partial | The Google authorization endpoints implement workspace/session binding, PKCE S256, exact Gmail/Drive read-only scopes, encrypted credentials, and per-family jobs. The Gmail backend phase is complete. Drive traverses selected roots through bounded durable jobs, supports shared drives without following shortcuts, extracts bounded PDF/DOCX text, exports native Docs/Sheets/Slides through bounded structure-aware parsers, and reconciles absent file IDs only after a successful whole-tree scan. | Provider connection/selection UI, incremental Drive change-token polling, and a live Google sandbox smoke test. |
| `GITHUB-001` | Partial | Opt-in App authorization binds encrypted single-use state to the workspace/session and saves only verified selected grants. Bounded file/README and issue/PR/review ingestion, durable polling, atomic reconciliation and freshness fences are covered by synthetic GitHub and local PostgreSQL tests. | Individual repository grant revocation and deletion, full connection management, live sandbox smoke, and phase review. |
| `SYNC-001` | Partial | PostgreSQL leases drive indexing, Gmail full/history sync, Drive page jobs, and selected GitHub snapshots. Authoritative complete scans reconcile missing data; failed GitHub scans hide stale results without treating failure as proof of absence. | Incremental Drive change polling, generalized provider scheduling, status APIs/UI, cross-provider replay, and operational metrics. |
| `SEARCH-001` | Partial | Authorized hybrid retrieval, filters, ranking, empty results and history are tested. Local PostgreSQL tests cover provider fixtures, indexed content, deletion or stale-authorization visibility, and tenant isolation. Signed-in web search supports basic workspace/provider filtering. | Live-provider certification, remaining filter and search UI scope, production semantic embeddings, and labeled retrieval evaluation. |
| `ANSWER-001` | Partial | Bounded authorized context produces extractive material claims with one-to-one citations and an explicit `no_authorized_results` insufficient-evidence response. | Production model gateway, synthesis/conflict evaluator, streaming UI, and measured grounding evaluation. |
| `CITATION-001` | Partial | The API links excerpts to authorized source/chunk IDs and allowlisted HTTPS targets. The web results panel opens allowlisted provider URLs. | Answer-level citation UI, target reauthorization endpoint, revoked-target end-to-end tests, and measured citation correctness. |
| `CONNECTION-001` | Partial | Connection, exact scope, cursor, deletion, job, and outbox storage exists; Google credentials use per-record AES-256-GCM envelope encryption, are decrypted only for claimed work, and refreshed credentials are re-encrypted. | Disconnect/revocation, KMS-backed key wrapping and rotation, sync cancellation, cascade workers, progress API/UI, and 24-hour enforcement. |
| `ACCOUNT-001` | Partial | Workspace state and durable deletion-request/job/outbox/audit storage exist. | Immediate access-termination service, cascade workers, receipt/status, export, backup-retention handling, and deadline alerts/tests. |
| `SAFETY-001` | Partial | Product and SDK are structurally read-only; strict API models reject unknown input; privacy-safe problems avoid echoing inputs and internal errors; database-backed auth/workspace dependencies fail closed; signed cursors and idempotency hashes bind principal/workspace context; identity and tenant tables use tested forced RLS. | Content sanitization boundaries, prompt-injection fixtures through retrieval/answering, log redaction, and full cross-user service tests. |

Current end-to-end MVP acceptance: `1/11` requirements. Partial means useful
prerequisite code exists; it does not increase the end-to-end count.

## Implemented code inventory

### Foundation

- `apps/web`: Next.js authentication, workspace selection, and indexed result search.
- `apps/api`: tested FastAPI liveness/readiness service and durable indexing worker.
- `apps/desktop`: Tauri/React shell plus a separately tested Rust manifest scanner.
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

Connector framework master tasks complete: `7/11`: `P4-001` through `P4-004`,
plus `P4-006` through `P4-008`. Scheduling, production
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
  readiness rejects any revision other than `0009_github_sync_runtime`.
- Local PostgreSQL 16/pgvector checks cover migrations, tenant isolation,
  authentication, Google and GitHub authorization/sync, indexing, and search.

Database master tasks complete: `12/15`: `P3-001` through `P3-012`. Seed data,
backup implementation, and the Phase 3 review remain open. These tables are
production schema, but they do not by themselves implement repositories or
user-visible workflows.

### API platform

- FastAPI publishes OpenAPI 3.1 and mounts a `/v1` router with six authentication
  operations, two Google authorization operations, two GitHub authorization
  operations, and hybrid search. The selected-root Drive checkpoint predated
  the GitHub routes (`9/49` catalogued operations at that time).
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
- The backend CI gate runs tests plus Black, Ruff, and strict mypy in the Docker
  target. The checkpoint evidence below labels the recorded totals by scope.

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
- Historical Drive-era evidence includes `160 backend tests at 90.97% line coverage`,
  `20 PostgreSQL integration tests`, web/build/health checks, and
  `scripts/smoke-auth.py`. These are snapshot figures, not current suite totals.

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
local-index backend scope. The web results panel supports provider and workspace
selection. Production model synthesis, labeled quality gates, answer/chat
presentation, and the remaining filters are open.

### Gmail full and incremental synchronization

- The read-only Gmail client refreshes expiring Google credentials, maps
  provider failures to sanitized SDK codes, reads a profile history ID, lists
  messages 25 at a time, and fetches only full message representations.
- Deterministic normalization preserves subject, sender, recipients, sent time,
  labels, thread/history IDs, safe canonical targets, and inert plain text.
  The MIME-aware parser prefers plain alternatives, decodes encoded headers and
  declared character sets, safely extracts visible HTML, normalizes Unicode and
  controls, excludes attachment bodies, and removes only high-confidence quoted
  history/signature regions.
- Attachments use message-plus-part stable identities. Bounded textual attachment
  bodies and separately stored message text are fetched through Gmail's read-only
  attachment endpoint. Unsupported binary or oversized parts become descriptor
  sources with explicit status and original MIME type, without fetching their
  content.
- An allowlisted, bounded metadata mapper preserves internal/RFC dates, labels,
  thread/message relationships, attachment details, and decoded sender,
  recipient, Bcc, and Reply-To identities. The shared SDK carries those people
  structurally; indexing refreshes `sources` and `source_people` even when the
  content version is unchanged, and search person filters query those rows.
- Revisions `0004_gmail_sync_runtime`, `0005_gmail_incremental_sync`, and
  `0006_gmail_reconciliation` add the authoritative, fixed-search-path sync-job
  claim function and per-source full-scan marker for the non-`BYPASSRLS` worker.
- Each provider page durably queues idempotent index jobs before a continuation
  job is committed. Continuation progress is envelope-encrypted in the job payload,
  while a one-way token fingerprint supplies idempotency. Only the final page
  commits the Gmail history cursor and successful-sync timestamp.
- Revoked refresh credentials mark the connection
  `reauthorization_required`; transient provider failures enter bounded durable
  retry, and raw tokens/provider bodies are never written to jobs or errors.
- Completed full sync schedules bounded Gmail history polling. Incremental pages
  refetch adds and label changes, purge deleted messages and their attachment
  sources, encrypt continuation progress, and commit the returned history cursor
  only on the last page.
- Full-sync pages mark each observed source with a deterministic encrypted run
  marker. Only successful completion purges active Gmail sources absent from the
  authoritative listing, so a failed or partial scan cannot erase unseen mail.
- Deletion retains only a scrubbed source tombstone and removes people rows,
  versions, chunks, embeddings, citations, and pending index work. Retryable
  failures honor bounded `Retry-After` or exponential full jitter; invalid,
  permanent, authentication, and exhausted failures enter explicit durable states.
- An expired Gmail cursor records `CURSOR_INVALID` and schedules a controlled
  full recovery without inventing or advancing a cursor.
- The PostgreSQL end-to-end test proves synthetic full pages → ready index →
  incremental update/delete → re-index/tombstone → committed cursor.

Gmail master tasks complete: `11/11`: `P5-001` through `P5-011`, for the tested
all-mail backend scope. Binary PDF/Office attachment parsing, configurable label
selection UI, and a live Google sandbox smoke test remain later integrations.

### Google Drive folder synchronization and document extraction

- The Drive client requests the existing exact read-only scope and lists at most
  100 children from one validated selected folder per provider request.
- Explicit field projection and strict parsing preserve stable file IDs, NFC
  names, parents, owners, MIME types, modified times, sizes, shared-drive IDs,
  and allowlisted Google links. Duplicate or oversized pages fail closed.
- My Drive is the default root; a job can carry one explicit selected or shared-
  drive root. Every discovered subfolder becomes a deterministic durable job.
- Folder IDs, logical paths, shared-drive IDs, and page tokens remain inside an
  encrypted progress envelope. A non-secret sync-run UUID coordinates completion.
- Shortcuts are indexed as inert descriptors and are never followed, so they
  cannot escape a selected root. Returned children must name the current parent.
- Files are indexed by immutable Drive ID with a stable descriptor, logical path,
  owner identities, and safe canonical target. Format-specific content replaces
  the descriptor in later Drive tasks without changing source identity.
- Revision `0007_drive_sync_runtime` adds the narrowly granted Drive claim
  function. The worker marks the connection successful only after all discovered
  folder/page jobs finish.
- A live PostgreSQL test proves root → subfolder traversal, encrypted job state,
  delayed whole-tree completion, extracted PDF text indexing, migration lifecycle,
  and tenant-safe storage.
- Regular PDFs use the read-only Drive media endpoint. Metadata size prevents
  unnecessary oversized downloads, while declared and streamed responses are
  independently capped at 20 MiB.
- The PDF parser is bounded to 500 pages and 4.9 million extracted characters.
  Text is Unicode-normalized before entering the deterministic indexing pipeline.
- Empty, encrypted, malformed, oversized, excessive-page, and truncated PDFs keep
  an inert descriptor with an explicit extraction status instead of failing the
  folder page or silently dropping the source.
- Uploaded DOCX files preserve headings, paragraphs, lists, and table rows/cells.
  The parser bounds downloaded bytes, ZIP member count, total expanded bytes,
  document XML bytes, and extracted characters, and uses defused XML handling.
- Native Google Docs use the read-only export endpoint to produce DOCX bytes for
  that same parser under Google's 10 MB export response limit. The indexed source
  retains its native file ID, MIME type, logical path, owner metadata, and
  canonical Google link.
- Native Google Sheets export to bounded XLSX parsing that preserves sheet names,
  non-empty rows, cell values, booleans, formulas, and cached results.
- Native Google Slides export to bounded PPTX parsing that preserves numeric slide
  order, visible text, and speaker notes.
- Both Office export paths cap raw/expanded/XML/text sizes and structural counts,
  block XML entities, retain native identity/MIME type, and record safe fallback
  statuses for malformed, empty, excessive, or truncated content.
- Every Drive page marks seen sources with a per-run UUID. Only a successfully
  completed whole-tree traversal schedules retryable authoritative reconciliation;
  absent sources are scrubbed and all versions, chunks, embeddings, people,
  citations, and pending index work are purged.
- The live PostgreSQL traversal test proves PDF, uploaded DOCX, and exported Docs,
  Sheets, and Slides text reach ready searchable versions, then proves an absent
  Drive file is tombstoned with its derived index data removed.

Drive master tasks complete: `10/10`: `P6-001` through `P6-010`. The tested
selected-root backend milestone passes its
[phase review](DRIVE_PHASE_REVIEW.md).

### GitHub App authorization and bounded repository sync

- The App HTTP gateway uses PKCE code exchange, verifies installation and user
  repository access, and holds scoped, short-lived read-only installation
  credentials only in memory. Opt-in start/callback routes persist encrypted
  single-use workspace/session state and audited repository grants, never
  provider tokens. A connection begins pending until a snapshot succeeds.
- Initial grants queue durable repository sync. The worker reads the verified
  default branch at one pinned commit, normalizes supported README/docs/code,
  and separately ingests bounded issues, pull requests, and discussions. It
  excludes unsupported, oversized, generated, and likely secret-bearing files.
- A complete snapshot stages content and reconciles absent sources atomically
  before renewing repository visibility. Failed verification closes
  visibility without treating a failed scan as evidence of deletion.
  Search results require an active, selected grant verified within 15 minutes.
- Migrations `0008_github_app_authorization` and `0009_github_sync_runtime`
  supply grant and sync persistence. Unit tests use synthetic GitHub responses.
  Local PostgreSQL tests cover authorization, worker/index/search behavior and
  the visibility fence; no live GitHub App credentials were used.
- Signed webhook ingress stores bounded, content-free replay records and
  coalesces provider reconciliation. Scope events fence in-flight snapshots
  before they can publish. Repository authority-change events target only
  that selection; suspended workspaces cannot fetch or publish a sync.
  Search revalidates each source/chunk pair immediately before returning it.
  Follow-up checks remove derived content for a verified App uninstall, not
  ambiguous repository failures. The full
  revocation and live acceptance matrix remains open.

### Signed-in web search and desktop scanner

- The browser results panel narrows indexed search by workspace and provider.
  It displays typed snippets and opens only allowlisted provider HTTPS source
  links and extractive answers with grounded citations. A GitHub form starts
  consent for selected IDs and reports pending sync without claiming success.
  The panel does not implement model synthesis or the full set of filters.
- The [desktop scanner](../apps/desktop/src-tauri/scanner/README.md) emits
  bounded metadata and hashes for allowed text under a caller-approved root.
  The local SQLite baseline compares accepted snapshots without advancing
  until acknowledgement. The core has no uploader or picker and does not
  send file contents or absolute paths. It fails closed on unsupported
  platforms; native packaging and end-to-end sync are not certified.

### Private connection and conversation metadata

Authenticated workspace members can list and read safe connection metadata and
status with signed pagination. The API excludes credentials and filesystem
paths. Private conversations can be created idempotently, listed, and opened
by their owner with current workspace and session checks. Confirmed deletion
clears private messages and citations and provides an owner-only operation
receipt; shared sources are unaffected. A private user-turn persistence
adapter exists, but no public message append/generation or SSE is wired.

Opt-in password recovery uses rate-limited generic requests and hashed,
expiring one-time tokens; a valid reset revokes all session families
atomically. Synthetic mail and PostgreSQL tests do not certify production
SMTP or Redis operation.

The grounding boundary packs bounded untrusted snippets and accepts only
strict, still-authorized citation references. It makes no model request and
does not verify semantic entailment. The converse/model path remains open.

The desktop scanner also contains an offline Ed25519 contract for
registration proofs and exact-byte request signatures, verified against
cross-language fixtures. Opt-in native registration verifies a 60-second
single-use challenge and returns an opaque credential once. It creates a
pending local-files connection. Device heartbeat, native keychain and file
transport remain open.

### First offline retrieval evaluation

The [synthetic, versioned evaluation](../apps/api/tests_database/fixtures/SEARCH_EVALUATION.md)
runs the actual local indexing and search API against PostgreSQL under the
tenant role. It covers 28 labeled cases, including foreign-tenant and
no-answer cases. This fixture reports 87.5% overall recall at 10, 40% recall in
its semantic subset, no unauthorized references, and abstention in all four
labeled no-answer cases. Citation checks establish provenance on this fixture
rather than independent entailment. A held-out benchmark, production semantic
embeddings and live provider baselines remain open.

## Explicitly unimplemented inventory

The following are still open:

- A general ORM model layer, seed data, and automated database backups.
- Reauthentication, Google login, production KMS, auth
  abuse throttling, and account profile editing.
- Incremental Drive change-token polling; Gmail/Drive connection UI and
  live provider smoke tests.
- GitHub individual repository revocation, live acceptance, and operational
  webhook retention and recovery validation.
- Binary Gmail attachment extraction, OCR, production semantic embeddings,
  model synthesis/conflict checks, conversation messages, and streamed answers.
- Unimplemented catalogued API operations, connection management, source
  browsing, advanced search filters, and conversation citation lifecycle.
- Native desktop folder picker, persisted registration/approval, watchers,
  offline queue, upload, and server-side manifest reconciliation.
- Disconnect/account deletion workflows, billing, production deployment,
  and operational dashboards.

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
| `B7` | Real provider and desktop slices | **In progress:** Gmail and selected-root Drive backend phases are complete for their tested scopes. GitHub has tested authorization, bounded snapshot sync, and signed webhook replay handling; live acceptance remains open. Web results/citations and the desktop scanner plus manifest baseline are partial product slices. Full provider management and native desktop sync remain. |

The active backfill item is always the first unfinished row. Backfills `B0`
through `B6` are complete; `B7` is active. No later slice may be reported
complete because a model, interface, fake, document, or application shell
exists.

## Evidence commands

The project gates for this audit are:

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

At `c8a644c`, the recorded fixture-backed gates included 310 backend unit
cases and 21 local PostgreSQL cases, plus targeted reruns for the provider
access recheck. These validate the tested code and database paths, not live
GitHub/Gmail/Drive account acceptance or production deployment.

For each release milestone, update the ledger, run checks relevant to the
changed scope, and verify the pushed commit. Keep feature branch evidence
separate from what is merged to `main` or deployed.
