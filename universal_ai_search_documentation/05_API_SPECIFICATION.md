# API Specification

> **Implementation status:** Specification and shared API platform primitives
> are complete; `7/49` catalogued `/v1` product endpoints are implemented.
> Existing liveness/readiness routes are foundation endpoints outside this
> catalog. See
> [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Goals and ownership

The API is the only public cloud boundary for web and desktop clients. It
authenticates principals, validates workspace membership, enforces resource
authorization, starts durable work, exposes status, orchestrates search and
grounded answers, and returns safe source links. Clients never connect directly
to PostgreSQL, Redis, provider APIs, or object storage except through a
short-lived upload URL issued for one approved object.

This stage owns public HTTP and Server-Sent Events contracts. Search semantics
belong to [`03_SEARCH_ENGINE_DESIGN.md`](03_SEARCH_ENGINE_DESIGN.md), persistence
and transaction behavior to [`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md),
connector internals to [`06_CONNECTOR_FRAMEWORK.md`](06_CONNECTOR_FRAMEWORK.md),
authentication and provider consent details to
[`09_AUTH_AND_OAUTH.md`](09_AUTH_AND_OAUTH.md), and desktop filesystem behavior
to [`10_DESKTOP_AGENT.md`](10_DESKTOP_AGENT.md).

The contracts and dependency-safe shared platform are implemented now. The
authentication routes and hybrid search route are integrated; remaining product
routes and generated clients are added in their feature phases.

## Protocol and base conventions

- Production uses HTTPS only. The local Compose environment may use HTTP on
  loopback addresses.
- The public application base path is `/v1`. Process health remains outside the
  product API at `/health/live` and `/health/ready`.
- JSON uses UTF-8 and `Content-Type: application/json`; errors use
  `application/problem+json`; streams use `text/event-stream`.
- Resource IDs and request IDs are lowercase canonical UUID strings.
- Timestamps are RFC 3339 UTC values with a `Z` suffix. Durations and latency
  fields use integer milliseconds.
- Byte, token, item, and quota values are integers. Ranking scores are finite
  JSON numbers and are not probabilities.
- Request objects reject unknown fields. Optional fields are omitted when not
  supplied; `null` is accepted only where the schema explicitly allows it.
- Response consumers must ignore unknown additive fields while continuing to
  validate known fields.
- Collection order is deterministic and documented per endpoint.
- No `GET` or `HEAD` route changes product state. OAuth callbacks are the only
  browser-navigation exception and are protected by expiring single-use state.

## Versioning and generated contracts

The major version appears in the path. Backward-compatible changes may add an
optional request field, response field, endpoint, error code, or SSE event.
Removing or renaming fields, changing meaning, tightening accepted input, or
changing authentication requires a new major version unless a published
deprecation window preserves compatibility.

OpenAPI 3.1 is the source of truth when implementation begins. Pydantic boundary
models generate the document; CI validates it, checks examples against schemas,
and regenerates strict TypeScript contracts in `packages/shared-types` without a
diff. Handlers may not return untyped dictionaries as public responses.

Deprecated operations return `Deprecation: true`, an RFC 3339 `Sunset` date,
and a `Link` header to migration guidance for at least one supported client
release cycle. API and desktop clients send `UAS-Client-Version`; unsupported
clients receive `426 Upgrade Required` with a safe upgrade URL from server
configuration, never from retrieved content.

## Authentication modes

### Browser session

The web application uses host-only cookies:

- `__Host-uas_access`: signed access token, `Secure`, `HttpOnly`, `Path=/`,
  `SameSite=Lax`, maximum lifetime 15 minutes;
- `__Host-uas_refresh`: opaque rotating refresh token, `Secure`, `HttpOnly`,
  `Path=/`, `SameSite=Strict`, maximum lifetime 30 days; and
- `__Host-uas_csrf`: random CSRF value readable by the same-origin web client,
  `Secure`, `Path=/`, `SameSite=Strict`.

Cookie-authenticated unsafe methods require `X-CSRF-Token` matching the CSRF
cookie and an allowlisted `Origin`. Missing or mismatched proof returns 403.
Login rotates CSRF state; refresh rotates the refresh token; logout revokes the
server session and expires all three cookies.

### Bearer session

Non-browser trusted clients use `Authorization: Bearer <access-token>`. They
store refresh tokens only in the operating-system keychain and send them to
`POST /v1/auth/refresh`; access tokens are kept in memory when practical.
Successful native login or refresh returns the new opaque refresh token exactly
once over TLS so it can replace the prior keychain value; it is never available
from a later read or log.
Bearer-authenticated requests do not use CSRF cookies, and a request containing
both bearer and browser credentials is rejected as ambiguous.

### Device signature

After registration, device synchronization endpoints require
`Authorization: Device <opaque-credential>` and these headers in addition to
the device ID in the path:

- `X-Device-Timestamp`: RFC 3339 UTC within the configured five-minute window;
- `X-Device-Nonce`: 128 bits or more of base64url randomness;
- `Content-Digest`: digest of the exact request body;
- `X-Device-Signature`: signature over method, canonical path, timestamp,
  nonce, content digest, and request ID.

The server loads the registered public key, verifies status and signature, and
atomically rejects nonce replay. Device credentials cannot call browser,
connection, search-history, export, or account endpoints.

### Deletion receipt

Account deletion revokes normal sessions immediately. Its one remaining public
capability is an opaque `Authorization: DeletionReceipt <token>` accepted only
by `GET /v1/account/deletion-status`. The server stores only the receipt hash;
the token expires after the deletion and support window. It cannot authenticate
any other operation.

## Workspace and authorization context

Workspace-scoped routes require `X-Workspace-ID`. The value selects among the
authenticated user's active memberships; it is never trusted as authorization.
The API resolves the session first, validates membership and workspace state,
then sets the trusted transaction-local database context.

`GET /v1/auth/me` returns the user's memberships and preferred workspace ID so
clients do not guess identifiers. Missing context returns
`WORKSPACE_REQUIRED`. Invalid, suspended, deleting, or inaccessible context
returns a non-enumerating 404 or the user's own actionable workspace-state
error. A resource ID is loaded only after workspace context is established.

An access token may cache a membership/version claim for performance, but every
sensitive request checks the authoritative authorization version. A stale claim
requires refresh or returns `AUTHORIZATION_CHANGED`; it never retains revoked
access.

## Standard request and response headers

Clients may send `X-Request-ID` as a UUID. The server accepts a valid value or
replaces it, returns `X-Request-ID` on every response, and propagates it to jobs,
provider calls, model calls, and audit events. Request IDs are correlation aids,
not idempotency keys.

The following headers have defined behavior:

| Header | Direction | Rule |
| --- | --- | --- |
| `X-Workspace-ID` | Request | Required on authenticated workspace routes |
| `Idempotency-Key` | Request | Required on cataloged durable writes; 8–128 URL-safe ASCII characters |
| `If-Match` | Request | Required when a mutable-resource route catalogs optimistic concurrency |
| `ETag` | Response | Opaque resource version for supported reads |
| `X-Reauth-Token` | Request | Five-minute, single-purpose proof for sensitive actions |
| `X-Confirm-Action` | Request | Exact action phrase required by cataloged destructive routes |
| `X-Request-ID` | Both | UUID correlation identifier |
| `UAS-Client-Version` | Request | Semantic client version and platform identifier |
| `Retry-After` | Response | Whole seconds or HTTP date for retryable throttling/unavailability |
| `RateLimit-Limit` | Response | Current bucket limit and window |
| `RateLimit-Remaining` | Response | Requests remaining in the current bucket |
| `RateLimit-Reset` | Response | Seconds until bucket reset |

Responses carrying private user data set `Cache-Control: private, no-store` and
`Vary: Origin, Authorization, Cookie, X-Workspace-ID` as applicable.

## Validation and serialization

Boundary schemas validate before application services run:

- strings have explicit minimum and maximum lengths;
- arrays have item and count limits and reject duplicates where order is not
  meaningful;
- UUIDs, timestamps, MIME types, email addresses, URLs, file extensions, and
  provider values use strict formats and allowlists;
- all URLs are HTTPS allowlisted provider URLs or application-owned routes;
- filters use typed fields and never accept SQL, regular expressions, tsquery,
  vector literals, object keys, absolute local paths, or arbitrary provider
  query syntax;
- upload metadata must agree with allowed MIME, extension, size, and checksum;
  server-side validation repeats after upload; and
- Markdown is inert content until sanitized by the presentation layer. The API
  never returns model-created authoritative links.

Validation failures report JSON Pointer locations without echoing secrets or
private bodies.

## Idempotency and optimistic concurrency

Routes marked `required` in the endpoint catalog atomically reserve an
`Idempotency-Key` for the authenticated principal, workspace, method, canonical
route, and request-body hash. For 24 hours:

- the same key and same request returns the original status, selected safe
  headers, and response body;
- the same key with a different method, route, workspace, or body returns
  `IDEMPOTENCY_KEY_REUSED` with 409;
- a concurrent duplicate while the first request is running returns
  `IDEMPOTENCY_IN_PROGRESS` with 409 and `Retry-After`; and
- a server crash may safely resume or return the durable operation/resource
  already attached to the key.

Every replay rechecks the current session, workspace, authorization version,
resource state, and deletion state. Idempotency never revives revoked access or
returns content that has since become inaccessible. Account deletion binds its
key directly to the durable deletion request because it spans all of a user's
workspaces.

Stored replay bodies cannot contain access tokens, refresh tokens, OAuth state,
signed upload URLs, deletion receipt tokens, source content, excerpts, or
provider credentials. Those routes use natural unique constraints, client
batch IDs, or regenerate short-lived capabilities after reauthorization.
Completed SSE retries are reconstructed as a finite authorized event sequence
from the persisted message; raw stream bytes are never cached as the replay
record.

Mutable resources returned with `ETag` require `If-Match` for selection changes
and other cataloged updates. A stale value returns 412
`RESOURCE_VERSION_MISMATCH`. Deletes still reauthorize and confirm the action;
an ETag never substitutes for permission.

## Cursor pagination, filtering, and sorting

All growing collections use keyset cursors. The common response is:

```json
{
  "items": [],
  "page": {
    "next_cursor": "opaque-base64url-value",
    "has_more": false
  }
}
```

`limit` defaults to 25 and accepts 1–100 unless an endpoint documents a lower
cap. `cursor` is an opaque, authenticated value containing the endpoint,
workspace, principal, normalized filter/sort hash, stable sort key, and 24-hour
expiry. Clients must not parse it. Changing filters, sort, workspace, or user
while reusing a cursor returns `CURSOR_CONTEXT_MISMATCH`; malformed or expired
cursors return `CURSOR_INVALID`.

Collections use a unique final tie-breaker, normally `id`. New items may appear
before a later page but existing items are not duplicated within one cursor
walk. Total counts are omitted unless inexpensive and explicitly documented.

List filters combine different fields with `AND` and repeated values within a
field with `OR`. Unknown filters or sorts are rejected. Default sorts are:

- connections and devices: `created_at DESC, id DESC`;
- sources: `source_timestamp DESC, id DESC`;
- search history, conversations, and operations: `created_at DESC, id DESC`;
- conversation messages: `created_at ASC, id ASC`.

## Rate limits and quotas

Rate limits are enforced after safe principal/IP derivation and before costly
work. The MVP authenticated free-user bucket is 30 accepted application
requests per rolling minute. Separate lower abuse buckets apply to login,
registration, reset, verification, OAuth start/callback, signed upload, and
webhook ingress. Device manifest and heartbeat buckets are per device; provider
webhooks are per verified installation.

A rejected request returns 429 `RATE_LIMITED` with `Retry-After` and rate-limit
headers and is not recorded as successful usage. Internal provider throttling
does not consume extra user requests when workers retry.

Workspace usage is available at `GET /v1/usage`. Attempts beyond 25,000 indexed
sources or 10 GB extracted content return 409 `QUOTA_EXCEEDED` before promotion,
including current and limit values safe for that workspace. Files over 100 MB
return 413 `FILE_TOO_LARGE` before upload or extraction. Limits are enforced
server-side even if a client omits size metadata.

## HTTP status and error contract

Success status usage is consistent:

- 200 for successful reads, updates, and commands returning a representation;
- 201 for synchronously created resources;
- 202 for accepted durable operations that continue asynchronously;
- 204 for successful commands with no response body; and
- 303 only for validated OAuth callback navigation to an allowlisted UI route.

Every non-SSE error uses this shape:

```json
{
  "type": "urn:uas:problem:connection-expired",
  "title": "Connection needs attention",
  "status": 409,
  "code": "CONNECTION_EXPIRED",
  "detail": "Reconnect this source to continue syncing.",
  "request_id": "018f4f3d-22f8-7c51-9f31-c22e1a7d9461",
  "retryable": false,
  "errors": [
    {"pointer": "/filters/date_from", "code": "INVALID_TIMESTAMP"}
  ]
}
```

`type`, `title`, `status`, `code`, `request_id`, and `retryable` are required;
`detail` and `errors` are optional. Titles and details are user-safe and never
reveal account existence, inaccessible resource existence, provider tokens,
private content, SQL, stack traces, object keys, or internal network names.

| Status | Use |
| ---: | --- |
| 400 | Malformed JSON, invalid cursor/header, or cross-field validation |
| 401 | Missing, expired, revoked, or invalid authentication |
| 403 | Authenticated but prohibited action, CSRF failure, or unverified email gate |
| 404 | Resource absent or concealed because it is outside authorized scope |
| 409 | State, idempotency, quota, connector, or deletion conflict |
| 412 | Stale `If-Match` precondition |
| 413 | Request or file exceeds documented byte limit |
| 422 | Well-formed request with unsupported semantic value |
| 426 | Client version is no longer supported |
| 429 | API rate limit exceeded |
| 500 | Unexpected server failure with opaque request ID |
| 502 | Upstream provider returned an unusable response |
| 503 | Required dependency unavailable or retryable degraded state |
| 504 | Upstream or model deadline exceeded |

Retryable responses include `Retry-After` when the server can estimate a safe
delay. Clients retry only idempotent reads or writes protected by a stable
idempotency key/client batch ID, use bounded exponential backoff with jitter,
and stop on permanent 4xx errors.

## Endpoint catalog

`Session` means browser cookie or bearer authentication. `Device` means the
registered signature contract. `Receipt` means deletion-receipt authentication.
The idempotency column states whether the header is required.

### Authentication endpoints

| Method | Path | Auth | Idempotency | Success |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/auth/register` | Public | no | 202 generic response |
| `POST` | `/v1/auth/email/verify` | Public token | no | 204 |
| `POST` | `/v1/auth/email/resend` | Public | no | 202 generic response |
| `POST` | `/v1/auth/login` | Public | no | 200 session |
| `POST` | `/v1/auth/refresh` | Refresh credential | no | 200 rotated session |
| `POST` | `/v1/auth/logout` | Session | no | 204 |
| `GET` | `/v1/auth/me` | Session | no | 200 principal and memberships |
| `POST` | `/v1/auth/reauthenticate` | Session | no | 200 short-lived reauth token |
| `POST` | `/v1/auth/password/request-reset` | Public | no | 202 generic response |
| `POST` | `/v1/auth/password/reset` | Public token | no | 204 and session-family revocation |

### Connection endpoints

| Method | Path | Auth | Idempotency | Success |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/connections` | Session + workspace | no | 200 page |
| `GET` | `/v1/connections/{connection_id}` | Session + workspace | no | 200 connection + ETag |
| `GET` | `/v1/connections/{connection_id}/status` | Session + workspace | no | 200 sync/deletion status |
| `POST` | `/v1/connections/google/authorize` | Session + verified email + workspace | no | 200 short-lived authorization URL |
| `GET` | `/v1/connections/google/callback` | Single-use OAuth state | no | 303 allowlisted UI route |
| `POST` | `/v1/connections/github/authorize` | Session + verified email + workspace | no | 200 installation URL |
| `GET` | `/v1/connections/github/callback` | Single-use install state | no | 303 allowlisted UI route |
| `PUT` | `/v1/connections/{connection_id}/selections` | Session + workspace + If-Match | required | 200 connection + ETag |
| `POST` | `/v1/connections/{connection_id}/sync` | Session + workspace | required | 202 operation |
| `DELETE` | `/v1/connections/{connection_id}` | Session + workspace + reauth + confirmation | required | 202 deletion operation |

### Search, conversation, and source endpoints

| Method | Path | Auth | Idempotency | Success |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/search` | Session + workspace | optional for `results`, required for `answer` | 200 search response |
| `POST` | `/v1/search/suggestions` | Session + workspace | no | 200 bounded suggestions |
| `GET` | `/v1/search/history` | Session + workspace | no | 200 page |
| `DELETE` | `/v1/search/history` | Session + workspace + confirmation | required | 204 |
| `POST` | `/v1/conversations` | Session + workspace | required | 201 conversation |
| `GET` | `/v1/conversations` | Session + workspace | no | 200 page |
| `GET` | `/v1/conversations/{conversation_id}` | Session + workspace | no | 200 conversation and message page |
| `DELETE` | `/v1/conversations/{conversation_id}` | Session + workspace + confirmation | required | 202 operation |
| `POST` | `/v1/conversations/{conversation_id}/messages:stream` | Session + workspace | required | 200 SSE stream |
| `GET` | `/v1/sources` | Session + workspace | no | 200 page |
| `GET` | `/v1/sources/{source_id}` | Session + workspace | no | 200 source |
| `DELETE` | `/v1/sources/{source_id}` | Session + workspace + confirmation | required | 202 operation |
| `POST` | `/v1/sources/{source_id}/reindex` | Session + workspace | required | 202 operation |

### Operation, usage, and privacy endpoints

| Method | Path | Auth | Idempotency | Success |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/operations/{operation_id}` | Session + workspace | no | 200 operation |
| `POST` | `/v1/operations/{operation_id}:retry` | Session + workspace | required | 202 replacement operation |
| `GET` | `/v1/usage` | Session + workspace | no | 200 usage and limits |
| `POST` | `/v1/account/export` | Session + workspace + reauth | required | 202 operation |
| `DELETE` | `/v1/account` | Session + reauth + exact confirmation | required | 202 receipt response and immediate logout |
| `GET` | `/v1/account/deletion-status` | Receipt | no | 200 deletion status only |

### Desktop endpoints

| Method | Path | Auth | Idempotency | Success |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/devices/registration-challenges` | Session + workspace | no | 201 short-lived challenge |
| `POST` | `/v1/devices/register` | Session + workspace + challenge signature | challenge ID | 201 device credential once |
| `GET` | `/v1/devices` | Session + workspace | no | 200 page |
| `GET` | `/v1/devices/{device_id}` | Session + workspace | no | 200 device |
| `POST` | `/v1/devices/{device_id}/heartbeat` | Device | no | 204 |
| `POST` | `/v1/devices/{device_id}/manifests` | Device | client `manifest_id` | 200 requested changes |
| `POST` | `/v1/devices/{device_id}/uploads:sign` | Device | client `upload_id` | 200 short-lived signed upload |
| `POST` | `/v1/devices/{device_id}/changes` | Device | client `change_batch_id` | 202 operation |
| `DELETE` | `/v1/devices/{device_id}` | Session + workspace + confirmation | required | 202 deletion operation |

### Webhook endpoints

| Method | Path | Auth | Idempotency | Success |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/webhooks/github` | Verified GitHub signature and delivery ID | provider delivery ID | 202 or 204 duplicate |

## Authentication contracts

Registration accepts email, password, full name, terms version, and locale.
Passwords are accepted only over TLS, never returned or logged, and are checked
against the password policy owned by the authentication stage. Both a new email
and an already registered email receive the same 202 shape:

```json
{
  "status": "verification_if_eligible",
  "message": "If this address can be registered, verification instructions will follow."
}
```

Login accepts `email` and `password`. Browser mode is selected by same-origin
cookie transport; native mode is an explicit allowlisted client type. The
response includes the user, active memberships, access expiry, and CSRF value
when applicable, but never returns password hashes or browser refresh tokens.
Invalid email, password, unlinked identity, or ineligible state returns the same
`INVALID_CREDENTIALS` response until authentication succeeds.

`GET /v1/auth/me` returns:

```json
{
  "user": {
    "id": "018f4f3d-22f8-7c51-9f31-c22e1a7d9461",
    "email": "owner@example.com",
    "full_name": "Example Owner",
    "email_verified": true
  },
  "memberships": [
    {
      "workspace_id": "018f4f44-e6df-7c21-8a91-8010f3c242bc",
      "name": "Example Owner",
      "role": "owner",
      "status": "active"
    }
  ],
  "preferred_workspace_id": "018f4f44-e6df-7c21-8a91-8010f3c242bc",
  "access_expires_at": "2026-08-03T21:15:00Z"
}
```

Reauthentication verifies the current password or supported identity challenge
and returns a single-purpose opaque token valid for at most five minutes. Its
purpose is bound to `disconnect`, `export`, or `delete_account`; it cannot be
used as an access token.

Password-reset request and email resend always return the generic 202 response.
Verification and reset tokens are single-use, purpose-bound, and accepted in
the JSON body rather than a URL query that may enter logs. Successful password
reset revokes every refresh-token family.

## Connection contracts

A connection representation contains ID, provider, safe display label, status,
granted read-only scope names, selected collection summaries, last successful
sync, current operation, sanitized error code, created/updated timestamps, and
available recovery action. It never contains tokens, provider account IDs,
provider payloads, cursors, internal retries, or object keys.

Authorization-start requests specify only the documented source families and
an allowlisted relative return path:

```json
{
  "source_families": ["gmail", "google_drive"],
  "return_path": "/settings/connections"
}
```

The server returns an HTTPS URL for the configured provider host and an expiry.
It creates signed, single-use state and PKCE material server-side. Callback
success creates or updates the connection, queues initial reconciliation, and
303-redirects with only `connection_id` and a safe result code. Provider tokens
and raw provider errors never appear in redirect parameters.

Selection updates contain immutable collection IDs returned by the API. The
server proves every selection belongs to that connection and requested
workspace, rejects provider write scopes, updates authorization version, and
queues reconciliation atomically.

Sync and disconnect return the common operation representation. Disconnect
requires `X-Reauth-Token`, `X-Confirm-Action: disconnect`, and an idempotency
key. At acceptance, credentials are removed and search/sync access stops even
though physical deletion continues.

## Asynchronous operation contract

Long-running sync, reindex, deletion, export, and retry requests return 202:

```json
{
  "operation": {
    "id": "018f4f61-9910-7244-a4c7-232bd3b4c553",
    "type": "connection_delete",
    "status": "pending",
    "progress": {"completed": 0, "total": null, "unit": "items"},
    "created_at": "2026-08-03T21:00:00Z",
    "started_at": null,
    "completed_at": null,
    "deadline_at": "2026-08-04T21:00:00Z",
    "error": null,
    "links": {"self": "/v1/operations/018f4f61-9910-7244-a4c7-232bd3b4c553"}
  }
}
```

Statuses are `pending`, `running`, `retry_wait`, `completed`, `failed`,
`blocked`, or `cancelled`. Progress is monotonic when a stable total exists;
otherwise `total` remains null rather than guessing. Errors contain a sanitized
code, safe detail, retryable flag, and recovery action. Internal worker IDs,
stack traces, provider bodies, paths, and credentials are never exposed.

`POST /operations/{id}:retry` is allowed only for a terminal retryable operation
and returns a new operation linked to the prior one. It never mutates a
completed operation or creates duplicate visible content.

## Search contracts

The search request aligns with the stage-03 planner but exposes only user-owned
inputs:

```json
{
  "query": "What did Maya decide about payment retries?",
  "mode": "answer",
  "providers": ["gmail", "github"],
  "filters": {
    "people": ["Maya"],
    "date_from": "2026-01-01T00:00:00Z",
    "date_to_exclusive": null,
    "repository_ids": [],
    "folder_ids": [],
    "source_types": [],
    "file_types": []
  },
  "limit": 20
}
```

`query` is 1–4,000 Unicode characters after whitespace validation. Provider
values are `gmail`, `google_drive`, `github`, and `local_files`. `limit` applies
to ranked results and accepts 1–50. Explicit filters remain active even if no
result exists. Repository and folder IDs must resolve within the selected
workspace; inaccessible IDs produce the same response as unknown IDs.

Results mode returns ranked sources without model generation. Answer mode adds
the grounded answer contract and requires an idempotency key because it may
persist history and incur model cost:

```json
{
  "request_id": "018f4f3d-22f8-7c51-9f31-c22e1a7d9461",
  "mode": "answer",
  "results": [
    {
      "source_id": "018f4f72-830d-7e83-9db4-fd95bcb1dfe8",
      "chunk_id": "018f4f73-3f88-73c0-a429-8af88f4e74df",
      "title": "Payment retry decision",
      "provider": "gmail",
      "source_type": "email",
      "snippet": "Maya recommended capped exponential retries...",
      "score": 0.87,
      "source_timestamp": "2026-01-15T18:00:00Z",
      "source_timestamp_kind": "sent",
      "location": {"heading_path": [], "page": null, "line_start": null, "line_end": null},
      "target": {"kind": "provider_url", "open_url": "https://mail.google.com/mail/u/0/#inbox/example-message-id"}
    }
  ],
  "answer": {
    "answer_markdown": "Maya chose capped exponential retries [c1].",
    "claims": [
      {"id": "claim-1", "text": "Maya chose capped exponential retries.", "material": true, "citation_ids": ["c1"]}
    ],
    "citations": [
      {"id": "c1", "claim_ids": ["claim-1"], "source_id": "018f4f72-830d-7e83-9db4-fd95bcb1dfe8", "chunk_id": "018f4f73-3f88-73c0-a429-8af88f4e74df", "excerpt": "Maya recommended capped exponential retries..."}
    ],
    "insufficient_evidence": {"value": false, "reason": null},
    "follow_up_queries": []
  },
  "degradation": {"semantic_search": "ok", "answer_generation": "ok"},
  "timing": {"total_ms": 1820, "retrieval_ms": 410, "generation_ms": 1180}
}
```

The server derives excerpts and open targets from authorized stored metadata.
`score` is only an ordering value. `target.open_url` is an allowlisted provider
URL or application-owned local-source route; retrieved text cannot create it.
Revoked targets disappear or return 404 on later reads.

No results returns 200 with an empty list, active filters, and bounded safe
suggestions. Insufficient answer evidence returns 200 with the stage-03 reason
code and cited results when available. Retrieval infrastructure failure returns
a retryable error rather than falsely implying no evidence.

Suggestions accept a prefix plus active filter types, return at most ten
server-derived titles/people/collections, and never reveal suggestions from
outside authorized content. Search history returns metadata and query text only
to its owner; clearing it requires `X-Confirm-Action: clear-search-history`.

## Conversation and SSE contracts

Conversation creation accepts an optional bounded title. A conversation read
returns its metadata and a cursor page of messages. Each complete assistant
message includes hydrated claims and citations; interrupted or failed messages
never appear as complete.

Streaming request:

```json
{
  "client_message_id": "018f4f8b-4fc5-799f-a9ed-232a5c4e1643",
  "retry_user_message_id": null,
  "content": "Compare the retry proposals.",
  "providers": ["gmail", "github"],
  "filters": {
    "repository_ids": [],
    "folder_ids": [],
    "people": [],
    "source_types": [],
    "file_types": [],
    "date_from": null,
    "date_to_exclusive": null
  }
}
```

`client_message_id` and the idempotency key together prevent a duplicate user
message or model call. A normal request supplies `content` and leaves
`retry_user_message_id` null. A retry after interruption supplies the persisted
user-message ID, omits content, and uses a fresh client message ID and
idempotency key; the server reauthorizes the existing message before starting a
new assistant attempt. Before stream establishment, authentication, validation,
quota, or dependency errors use normal problem JSON. After headers are sent,
the server emits named SSE events with monotonically increasing opaque IDs:

| Event | Data contract |
| --- | --- |
| `message.started` | Request, user-message, and pending assistant-message IDs |
| `retrieval.completed` | Safe result summaries, degradation state, and timing |
| `claim.completed` | One fully buffered claim plus its validated, hydrated citations and rendered Markdown fragment |
| `message.insufficient` | Safe reason, cited results, and follow-up queries |
| `message.completed` | Final message ID, usage, complete timing, and persistence confirmation |
| `error` | Problem object with request ID and retryability |

The server sends an SSE comment heartbeat at least every 15 seconds. It sets
`Cache-Control: no-store`, `X-Accel-Buffering: no`, and disables proxy response
buffering. Each `claim.completed` unit is emitted only after its citations match
the supplied authorized context. The client does not display a factual claim
before that event.

`message.completed`, `message.insufficient`, and `error` are terminal. A client
disconnect cancels avoidable model work and marks the pending assistant message
interrupted. The MVP does not resume a partial model stream with
`Last-Event-ID`; a retry follows the persisted-user-message contract above.

## Source and citation contracts

Source list filters mirror search provider, collection, person, date, source
type, and file-type filters. Source detail returns safe metadata, sync/index
state, extraction failure code, approved preview when available, and its current
open target. It does not return credentials, absolute local paths, provider
payloads, deleted versions, embeddings, internal object keys, or other users'
metadata.

Provider targets are HTTPS URLs validated against provider host and resource
patterns. Local-file targets use an application-owned deep link containing only
an opaque source ID; the desktop app reauthorizes, maps it to its own manifest,
and requests user confirmation before opening. The cloud API never returns an
absolute local path.

Source deletion requires `X-Confirm-Action: delete-source`. Reindex returns an
operation and leaves the prior searchable version active until atomic promotion.
Citation excerpts are bounded server snapshots. If source permission is
revoked, citation opening fails safely and saved answers are marked incomplete
or removed during deletion reconciliation.

## Desktop synchronization contracts

Registration begins with a 60-second single-use challenge. The completion body
contains challenge ID, public key, platform, app version, display name, and a
signature over the challenge. A successful response returns the device ID and
one opaque device credential exactly once; the client stores it in the OS
keychain. If that response is lost, the client starts a new challenge; the
server never replays a raw credential from generic idempotency storage.

Manifest requests contain:

```json
{
  "manifest_id": "018f4fa1-dbbc-77c3-bb8a-1fe08cf626aa",
  "folder_id": "018f4fa2-6ccd-77f1-aa96-9d43f85b93de",
  "sequence": 4,
  "final": true,
  "entries": [
    {
      "external_id": "client-stable-id",
      "relative_path": "notes/retries.md",
      "size_bytes": 4812,
      "modified_at": "2026-08-03T20:00:00Z",
      "content_sha256": "base64url-sha256",
      "mime_type": "text/markdown"
    }
  ]
}
```

Each page contains at most 1,000 entries and 2 MB of JSON. Relative paths are
normalized, must remain under the registered folder root, and cannot contain
NUL, traversal, or absolute prefixes. The server returns requested upload IDs,
acknowledged deletions, skipped entries with safe reason codes, and the next
expected sequence. Replaying the same manifest ID and content returns the same
result; changed content with that ID returns a conflict.

Signed-upload requests bind `upload_id`, device, workspace, source external ID,
opaque object key, exact byte size, SHA-256 digest, MIME type, and expiry. URLs
expire within ten minutes, permit one `PUT`, and include required digest and
content-type headers. Object-store success alone never makes content searchable;
`changes` completion validates object metadata and creates an indexing operation.

Heartbeat contains app version, platform, and aggregate queue state only. It
does not send file lists or absolute paths. Device deletion requires
`X-Confirm-Action: revoke-device`, immediately rejects new signatures, and
offers tracked deletion of that device's indexed sources.

## Export and account-deletion contracts

Export requires a purpose-bound reauth token and returns an operation. When
complete, operation detail may issue a newly authorized, single-use download
URL with a short expiry. The URL is never stored in idempotency replay data and
cannot be regenerated after account deletion begins.

Account deletion requires all of:

- `X-Reauth-Token` with `delete_account` purpose;
- `X-Confirm-Action: delete-account`;
- JSON body `{"confirmation":"DELETE MY ACCOUNT"}`; and
- a fresh idempotency key.

The 202 response returns a deletion request ID, deadline no later than 24 hours,
and deletion receipt token exactly once. The commit revokes sessions, clears
provider credentials, and blocks search/sync immediately. The receipt endpoint
returns only request ID, status, requested/deadline/completed timestamps,
remaining resource counts, sanitized failure codes, and support guidance. It
cannot reveal identity, source, or provider content.

Repeated deletion with the same idempotency key returns the existing request
and, if the original receipt was already delivered, issues a replacement only
after an explicitly documented recovery challenge; generic idempotency storage
never replays the raw receipt.

## Webhook and provider-callback safety

OAuth callbacks validate fixed callback origin, state hash, nonce, expiry,
single use, PKCE, provider response, and current user/workspace eligibility
before exchanging or storing credentials. Callback error redirects use a small
allowlisted result-code vocabulary and never copy provider error descriptions.

GitHub webhooks verify the signature against the raw body before JSON parsing,
enforce timestamp/replay policy when available, derive the installation from
the verified payload, and deduplicate the provider delivery ID. Unknown,
revoked, or unselected installations return a non-enumerating accepted or
ignored response. The endpoint acknowledges durable job creation quickly;
provider fetching and normalization run asynchronously.

Webhook bodies have strict byte limits and are not logged. Retrieved URLs,
callback parameters, provider bodies, and webhook content cannot choose an
outbound destination or database workspace.

## CORS, caching, and transport security

- Production CORS allows only configured exact web origins, never `*` with
  credentials. Desktop bearer clients do not depend on browser CORS.
- Unsafe cookie requests require CSRF and Origin validation. SameSite cookies
  are defense in depth, not the only check.
- HSTS, TLS versions, cipher policy, proxy trust, and certificate rotation are
  deployment-owned. The application trusts forwarded headers only from
  configured proxies.
- Authentication, source, search, conversation, operation, export, usage, and
  deletion responses are `no-store`.
- OAuth authorization URLs, signed upload/download URLs, refresh tokens,
  reauth tokens, and deletion receipts are never cached or logged.
- Content compression is disabled for secrets and SSE where it creates leakage
  or buffering risk; static public assets use normal immutable caching.
- Maximum header, JSON, multipart, and stream durations are bounded at the edge
  and application.

## Observability and privacy

Every request records method template, status, duration, request ID, service,
client class/version, safe error code, and keyed hashes of workspace/user/device
IDs when available. Search stage timing, operation IDs, job correlation, model
usage, rate-limit outcome, and SSE terminal state use structured fields.

Logs and metrics exclude raw authorization/cookie/CSRF/signature headers,
idempotency keys, emails, OAuth state/code, queries, prompts, results, excerpts,
provider URLs/bodies, local paths, upload URLs, deletion receipts, and raw
request/response bodies. Route templates are logged instead of identifier paths.
Unexpected exceptions map to opaque 500 responses and restricted diagnostics.

## Contract and security testing

The API test suite must include:

- OpenAPI schema validation, example validation, generated TypeScript diff, and
  backward-compatibility comparison;
- every route's success, validation, authentication, authorization, workspace,
  content-type, and documented error behavior;
- browser cookie, bearer, ambiguous-auth, CSRF, Origin, CORS, refresh rotation,
  logout, reset, and session-replay tests;
- two-workspace and two-user object-level authorization tests for every ID and
  cursor-bearing route, including random UUID enumeration;
- idempotency same-body replay, changed-body conflict, concurrent request,
  crash-after-commit, expiry, and secret-exclusion tests;
- keyset pagination under concurrent inserts/deletes and cursor tampering,
  context mismatch, and expiry tests;
- rate-limit boundary, retry header, separate-bucket, and rejected-usage tests;
- OAuth denial, state/nonce/PKCE expiry/replay, fixed return path, provider-error
  sanitization, and read-only scope tests;
- webhook signature-before-parse, delivery replay, body limit, unknown
  installation, and durable-acknowledgement tests;
- search filter combinations, no-results, insufficient evidence, degraded
  lanes, citation lineage, revoked target, and prompt-injection output tests;
- SSE event ordering, validated-claim buffering, heartbeat, disconnect,
  interruption, terminal error, proxy-buffer, and duplicate-message tests;
- desktop challenge, signature, timestamp, nonce replay, traversal, manifest
  sequencing, file-size, digest, MIME, signed URL scope/expiry, and revocation
  tests;
- operation retry, progress monotonicity, sanitized error, and cross-tenant
  status tests; and
- export/deletion reauth, exact confirmation, immediate access cutoff, receipt
  confinement, 24-hour deadline, and complete reconciliation tests.

Fuzzing targets JSON decoders, filter schemas, cursors, IDs, signature headers,
path normalization, SSE parsing, and authorization order. Tests use synthetic
private content and fake providers only.

## Requirement coverage

| Requirement | API support |
| --- | --- |
| `AUTH-001` | Non-enumerating registration/reset, verified identity, rotating cookie/bearer sessions, CSRF, logout, and reauthentication |
| `DESKTOP-001` | Signed device registration, scoped manifests/uploads, path rejection, revocation, and deletion operation |
| `GOOGLE-001` | Single-use PKCE authorization, read-only source families, safe callback, selection, status, and disconnect contracts |
| `GITHUB-001` | GitHub App authorization, selected repository IDs, verified deduplicated webhook ingress, and reconciliation status |
| `SYNC-001` | Idempotent sync commands, durable operations, monotonic progress, last success, sanitized errors, and safe retries |
| `SEARCH-001` | Workspace-first authorization, typed filters, deterministic pages, safe no-results, quota, and degradation behavior |
| `ANSWER-001` | Non-stream and SSE grounded-answer shapes, buffered validated claims, conflicts, and insufficient-evidence events |
| `CITATION-001` | Server-hydrated lineage, bounded excerpts, safe provider/deep-link targets, and revoked-target failure |
| `CONNECTION-001` | Reauth and exact confirmation, immediate credential/access cutoff, idempotent 202 operation, progress, and 24-hour deadline |
| `ACCOUNT-001` | Reauth, exact phrase, immediate logout, one-purpose receipt, progress-only status, export, and 24-hour deadline |
| `SAFETY-001` | Strict boundary schemas, authorization before loading, no content-created links/tools, prompt-injection tests, and secret-safe errors/logs |

## Stage 05 completion criteria

- Transport, versioning, schemas, headers, authentication modes, workspace
  selection, CORS, CSRF, caching, and serialization rules are explicit.
- Every public route has method, path, authentication, idempotency, and success
  status ownership.
- Pagination, filtering, rate limits, quotas, retries, ETags, idempotency, and
  asynchronous operations have deterministic behavior.
- Search, grounded answers, citations, SSE events, source targets, desktop
  synchronization, OAuth callbacks, webhooks, exports, and deletion receipts
  align with prior safety and database stages.
- Errors are typed, retry-aware, non-enumerating, and exclude private content
  and internal details.
- Contract, cross-tenant, replay, streaming, tampering, and deletion tests are
  defined.
- Every product requirement maps to concrete API behavior.
- The shared runtime supplies OpenAPI 3.1 routing, request IDs, strict boundary
  models, problem details, cursor/idempotency helpers, and fail-closed
  auth/workspace interfaces while product routes remain closed.
- `./scripts/validate-api-specification.sh` passes.
