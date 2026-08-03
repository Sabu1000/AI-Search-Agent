# Implementation Status

**Last audited:** 2026-08-03

**Audited commit:** `2b07d50`

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
| `01_PROJECT_SPEC.md` | Complete | Not started as an MVP | The 11 MVP requirements have testable acceptance criteria, but none passes end to end. See the requirement audit below. |
| `02_SYSTEM_ARCHITECTURE.md` | Complete | Partial | Process shells and local dependencies exist. Durable workflows, repositories, authorization enforcement, queues, connector orchestration, indexing, search, and answer services do not. |
| `03_SEARCH_ENGINE_DESIGN.md` | Complete | Not started | No lexical retrieval, vector retrieval, fusion, reranking, context builder, citation builder, grounding evaluator, or search endpoint exists. |
| `04_DATABASE_SCHEMA.md` | Complete | Not started | PostgreSQL/pgvector starts and extensions initialize, but there is no migration framework, no application tables, no RLS policy, no seed path, and no migration test. Current count: `0/33` specified tables. |
| `05_API_SPECIFICATION.md` | Complete | Not started | FastAPI exposes liveness/readiness only. None of the `49` catalogued `/v1` product endpoints is implemented. Shared auth, problem details, idempotency, pagination, rate limiting, and SSE behavior also remain. |
| `06_CONNECTOR_FRAMEWORK.md` | Complete | SDK implemented; provider ecosystem partial | The typed SDK, four change variants, registry, retry policy, runtime stream validator, fake connector, Docker gate, and CI job are implemented. Real Gmail, Drive, GitHub, and local-file connectors, OAuth persistence, scheduling, logging, and metrics are not. |

The next numbered design document is `07_INDEXING_PIPELINE.md`, but it is
paused while the database/API prerequisites are backfilled. A document may be
the next specification without being the next safe implementation task.

## MVP requirement audit

| Requirement | Status | Implemented evidence | Missing acceptance path |
| --- | --- | --- | --- |
| `AUTH-001` | Not started | None | User persistence, password hashing, sessions/tokens, register/login/logout UI and APIs, safe errors, and end-to-end tests. |
| `DESKTOP-001` | Partial | Tauri React/Rust shell compiles and is container-checked. | Signed installers, folder selection, root authorization, scanner/watcher, device registration, offline queue, removal/deletion flow, and platform tests. |
| `GOOGLE-001` | Not started | Canonical SDK provider contracts only. | OAuth scopes/callbacks, encrypted credentials, Gmail and Drive clients, selections, full/incremental sync, revocation, UI, and provider contract tests. |
| `GITHUB-001` | Not started | Canonical SDK provider contract only. | GitHub App, installation/repository selection, API client, webhooks, reconciliation, access removal, UI, and provider contract tests. |
| `SYNC-001` | Partial | SDK defines deterministic changes, retry classes, terminal cursors, health results, and contract tests. | Durable jobs/cursors, scheduler, worker integration, status APIs/UI, sanitized persisted errors, replay tests, and operational metrics. |
| `SEARCH-001` | Not started | Search behavior is specified only. | Authorized FTS/vector retrieval, filters, fusion/ranking, API, UI states, evaluation data, and isolation tests. |
| `ANSWER-001` | Not started | Answer/grounding behavior is specified only. | Model gateway, context construction, streaming generation, claim support checks, insufficient-evidence behavior, UI, and evaluation. |
| `CITATION-001` | Not started | Citation shape and behavior are specified only. | Stored source spans, citation builder, authorization recheck, safe target opening, API/UI rendering, and correctness tests. |
| `CONNECTION-001` | Not started | SDK has deletion and permission-change event types. | Connection storage, credential revocation, sync cancellation, cascade jobs, progress API/UI, 24-hour enforcement, and end-to-end tests. |
| `ACCOUNT-001` | Not started | Database/API deletion contracts are specified only. | Immediate access termination, durable deletion workflow, receipt/status, export, backup-retention handling, and deadline alerts/tests. |
| `SAFETY-001` | Partial | Product and SDK are structurally read-only; SDK validates URLs/JSON and masks credentials. | Authorization middleware, tenant/RLS enforcement, content sanitization boundaries, prompt-injection fixtures through retrieval/answering, log redaction, and cross-user tests. |

Current end-to-end MVP acceptance: `0/11` requirements. Partial means useful
prerequisite code exists; it does not increase the end-to-end count.

## Implemented code inventory

### Foundation

- `apps/web`: tested Next.js production shell.
- `apps/api`: tested FastAPI liveness/readiness service and worker entry point.
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

## Explicitly unimplemented inventory

The following do not exist in production code yet:

- Application database migrations, SQLAlchemy models/repositories, RLS
  policies, or seed data.
- Account registration, login, sessions, authorization middleware, OAuth, or
  encrypted provider credential storage.
- Any real provider client or provider data synchronization.
- Extraction, parsing, chunking, embedding, deduplication, or indexing workers.
- Keyword, vector, hybrid, filtered, or reranked search.
- Conversations, model calls, streamed answers, grounding, or citations.
- Product `/v1` endpoints, web dashboard, search/chat UI, connection UI, or
  source browser.
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
| `B3` | Stage 08/09 security and auth design checkpoint | Complete the owning security/auth specifications before implementing credentials, sessions, authorization middleware, and OAuth. |
| `B4` | Authentication vertical slice | Users, password/session security, register/login/refresh/logout/me endpoints and minimal UI with integration tests. |
| `B5` | Stage 07 indexing design and implementation | Extraction/chunking/deduplication/embedding queue contracts and an end-to-end fake-connector-to-index path. |
| `B6` | Stage 03 search implementation | Tenant-safe FTS/vector retrieval, fusion, filters, ranking, context, citations, API, and evaluation tests. |
| `B7` | Real provider and desktop slices | Google, GitHub, then local desktop behavior, each certified by the Connector SDK suite and integrated end to end. |

The active backfill item is always the first unfinished row. Backfill `B0` is
complete with this tracked checkpoint, and `B1` is next. No later slice may be
reported complete because a model, interface, fake, document, or application
shell exists.

## Evidence commands

The audit baseline is reproducible with:

```sh
pnpm check
pnpm build
./scripts/test-backend.sh
./scripts/test-connector-sdk.sh
./scripts/test-desktop-rust.sh
docker compose build api web
```

Every backfill commit must update this ledger, run the checks relevant to its
changed risk surface, push to `origin/main`, and verify the remote commit before
the next slice begins.
