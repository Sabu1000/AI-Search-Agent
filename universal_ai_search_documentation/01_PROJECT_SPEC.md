# Project Specification

## Product name

Universal AI Search

## Problem

Knowledge is fragmented across local folders, email, cloud drives, and
source-control systems. Users waste time remembering where information lives
and manually searching each system.

## Product promise

A user connects approved data sources and asks a natural-language question.
The system returns a grounded answer with direct citations to the original
sources.

## Product principles

- **Grounded by default:** answers are based only on content the product
  retrieved for the current user and include supporting citations.
- **Read-only:** version 1 never edits provider content, sends messages, merges
  code, or lets retrieved content trigger an action.
- **User-controlled access:** users select the folders, accounts, and
  repositories they connect and can revoke that access.
- **Secure isolation:** a user can never retrieve another user's content.
- **Honest failure:** when evidence is absent or insufficient, the product says
  so instead of inventing an answer.

## Primary users

- Individual professionals
- Software engineers
- Researchers
- Founders and operators

Small teams are a later-release audience. Shared workspaces and
organization-wide administration are not part of this MVP.

## Functional requirements and acceptance criteria

| ID | Requirement | Acceptance criteria |
| --- | --- | --- |
| `AUTH-001` | A visitor can create an account and sign in. | A new user can register with valid credentials, start an authenticated session, sign out, and sign back in. Invalid or duplicate credentials produce a safe, actionable error without exposing account existence unnecessarily. |
| `DESKTOP-001` | A user can install the desktop agent and select local folders. | Supported macOS and Windows packages install successfully. Only explicitly selected folders are scanned; removing a folder stops future sync and offers deletion of its indexed content. |
| `GOOGLE-001` | A user can connect Gmail and Google Drive through Google OAuth. | The consent flow requests only documented read-only scopes. A successful connection imports content the user authorized; denial or revocation leaves the account usable and displays recovery guidance. |
| `GITHUB-001` | A user can install a GitHub App on selected repositories. | Only repositories selected during installation are synchronized. Adding or removing repository access is reflected on the next sync without granting broader organization access. |
| `SYNC-001` | A user can see connector sync status and errors. | Each connection displays its last successful sync, current state, and an actionable sanitized error when a sync fails. Retrying cannot create duplicate indexed items. |
| `SEARCH-001` | A user can search across connected sources and filter results. | Keyword queries return only the current user's authorized content. Person, date, repository, folder, source, and file-type filters work individually and in supported combinations, including a clear no-results state. |
| `ANSWER-001` | A user can ask a natural-language question and receive a grounded answer. | Every material factual claim is supported by one or more citations. If retrieved evidence is insufficient or conflicting, the answer communicates that limitation rather than filling the gap from unsupported model knowledge. |
| `CITATION-001` | A user can inspect the source behind each answer. | Every citation identifies its provider and source, shows enough context to understand the match, and opens the original provider item or approved local-file location when it is still accessible. Broken or revoked targets fail safely. |
| `CONNECTION-001` | A user can disconnect a provider and delete its indexed data. | A confirmed disconnect stops new syncs, revokes or removes stored credentials, queues deletion of derived content, reports progress, and completes deletion within 24 hours. Other providers remain searchable. |
| `ACCOUNT-001` | A user can delete the account and all retained data. | After explicit confirmation, access ends immediately and all user content, derived data, credentials, and active sessions are deleted within 24 hours, subject only to documented backup-retention rules. |
| `SAFETY-001` | Connected and retrieved content is treated as untrusted data. | Content cannot invoke tools, alter system instructions, initiate provider writes, or bypass authorization. Prompt-injection test fixtures do not cause actions or cross-user disclosure. |

## Cross-cutting behavior

- Authorization is checked before loading source content, search results, chat
  history, citations, connection state, or deletion state.
- Provider credentials and content are never exposed in logs or user-visible
  errors.
- Long-running imports and deletions expose status rather than holding an HTTP
  request open.
- Retried sync, indexing, webhook, and deletion jobs are idempotent.
- Accessibility, empty, loading, offline, permission-revoked, and failure states
  are part of the feature—not follow-up polish.

## Non-goals for MVP

- Sending email
- Editing files
- Autonomous agents
- Slack, Notion, Microsoft 365, Jira, or Dropbox connectors
- Mobile applications
- Enterprise SSO
- Organization-wide permissions
- OCR for scanned documents
- Shared team workspaces
- Self-hosted deployment

## Success metrics

