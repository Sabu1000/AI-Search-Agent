# Connector Framework

> **Implementation status:** The shared Connector SDK scope is implemented and
> tested. The wider connector phase is partial (`7/11` master tasks). The Gmail
> phase is complete (`11/11`) for the tested all-mail backend scope, including
> normalization, reconciliation, derived-data deletion, and bounded recovery;
> Bounded read-only Drive folder traversal is implemented; Drive content formats,
> GitHub, and local-file providers remain. See
> [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Goals and ownership

The connector framework gives Gmail, Google Drive, GitHub, and the desktop
local-file agent one provider-neutral contract. A connector authenticates with
its provider, reads selected content, follows provider pagination, and emits a
deterministic stream of normalized changes. The indexing pipeline consumes
that stream without containing provider-specific branches.

This document owns provider lifecycle, normalized connector models, retry
classification, cursor rules, connector registration, and connector
certification. [`07_INDEXING_PIPELINE.md`](07_INDEXING_PIPELINE.md) owns
chunking, extraction, embeddings, and persistence of emitted changes.
[`09_AUTH_AND_OAUTH.md`](09_AUTH_AND_OAUTH.md) owns OAuth callback validation,
encrypted token persistence, and scope grants. The API and durable job records
are owned by [`05_API_SPECIFICATION.md`](05_API_SPECIFICATION.md) and
[`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md).

## Implemented SDK package

The executable SDK is the Python 3.12 package at `packages/connector-sdk`,
imported as `uas_connector_sdk`. It contains:

| Module | Responsibility |
| --- | --- |
| `models.py` | Strict immutable models, canonical providers, change variants, UTC and URL validation, stable hashes |
| `protocol.py` | The connector interface required from every provider implementation |
| `errors.py` | Sanitized permanent and retryable provider error taxonomy |
| `retry.py` | Bounded exponential backoff, full jitter, and `Retry-After` support |
| `registry.py` | Provider-to-factory registration with duplicate and identity checks |
| `contract.py` | Runtime stream validation before indexing accepts changes |
| `testing.py` | A deterministic fake connector and reusable connector-author assertions |

The production API/worker image installs this package. A dedicated Docker test
image runs Black, Ruff, strict mypy, pytest, and a minimum 90% coverage gate.
CI runs that image independently so connector contract failures cannot be
hidden by web or API success.

## Implemented Gmail synchronization adapter

The backend Gmail adapter uses only profile, message-list, and full-message GET
operations plus the OAuth token refresh endpoint. It maps authentication,
permission, rate-limit, outage, and malformed-response failures into the SDK's
sanitized taxonomy. Its deterministic MIME parser walks nested multipart trees,
prefers plain text within alternatives, combines distinct inline body sections,
and safely falls back to visible HTML. It decodes RFC 2047 headers and declared
body character sets, normalizes Unicode, line endings, whitespace, and control
characters, and applies a 5 MB extracted-body limit. Scripts, styles, hidden HTML,
forwarded-message parts, and filename/disposition/attachment-ID bodies never enter
the searchable text. Semantic HTML quote/signature containers, standard original
message separators, quote-marked reply suffixes, and the conventional signature
delimiter are removed; ambiguous text is retained. Parser decisions and skipped
attachment counts are preserved as non-secret provider metadata for auditability.

Filename or attachment-disposition MIME parts become separate `attachment`
documents with the stable identity `<message-id>:attachment:<part-id>`. The
client hydrates separately stored Gmail text bodies through the read-only
attachment GET endpoint. It fetches at most 100 external text parts per message,
caps each decoded part at 5 MB, and never fetches unsupported binary parts merely
to construct an index record. Plain text, HTML, Markdown, CSV, JSON, XML, YAML,
and other `text/*` attachments reuse the inert parser and remain linked to their
parent message/thread. Unsupported or oversized parts produce bounded searchable
descriptors with an explicit extraction status and original MIME type, ready for
later binary parsers without changing source identity.

Gmail metadata extraction decodes and bounds an explicit RFC header allowlist.
It records internal and RFC dates, labels, thread/history/message relationships,
attachment counts and details, and structured sender, recipient, Bcc, and
Reply-To identities. Provider identities are normalized into the shared
`DocumentPerson` contract and persisted in `source_people`, so person filters do
not depend on a flattened author string. An unchanged content version still
refreshes mutable source metadata and its complete people set atomically.

The worker claims a Gmail job through an authoritative database function,
decrypts credentials only in memory, refreshes and re-encrypts them when they
are near expiry, and imports at most 25 message references per job. Each page
durably queues idempotent index jobs before a separately identified continuation
job is created. The continuation page token and history ID are envelope-encrypted
in that job, and only a one-way token fingerprint is used for idempotency. The
initial profile history ID becomes the `gmail` connection cursor only when the
last page succeeds. A crash can therefore replay a page without advancing past
content or duplicating a searchable source.

After the last full-sync page, the worker schedules a one-minute incremental
poll from the committed cursor. Each incremental job requests at most 100 Gmail
history records, deduplicates message changes, refetches current content for
adds and label changes, and tombstones messages reported deleted. Tombstones
immediately disappear from search, cancel unclaimed index jobs, update usage,
and emit an outbox event. History pagination uses the same encrypted
continuation boundary as full sync, and the new cursor is committed only on the
last page. Gmail's expired-cursor `404` fails that incremental job with the
sanitized `CURSOR_INVALID` code and schedules a controlled full recovery.

Each full scan carries an encrypted, deterministic run marker. Sources seen on
every page receive that marker, and only successful completion reconciles active
Gmail sources that were absent from the authoritative listing. Message deletion
purges the parent email, its attachment sources, document versions, chunks,
embeddings, people rows, and pending index jobs while retaining a scrubbed source
tombstone. This preserves replay safety without leaving searchable derived data.
Rate limits honor a validated `Retry-After` value capped at 30 seconds; other
transient failures use capped exponential full jitter. Authentication failures
require reauthorization, malformed or permanent failures stop retrying, and
exhausted transient work is dead-lettered durably.

This implements `P5-001` through `P5-011` for the tested all-mail backend scope.
Binary PDF/Office content parsing, configurable label-selection UI, and a live
Google sandbox smoke test remain later integrations rather than unfinished Gmail
phase tasks.

## Canonical providers and source identity

The only version 1 provider identifiers are `gmail`, `google_drive`, `github`,
and `local_files`. A connector declares exactly one identifier, and its
registry key, emitted changes, and normalized documents must all match it.

`external_id` is the provider's stable identity within one connection. It must
not contain a display name, mutable path, access token, workspace ID, or random
value. Examples are a Gmail message ID, Drive file ID, GitHub
installation/repository/object tuple, or desktop device/root/file identity.
Renames update the existing item; they do not create a new item. The database
enforces connection-scoped uniqueness as specified in
[`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md).

## Connector protocol

Every implementation satisfies this asynchronous interface:

```python
class Connector(Protocol):
    provider: Provider

    async def authorize_url(self, state: str) -> str: ...
    async def exchange_code(self, code: str) -> Credentials: ...
    async def refresh_credentials(self, credentials: Credentials) -> Credentials: ...
    def full_sync(self, ctx: SyncContext) -> AsyncIterator[Change]: ...
    def incremental_sync(
        self, ctx: SyncContext, cursor: JsonObject
    ) -> AsyncIterator[Change]: ...
    async def fetch_item(self, external_id: str) -> RawItem: ...
    async def normalize(self, item: RawItem) -> NormalizedDocument: ...
    async def health_check(self) -> HealthResult: ...
```

Provider implementations may wrap vendor clients internally, but no vendor
type crosses this boundary. Methods that read content are read-only. The
framework never accepts provider write scopes or exposes a generic action/tool
method.

## Credential security boundary

`Credentials` is ephemeral exchange material. Access and refresh tokens use
Pydantic `SecretStr`, so standard JSON serialization and representations mask
them. Connector code must never place credentials in a change, cursor,
exception message, URL query log, provider metadata, metric label, or test
fixture committed to Git.

The OAuth service encrypts credentials and supplies them only while executing
an authorized connection job. The SDK intentionally does not persist tokens.
Disconnect and account deletion revoke provider grants where supported and
remove encrypted credentials according to
[`09_AUTH_AND_OAUTH.md`](09_AUTH_AND_OAUTH.md). Tests use synthetic credentials
and assert that serialization and `repr` cannot reveal them.

## Sync context and selection enforcement

`SyncContext` carries immutable `workspace_id`, `connection_id`, `sync_job_id`,
selected collection IDs, and a UTC request time. The orchestrator constructs it
only after authorization and connection-state checks. A connector must apply
the selected Gmail labels, Drive roots, GitHub repositories, or desktop roots
before fetching content—not filter an over-broad import afterward.

The SDK context intentionally contains no user-controlled SQL, local absolute
path, raw credential, or logging context. Implementations attach internal
trace data outside provider requests and may log only opaque IDs.

## Normalized document contract

An `UPSERT` contains one immutable `NormalizedDocument`:

| Field | Rule |
| --- | --- |
| `external_id` | Stable, non-empty provider identity, at most 512 characters |
| `provider` | One canonical provider and equal to the change provider |
| `source_type` | Stable lowercase connector vocabulary, at most 64 characters |
| `title` | Human-readable non-empty title; provider text remains untrusted data |
| `content` | Extractable source text, at most 5,000,000 characters per emitted item |
| `canonical_url` | Optional HTTPS URL with a host and no embedded credentials |
| `mime_type` | Valid provider-derived MIME description; never inferred from a dangerous URL |
| `authors` | Provider identities/display strings, not application authorization claims |
| `created_at`, `modified_at` | Optional timezone-aware UTC values; modification cannot precede creation |
| `access_metadata` | Public flag plus user, group, and domain identifiers used for later ACL mapping |
| `provider_metadata` | JSON-only provider details, capped at 64 KiB and never containing secrets |

`content_hash` and `permissions_hash` use SHA-256 over UTF-8 content and
canonical sorted JSON. Python's process-randomized `hash()` is forbidden for
durable identity or deduplication.

Content remains untrusted. Normalization preserves source meaning but never
interprets text as instructions, templates, commands, HTML to execute, or
authorization policy.

## Change stream contract

Connectors emit a discriminated union with these exact types:

| Type | Payload and effect |
| --- | --- |
| `UPSERT` | Complete normalized document; create or replace the source version idempotently |
| `DELETE` | Stable `external_id`; tombstone the source and derived data for that connection |
| `PERMISSION_CHANGED` | Stable `external_id` and replacement access metadata; revoke stale access before later search |
| `CURSOR_ADVANCED` | Non-empty JSON cursor capped at 16 KiB; commit only after all earlier changes succeed |

Every change has a provider and deterministic `change_id`. `make_change_id`
computes SHA-256 from canonical provider, external identity, and provider
version. Replaying the same page therefore emits the same IDs and produces no
duplicate indexed items.

Each full or incremental stream contains exactly one `CURSOR_ADVANCED`, and it
must be last. The contract validator rejects a provider mismatch, duplicate
change ID, missing cursor, or any change after a cursor. The indexing worker
applies changes and the terminal cursor in one durable transaction/outbox
boundary. It never saves a cursor before all preceding effects are durable.

## Full and incremental synchronization

Full sync enumerates all currently selected provider items and finishes with a
new durable cursor. It must reconcile deletions for items previously known to
the same connection but absent from an authoritative listing. Incremental sync
starts from the last committed cursor, handles every provider page, and emits
deletions and permission changes as first-class events.

If a provider says the cursor expired or is invalid, the connector raises
`CursorInvalidError`. The orchestrator records a sanitized recovery state and
schedules a full reconciliation; it does not silently skip changes or invent a
cursor. Crash recovery replays the uncommitted provider page. Deterministic
change IDs and database uniqueness make replay safe.

## Pagination and backpressure

Connectors pull one provider page at a time and yield changes incrementally;
they must not load an entire mailbox, drive, or repository into memory. A new
page is requested only as the worker consumes the current page. Provider page
tokens are opaque, scoped to the connection and selections, and stored only
inside the encrypted/bounded cursor envelope when persistence is required.

An individual malformed item raises `MalformedItemError` with a safe category.
The job policy may quarantine and visibly report that item, but must not log its
raw content. Systemic schema drift fails the job rather than marking a cursor
successful over unread content.

## Retry and error taxonomy

| Error | Retry | Required handling |
| --- | --- | --- |
| `AuthenticationError` | No | Mark reauthorization required; never retry a revoked/invalid token loop |
| `PermissionDeniedError` | No | Reconcile selections and show a sanitized access error |
| `RateLimitError` | Yes | Honor bounded `Retry-After`; otherwise use backoff with jitter |
| `ProviderUnavailableError` | Yes | Retry transient timeout/5xx failures within the job budget |
| `MalformedItemError` | No for the same item | Quarantine/report without raw content leakage |
| `CursorInvalidError` | No incremental retry | Schedule a controlled full reconciliation |
| `ContractViolationError` | No | Fail closed; connector code or output violated the SDK boundary |

`RetryPolicy` defaults to five attempts, a 0.5 second base, and a 30 second
maximum. Delay uses exponential full jitter. Provider `Retry-After` replaces
the jitter delay but remains bounded by the maximum. An injected sleep and
random source make the policy deterministic in tests. Job-level deadlines and
queue rescheduling are orchestrator responsibilities.

## Registry and construction

`ConnectorRegistry` maps one canonical provider to a zero-argument factory.
Registration constructs the connector once to verify its declared provider.
Duplicate keys, mismatched factories, and requests for unregistered providers
fail closed with `ContractViolationError`. Application startup—not request
input—controls registry population.

Provider dependencies are created inside factories from validated settings.
Tests and local development register `FakeConnector`; production must never
register the fake implementation.

## Provider-specific requirements

### Gmail

- Request documented Gmail read-only scopes only.
- Import messages and attachments as separate stable items linked by message
  and thread IDs.
- Preserve sender, recipients, labels, internal date, and attachment metadata;
  strip repeated quoted history/signatures only when the raw item remains
  reproducible.
- Use Gmail history IDs for incremental sync and reconcile when history expires.
- Label selection is enforced before message content is fetched.

### Google Drive

- Request documented Drive read-only scopes and honor selected roots/shared
  drives.
- Export native Docs, Sheets, and Slides to deterministic parseable formats.
- Preserve file ID, parent/folder path metadata, owners, MIME type, and modified
  time; IDs—not paths—define identity.
- Use Drive change page tokens and emit removals plus permission changes.
- Shortcuts are represented without recursively escaping selected roots.

The implemented Drive API adapter lists at most 100 children of one validated
folder per request, supports shared-drive corpora, requests an explicit field
projection, and rejects malformed, oversized, or duplicate provider pages. It
normalizes stable file IDs, names, owners, parents, modified time, MIME type,
size, safe Google links, and shortcut targets. File descriptors are inert and
shortcuts are represented without following their targets.

The Drive worker traverses a selected root (My Drive by default) through durable
folder and page jobs. It indexes non-folder descriptors, schedules discovered
folders by stable ID, and never follows shortcuts. Folder IDs, logical paths,
shared-drive IDs, and opaque page tokens are envelope-encrypted in job progress;
only a random sync-run UUID is visible for completion accounting. Each page is
atomically handed to indexing before its job completes, and the connection is
marked successful only after no discovered folder/page job remains.

### GitHub

- Authenticate as a GitHub App installation scoped to selected repositories.
- Ingest allowed README/docs/source files, issues, pull requests, review
  comments, and supported metadata using stable node/database IDs.
- Exclude binary, generated, vendored, oversized, and secret-like content
  before emitting it.
- Webhooks accelerate sync but remain hints; scheduled reconciliation repairs
  missed or reordered delivery.
- Repository removal immediately stops fetches and emits permission/deletion
  reconciliation without broadening organization access.

### Local files

- Accept signed desktop change batches only for registered devices and selected
  roots as specified by [`10_DESKTOP_AGENT.md`](10_DESKTOP_AGENT.md).
- Normalize relative logical paths; never upload or return an absolute local
  path through provider metadata, errors, or citations.
- Use device/root/file identity rather than a mutable path alone, and represent
  rename, deletion, and permission changes idempotently.
- Reject symlink traversal outside a selected root and content over the 100 MB
  MVP file limit.

## Health, logging, and metrics boundary

`health_check` returns `healthy`, `degraded`, or `unavailable`, a UTC check
time, non-negative latency, and an optional bounded safe detail. It verifies
connector configuration/provider reachability without listing user content or
printing credentials. A provider outage degrades only its connector; other
providers and already indexed authorized content remain searchable.

The SDK supplies classifications, not an observability backend. The worker will
record provider, operation, attempt, duration, safe error code, change counts,
and opaque connection/job IDs under
[`13_OBSERVABILITY_AND_OPERATIONS.md`](13_OBSERVABILITY_AND_OPERATIONS.md).
Tokens, content, URLs containing private identifiers, cursors, and raw provider
responses are forbidden in logs and metric labels.

## Connector certification test kit

Every production connector must reuse the SDK test patterns and pass:

1. OAuth exchange, refresh, revocation/expiry, and credential-redaction tests.
2. Full and incremental pagination with a terminal cursor.
3. Stable identity and deterministic replay/idempotency tests.
4. Delete and permission-change propagation.
5. Invalid/expired cursor recovery without skipped content.
6. Rate-limit and transient-outage retry behavior, including `Retry-After`.
7. Permanent auth/permission failures without retry loops.
8. Malformed, oversized, unsafe-URL, and secret-like item rejection.
9. Selection boundaries and cross-connection/workspace isolation.
10. Health results that contain no provider data or credentials.

`FakeConnector` is the executable reference: it implements auth, refresh,
fetch, normalization, health, full sync, incremental sync, upsert, deletion,
permission change, terminal cursor, rate-limit-once, malformed-item, and invalid
cursor behaviors. Current SDK tests run with 99% line coverage; CI enforces at
least 90% as the durable gate.

Run the package gate with:

```sh
./scripts/test-connector-sdk.sh
```

## Requirement coverage

| Requirement | Connector-framework responsibility |
| --- | --- |
| `DESKTOP-001` | Defines the local-files provider identity, selected-root boundary, safe path metadata, and idempotent desktop changes. |
| `GOOGLE-001` | Defines read-only Gmail/Drive connector lifecycles, selections, normalization, and recovery behavior. |
| `GITHUB-001` | Defines installation/repository scope enforcement, supported content, webhook reconciliation, and access removal. |
| `SYNC-001` | Defines health, sanitized errors, deterministic retries, cursor recovery, and replay-safe changes. |
| `CONNECTION-001` | Requires disconnect to halt fetches and drive credential plus derived-data deletion through first-class changes. |
| `SAFETY-001` | Treats all provider content as data, prohibits write/action methods, validates URLs/metadata, and prevents credential leakage. |

## Stage 06 completion criteria

- The connector protocol, ownership boundaries, canonical providers, lifecycle,
  normalized model, and all four change variants are unambiguous.
- Cursor commit ordering, deterministic idempotency, pagination, deletion,
  permission changes, retry classification, and safe failure behavior are
  testable.
- Credential, selection, untrusted-content, URL, metadata, and logging safety
  boundaries are explicit.
- Gmail, Drive, GitHub, and local-file provider requirements are defined.
- The Python SDK, registry, retry framework, runtime validator, fake connector,
  Docker test target, CI job, and reusable contract tests are implemented.
- Black, Ruff, strict mypy, all SDK tests, and at least 90% line coverage pass
  through `./scripts/test-connector-sdk.sh`.
- `./scripts/validate-connector-framework.sh` and the full `pnpm check` pass.
