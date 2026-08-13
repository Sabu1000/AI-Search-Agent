# Security and Privacy

## Goals and ownership

This document owns the product-wide security, privacy, abuse-resistance, and
data-lifecycle rules. [`09_AUTH_AND_OAUTH.md`](09_AUTH_AND_OAUTH.md) owns the
authentication and provider-authorization protocols. The API, database,
connector, indexing, deployment, and operations stages must implement these
rules rather than redefining them.

The product processes private email, files, and source code. Security is a
launch requirement. Version 1 is read-only: no retrieved content, model output,
connector, webhook, or desktop file can authorize a write to a source system.

## Security boundaries and threat model

Trust boundaries exist between the browser, desktop agent, public API,
workers, PostgreSQL, Redis, object storage, model providers, source providers,
and operators. All data crossing a boundary is authenticated where applicable,
bounded, validated, and treated as untrusted.

The MVP explicitly defends against:

- account takeover, credential stuffing, session theft, fixation, and replay;
- CSRF, CORS mistakes, XSS, clickjacking, open redirects, and cache leakage;
- OAuth state/code interception, token disclosure, and excessive scopes;
- broken object authorization and cross-workspace database access;
- forged webhooks, desktop request replay, and malicious uploaded files;
- SQL/command/template injection, SSRF, path traversal, decompression bombs,
  and unsafe parser behavior;
- prompt injection that attempts tool use, data exfiltration, or false links;
- secrets or private content leaking through logs, traces, errors, analytics,
  idempotency records, queues, or model requests; and
- deletion races, backup restoration of deleted access, and compromised
  dependencies or images.

The system does not claim to protect a fully compromised user device or a
provider account after the provider itself grants an attacker access. Those
events must still support session/connection revocation and auditable recovery.

## Data classification and minimization

| Class | Examples | Required handling |
| --- | --- | --- |
| Secret | Passwords, session and reset tokens, OAuth credentials, KMS data keys, device credentials | Never logged or returned after initial issuance; encrypted or one-way hashed; shortest practical retention |
| Private content | Email/file bodies, source code, chunks, prompts, answers, excerpts, attachment text | Workspace-scoped, encrypted in transit and at rest, excluded from routine telemetry |
| Sensitive metadata | Email address, filenames, repository names, source URLs, provider subject IDs, IP/user agent | Minimized, access-controlled, redacted or hashed in aggregate telemetry |
| Operational metadata | Opaque IDs, state enums, byte counts, durations, error classes | Permitted in structured logs when it cannot reconstruct private content |
| Public | Published product documentation and static assets | Integrity controls still apply |

Only fields needed for an owned requirement may be collected. Queue and outbox
payloads carry identifiers, not credentials or full content. Model providers
receive only the bounded context needed for the current answer. Private content
is not used for model training under the production provider agreement.

## Tenant authorization

- A workspace is the tenant and authorization boundary.
- The API authenticates the principal before resolving `X-Workspace-ID`.
- Membership, role, account state, workspace state, and authorization version
  are checked before any tenant resource identifier is loaded.
- Repository methods include a workspace predicate even when PostgreSQL RLS
  also applies. The API sets transaction-local `app.user_id` and
  `app.workspace_id`; pooled connections never retain session-scoped context.
- Workers derive workspace authority from a claimed durable job and then set
  the same transaction-local context. Caller-controlled job payloads cannot
  select another workspace.
- Search applies workspace, source state, and permission predicates in SQL
  before lexical or vector candidate selection.
- Inaccessible resource identifiers receive non-enumerating responses.
- Authorization and deletion state are rechecked before returning cached,
  paginated, streamed, or idempotently replayed private data.

Roles are least-privilege. Migration credentials are unavailable to the API and
workers. Application roles cannot bypass RLS, create roles, alter policies, or
read raw credential envelopes through general repositories.

## Cryptography and secret management

- TLS 1.2 or newer is required on every external hop; production cookies and
  credentials are never accepted over plaintext HTTP.
- Managed storage encryption covers database, object storage, volumes, logs,
  and backups. Provider credentials also use application-layer envelope encryption
  as defined in `09_AUTH_AND_OAUTH.md`.
- Passwords use Argon2id with versioned parameters and opportunistic rehashing.
- Random tokens use a cryptographically secure generator and at least 128 bits
  of entropy. Stored bearer, refresh, verification, reset, reauthentication,
  deletion-receipt, and device credentials are keyed hashes, never plaintext.
- Signing, hashing, and encryption keys have separate purposes. Production
  startup rejects missing, short, shared, or known development secrets.
- Secrets come from a managed secrets service or workload identity, never the
  image, source tree, Compose file, browser bundle, logs, or CI output.