| Metric | MVP release gate | Measurement |
| --- | --- | --- |
| Retrieval Recall@10 | `>= 0.85` | Mean Recall@10 across the versioned, labeled retrieval evaluation set, reported overall and by source type. |
| Citation correctness | `>= 0.95` | Supported citations divided by evaluated citations; a citation is supported only when its source entails the associated material claim. |
| Unsupported material claim rate | `<= 0.03` | Material claims lacking support in their cited or retrieved context divided by all evaluated material claims. |
| Median query latency | `<= 6 seconds` | Median server-observed time from accepting a question to the final answer event in a production-like environment, reported alongside p95. |
| Initial connector sync success | `>= 0.98` | Completed initial syncs divided by eligible initial sync attempts over a rolling 7-day window, with provider outages reported separately rather than silently excluded. |
| Incremental sync success | `>= 0.995` | Completed incremental sync runs divided by eligible runs over a rolling 7-day window, deduplicated by scheduled job. |
| Account deletion completion | `<= 24 hours` | Elapsed time from confirmed deletion request to deletion completion for every account; failures alert operators and remain visible until resolved. |

Evaluation datasets, sampling, confidence intervals, and regression procedures
are specified in [`12_TESTING_AND_EVALUATION.md`](12_TESTING_AND_EVALUATION.md).

## MVP limits and required behavior

| Limit | Value | Behavior at the limit |
| --- | --- | --- |
| Indexed items | 25,000 per user | Stop accepting additional items, preserve already indexed content, and explain how to remove sources or upgrade. |
| Extracted content | 10 GB per user | Reject new content before exceeding the quota and report current usage without exposing internal storage paths. |
| Individual file size | 100 MB | Skip the file with a visible reason; do not partially index it. |
| Free-user request rate | 30 requests per minute | Return a retryable rate-limit response with a retry time; do not count rejected requests as successful usage. |
| Connector permissions | Read-only | Reject configurations requiring provider write scopes. |

Exact API error shapes and quota headers belong to
[`05_API_SPECIFICATION.md`](05_API_SPECIFICATION.md).

## Core screens and traceability

| Screen | Requirements served |
| --- | --- |
| Marketing and pricing | Product promise, limits, and read-only boundary |
| Sign up and login | `AUTH-001` |
| Onboarding | `GOOGLE-001`, `GITHUB-001`, `DESKTOP-001` |
| Connections | `GOOGLE-001`, `GITHUB-001`, `SYNC-001`, `CONNECTION-001` |
| Search and chat | `SEARCH-001`, `ANSWER-001`, `CITATION-001`, `SAFETY-001` |
| Sources browser | `SEARCH-001`, `CITATION-001` |
| Sync status | `SYNC-001` |
| Privacy and deletion settings | `CONNECTION-001`, `ACCOUNT-001` |
| Billing | MVP limits and usage visibility |

## Requirement ownership in later documents

| Topic | Owning document |
| --- | --- |
| Component boundaries and technology choices | [`02_SYSTEM_ARCHITECTURE.md`](02_SYSTEM_ARCHITECTURE.md) |
| Retrieval and ranking behavior | [`03_SEARCH_ENGINE_DESIGN.md`](03_SEARCH_ENGINE_DESIGN.md) |
| Persistence and tenant isolation | [`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md) |
| HTTP and streaming contracts | [`05_API_SPECIFICATION.md`](05_API_SPECIFICATION.md) |
| Security controls and deletion guarantees | [`08_SECURITY_AND_PRIVACY.md`](08_SECURITY_AND_PRIVACY.md) |
| Login and provider authorization flows | [`09_AUTH_AND_OAUTH.md`](09_AUTH_AND_OAUTH.md) |
| Desktop platform behavior | [`10_DESKTOP_AGENT.md`](10_DESKTOP_AGENT.md) |
| Evaluation details and release gates | [`12_TESTING_AND_EVALUATION.md`](12_TESTING_AND_EVALUATION.md) |
| Roadmap sequencing | [`14_PRODUCT_ROADMAP.md`](14_PRODUCT_ROADMAP.md) |
| Pricing assumptions and unit economics | [`16_COST_MODEL.md`](16_COST_MODEL.md) |

## Stage 01 completion criteria

- Every MVP user story has a stable requirement ID and observable acceptance
  criteria.
- Product safety principles, non-goals, quotas, and behavior at limits are
  explicit.
- Success metrics define both a threshold and a measurement method.
- Core screens trace to the requirements they serve.
- Later design decisions have a named owning document.
- `./scripts/validate-project-spec.sh` passes.