- Rotation supports overlapping verification keys for a bounded window while
  new issuance uses only the current key. Emergency rotation can revoke all
  affected sessions or credential records.

Cryptographic algorithms and parameters are centralized, versioned in stored
records where needed, and tested. Product code must not invent ad hoc
encryption or compare secret values with ordinary string equality.

## Browser, API, and content security

Browser session cookies, CSRF proof, bearer behavior, device signatures, and
deletion receipts follow `05_API_SPECIFICATION.md` and
`09_AUTH_AND_OAUTH.md`. In addition:

- CORS is an exact configuration allowlist with credentials permitted only for
  owned web origins; wildcard origins and reflected caller origins are banned.
- Unsafe cookie-authenticated requests require an allowlisted `Origin` plus
  double-submit CSRF proof. Requests carrying both cookie and bearer identity
  are rejected.
- HTML responses use a nonce-based Content Security Policy, deny framing,
  disable MIME sniffing, set a strict referrer policy, and constrain browser
  permissions. HSTS is enabled after HTTPS readiness is verified.
- Private API responses use `Cache-Control: private, no-store` and the relevant
  `Vary` headers. Service workers and CDNs do not cache authenticated content.
- User/provider Markdown is rendered with an allowlist sanitizer. Raw HTML,
  scripts, event handlers, dangerous URL schemes, and automatic remote content
  are removed. External links are visibly marked and opened safely.
- Redirect and callback targets are server-owned identifiers mapped to exact
  application routes; caller-supplied absolute URLs are never followed.

## Input, file, network, and provider safety

All boundary schemas reject unknown fields, impose byte/item/depth limits, and
validate types before business logic. Database access uses parameterized
queries. No input is passed to a shell or evaluated as code.

Uploads and provider objects are checked by declared and detected type, size,
checksum, archive expansion ratio, nesting, and parser timeout. Extraction runs
without network access in a resource-limited process/container. Absolute paths,
parent traversal, symlink escape, macros, executables, and active document
content are rejected or kept inert. Failed artifacts are quarantined by opaque
object key and are not indexed.

Outbound requests use HTTPS, fixed provider/model allowlists, DNS/IP checks,
bounded redirects, timeouts, response-size limits, and no caller-controlled
proxy. Retrieved documents, model output, source URLs, and webhook bodies can
never trigger an outbound fetch. Provider webhooks are authenticated before
parsing costly payloads and remain hints reconciled against provider APIs.

## Prompt-injection and model boundary

Retrieved text is delimited and labeled untrusted. System instructions state
that it is evidence only, cannot change policy, and cannot request tools,
network calls, secrets, or actions. Version 1 exposes no action tools to the
model.

Context includes opaque citation IDs rather than authoritative links. Structured
model output is schema-validated; every cited ID must exist in the supplied
authorized context. Unsupported claims are removed or produce the specified
insufficient-evidence response. Model-created URLs are never rendered as source
citations. Prompt-injection fixtures are part of search/answer evaluation.

## Logging, auditing, and incident evidence

Structured application logs may contain request ID, opaque principal/workspace
IDs, route template, status, duration, safe error code, job ID, provider type,
and aggregate counts. They must never contain:

- passwords, password hashes, tokens, cookies, authorization headers, OAuth
  codes/state/verifiers, encryption material, signed URLs, or device secrets;
- full request/response bodies, document/email bodies, source code, chunks,
  private prompts, answers, excerpts, absolute local paths, or webhook bodies;
- raw email addresses, provider subject IDs, repository/folder names, or query
  text unless a separately approved operational requirement defines redaction.

Errors are allowlisted and sanitized before logging, persistence, or client
return. Tracing records operation names and timings, not content. Production
debug logging is disabled.

Security audit events record actor, workspace when retained, action, target
type/opaque ID, outcome, request ID, timestamp, and safe reason code. Login,
session replay/revocation, membership/role changes, OAuth connect/disconnect,
credential rotation failure, export, deletion, device registration/revocation,
and operator access are audited. Audit events are append-oriented, access is
restricted, clocks are UTC-synchronized, and alerts detect deletion or
high-risk-event gaps.

## Abuse prevention and operational controls

Rate limits use normalized IP and opaque account/device/provider buckets. Login,
registration, verification, reset, OAuth start/callback, uploads, and webhooks
have tighter limits than normal authenticated API traffic. Responses do not
reveal whether an email, OAuth identity, workspace, repository, or source
exists. Progressive delays and alerts apply to repeated authentication failure;
an attacker cannot create unbounded durable rows or expensive jobs.

Production dependencies are locked, reviewed, and scanned. CI runs secret,
dependency, static-analysis, and container-image checks before release;
actionable critical findings block deployment under the vulnerability policy.
Images run as non-root with a read-only filesystem and minimal capabilities
where supported. Software bills of materials and image digests are retained.

Operator access requires individual identities, MFA, least privilege,
time-bounded elevation, and audit logging. Production data is not copied to
development. Support tooling shows metadata by default and requires explicit,
audited elevation for private content.

## Privacy choices and consent

- Local-folder indexing requires a preview and explicit root selection.
- Users choose folders, repositories, and provider collections and can define
  documented exclusion patterns before indexing.
- Consent screens state the exact provider scopes and data categories used.
- Connections can be disabled or deleted without navigating away from the
  account. Disconnect immediately blocks fetches and search access to derived
  content, then shows tracked deletion progress.
- Users can request a portable account export and complete account deletion.
- Product analytics are opt-in where required, collect no private content, and
  respect deletion and retention rules.

## Retention, export, deletion, and backups

The authoritative retention schedule must be configured before production and
list each data class, purpose, duration, deletion trigger, backup behavior, and
owner. A nullable expiry never silently means permanent retention.

On connector disconnect, provider access is revoked when supported, encrypted
credentials and scopes are cleared in the cutoff transaction, sync is disabled,
and a durable job deletes derived content. On account deletion, normal sessions
and access stop immediately and tracked deletion completes within the 24-hour
product target. Active database rows and object-storage content are reconciled;
only content-free tombstones/audit evidence required for safety may remain.

Exports require recent reauthentication, are generated asynchronously into an
encrypted private object, use a single-use short-lived download capability, and
expire automatically. Export archives never include password/session/provider
secrets or data outside the authorized account/workspace scope.

Backups are encrypted, access-controlled, immutable for the required recovery
window, and expire on a documented schedule. Restore tests prove RLS, deletion
tombstones, and revoked credentials remain effective. A restore never
reactivates a deleted account or connection; post-restore reconciliation
reapplies tombstones before serving traffic.

## Security verification and release gates

The automated security suite must cover:

1. Cross-user and cross-workspace read/write/search isolation, including guessed
   IDs, cursors, caches, streams, workers, and idempotent replay.
2. Session fixation, refresh rotation, concurrent refresh, replay-family
   revocation, logout, password reset, and authorization-version changes.
3. CSRF, CORS, cookie flags, mixed authentication, CSP, redirect, and cache
   behavior.
4. OAuth state/PKCE expiry, single use, mix-up defense, scope validation,
   credential encryption, rotation, disconnect, and redaction.
5. Rate-limit/account-enumeration behavior and bounded durable/expensive work.
6. Webhook signatures/replay and desktop signatures/nonces/body digests.
7. Upload type/size/archive/path/parser isolation and outbound SSRF controls.
8. Prompt-injection, invalid citation, unsafe link, and structured-output
   rejection fixtures.
9. Log, trace, problem response, queue, outbox, and audit-event redaction.
10. Export/deletion races, object cleanup, backup expiry, and restore safety.
11. Secret, dependency, static-analysis, and container scans in CI.

Security-sensitive code requires focused tests and review. Production release
also requires threat-model review, external penetration testing for the public
surface, an incident-response runbook, key-rotation drill, and backup-restore
exercise. Findings have named owners and cannot be waived silently.

## Requirement coverage

| Requirement | Security contribution |
| --- | --- |
| `AUTH-001` | Defines password, session, browser, abuse, audit, and tenant controls. |
| `DESKTOP-001` | Requires explicit folder consent, keychain-backed credentials, signed/replay-safe requests, and parser isolation. |
| `GOOGLE-001` | Requires least-privilege consent, envelope-encrypted credentials, bounded provider access, and disconnect deletion. |
| `GITHUB-001` | Requires selected-repository access, verified webhooks, short-lived installation tokens, and access reconciliation. |
| `SYNC-001` | Constrains job authority, payloads, retries, errors, and provider isolation. |
| `SEARCH-001` | Requires tenant/permission filtering before retrieval and private cache boundaries. |
| `ANSWER-001` | Defines the untrusted-content and validated-model-output boundary. |
| `CITATION-001` | Requires authorized supplied-context IDs and safe source links. |
| `CONNECTION-001` | Defines immediate credential/access cutoff and tracked derived-data deletion. |
| `ACCOUNT-001` | Defines reauthenticated export, immediate revocation, complete deletion, and restore safety. |
| `SAFETY-001` | Establishes read-only behavior, injection defenses, minimization, and release verification. |

## Stage 08 completion criteria

This specification is complete when the threat model, data classes, tenant and
cryptographic boundaries, browser/input/model defenses, audit policy, privacy
choices, retention/deletion rules, and executable security tests are explicit
and consistent with stages 02, 04, 05, 06, and 09. Specification completion is
not implementation: Phase 14 tasks remain open until production controls and
their tests exist.
