# Authentication and OAuth

## Goals and ownership

This document owns application identity, password authentication, email proof,
session lifecycle, reauthentication, workspace authorization, provider OAuth,
GitHub App authorization, credential storage, and revocation. It refines the
HTTP surface in [`05_API_SPECIFICATION.md`](05_API_SPECIFICATION.md), persists
only through [`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md), and obeys the
security controls in [`08_SECURITY_AND_PRIVACY.md`](08_SECURITY_AND_PRIVACY.md).

The first implementation slice after this design checkpoint is deliberately
smaller than this document: email/password registration, verification, login,
refresh, logout, `me`, workspace creation/membership, and their minimal web UI.
Google and GitHub connection implementation remains in provider phases.

## Identity and account state

`users` is the application principal. `auth_identities` maps a verified issuer
and subject to one user; email is profile/contact data and is not the stable
external subject. The MVP supports a password identity and may later add
allowlisted social-login issuers without silently linking accounts.

Canonical email comparison uses the database's case-insensitive identity
constraint after trimming surrounding whitespace. The application does not
apply provider-specific dot or plus-address rewriting. Registration, resend,
reset, and failed login return generic responses so callers cannot enumerate
accounts.

User states are `pending_verification`, `active`, `suspended`, and `deleting`.
Only `active` users receive normal sessions. A verified user receives a personal
workspace and active owner membership in the same transaction; retrying that
transition is idempotent. Suspended/deleting users, suspended memberships, and
deleting workspaces fail closed.

## Password registration and storage

Registration accepts the bounded fields defined by the API contract, records
the accepted terms version, and validates passwords without logging or
persisting plaintext. The initial policy is:

- 12–128 Unicode characters after rejecting control/NUL characters;
- no composition rules that reduce usable passphrases;
- reject values found in a locally checked compromised/common-password set and
  values containing the normalized email address; and
- allow paste and password-manager generated values.

Passwords are hashed with Argon2id through a maintained library. Parameters are
versioned and calibrated in deployment so verification is intentionally
expensive without exhausting the API under the configured login concurrency.
The baseline target is 64 MiB memory, 3 iterations, parallelism 1, and a
16-byte or larger random salt; calibration may raise these values. A successful
login opportunistically rehashes an older/weaker encoded hash in the same
transaction. Hashes are never exposed to clients, logs, audit details, or
general user repositories.

## Email verification and password reset

Verification and reset values contain at least 256 bits of random base64url
entropy. Only a keyed hash, purpose, user ID, creation/expiry, and consumption
state are stored in `one_time_tokens`. The raw value is sent once in an HTTPS
application-owned URL and never appears in analytics or logs.

- Email verification expires after 24 hours.
- Password reset expires after 30 minutes.
- Tokens are single-use and consumed atomically with the state change.
- Issuing a newer live token invalidates older tokens for that user/purpose.
- Resend/reset endpoints always return the generic 202 response and are
  rate-limited by normalized IP plus a keyed email bucket.
- Email links land on the web app, which submits the token in a request body;
  tokens are not sent to third-party assets or retained in browser history.

Password reset updates the Argon2id hash, increments the user's authorization
version, revokes every session family and reauthentication token, consumes the
reset token, and writes an audit event in one transaction. It sends a security
notification without including secrets.

## Login and session issuance

Login verifies a password even for unknown/ineligible accounts by using a
current dummy Argon2id hash, reducing timing-based enumeration. The public
failure is always `INVALID_CREDENTIALS`. Rate limits use IP and keyed identity
buckets, while successful login clears only the appropriate failure state.

On success the service creates a server session family, rotates CSRF state,
records safe device metadata, and returns the memberships described by
`GET /v1/auth/me`. The browser and native transports share server-side session
semantics but expose credentials differently.

Access tokens are signed, audience/issuer checked tokens valid for at most 15 minutes.
They contain a token ID, user ID, session ID, issued/expiry times,
authentication time, and authorization version. They contain no email, private
profile, provider token, workspace authority, or content. A workspace claim, if
later added as a cache hint, never replaces membership lookup on sensitive
requests. Signing keys use explicit algorithms and key IDs; verification
allowlists algorithms and validates every required claim.

Refresh tokens are opaque values with at least 256 bits of entropy. Only their
keyed hash and session-family metadata are stored. A refresh token is returned
once, never available from a later read, and cannot be accepted as an access
token.

## Browser and native credential transport

Browser sessions use the three exact host-only cookies defined in the API
specification:

- `__Host-uas_access`: `Secure`, `HttpOnly`, `Path=/`, `SameSite=Lax`;
- `__Host-uas_refresh`: `Secure`, `HttpOnly`, `Path=/`, `SameSite=Strict`; and
- `__Host-uas_csrf`: `Secure`, `Path=/`, `SameSite=Strict`, readable only so
  the same-origin client can echo it as `X-CSRF-Token`.

Cookie-authenticated unsafe requests require a configured exact `Origin` and a
constant-time match between the CSRF header, cookie, and session-bound CSRF
hash. Login and refresh rotate CSRF state. Logout expires all cookies even when
the server session is already absent. Requests mixing browser cookies with an
`Authorization: Bearer` credential are rejected.

The desktop/native client keeps the refresh token only in the operating-system
keychain and keeps access tokens in memory when practical. Native login and
refresh return the new refresh token once over TLS; browser endpoints never put
refresh tokens in JSON or readable storage. No browser bundle or desktop log
may contain an application or provider client secret.

## Refresh rotation and replay response

Refresh is an atomic compare-and-rotate operation:

1. Hash the presented token and lock its session-family record.
2. Confirm token/session/user state, absolute expiry, idle expiry, and current
   authorization version.
3. If current, mark it used, store the successor hash, and issue one new access
   token plus one new refresh token.
4. If an already-used token is presented, revoke the entire family and all of
   its descendants, increment the user's authorization version when warranted,
   reject the request, and write a high-risk audit event.

Concurrent requests therefore produce one successful rotation; the loser is
treated as replay and revokes the family. Clients serialize refresh attempts
and return to login after ambiguity. There is no grace window that permits the
same refresh credential twice.

Refresh tokens have a maximum absolute lifetime of 30 days and an initial
seven-day idle limit, both configurable only downward without a reviewed
change. Rotation cannot extend the family beyond its absolute expiry. Logout
revokes the current family. Password reset, account suspension/deletion, or a
high-confidence compromise revokes all families. Ordinary password change
requires recent reauthentication and also revokes other families.

## Reauthentication and sensitive actions

Disconnect, export, password change, and account deletion require a fresh
authentication event. `POST /v1/auth/reauthenticate` verifies the current
password or a future approved identity challenge and returns a random opaque
proof. Only a keyed hash is stored.

The proof is single-use, expires in five minutes, and is bound to user, session,
purpose (`disconnect`, `export`, `change_password`, or `delete_account`), and
authorization version. The target route consumes it atomically and also
requires the exact confirmation/idempotency contract defined in stage 05.

## Workspace authorization

Authentication establishes who is calling; it does not establish tenant
access. For each workspace route the API:

1. Authenticates the session and rejects stale/revoked authorization versions.
2. Parses `X-Workspace-ID` as a selector, not proof.
3. Loads that user's active membership and verifies user/workspace state.
4. Establishes transaction-local `app.user_id` and `app.workspace_id`.
5. Loads the target resource with an explicit workspace predicate.

The MVP roles are `owner`, `admin`, and `member`. Owners may delete the account
or workspace and manage ownership; admins may manage connections and members;
members may search and view authorized sources. Exact route permissions live in
one policy module and are tested as a matrix. Membership/role/status changes
increment an authorization version and take effect before cached claims,
cursors, streams, or replays can disclose data. Inaccessible resources return a
non-enumerating 404 after authentication.

## OAuth transaction contract

Provider authorization begins only for an active, email-verified user with an
active workspace membership that permits connection management. The API creates
an `oauth_transactions` record with:

- a random state value whose keyed hash is stored;
- user, workspace, provider, intended action, exact callback identifier, and
  allowlisted post-callback route identifier;
- a random nonce when the provider uses OpenID Connect;
- an RFC 7636 PKCE verifier whose encrypted form is stored only until callback,
  and its `S256` challenge sent to the provider; and
- creation and expiry no more than 10 minutes apart.

State has at least 256 bits of entropy, is bound to the initiating browser
session, and is consumed atomically before code exchange. The callback uses one
fixed registered HTTPS URI per provider/environment. It rejects missing,
expired, used, provider/action-mismatched, session-mismatched, or malformed
state; OAuth errors are also consumed to prevent replay. No caller-supplied URL
is used for callback or final navigation.

The token request uses the exact provider token endpoint, PKCE verifier, fixed
redirect URI, and confidential-client authentication from the secrets service
where required. Authorization codes and callback query strings are never
logged. The service validates issuer, audience, nonce, subject, signature, and
time claims for identity tokens. Provider identity is linked only after an
authenticated explicit flow; matching an unverified email never auto-links an
account.

## Google connection flow and scopes

1. The user selects Connect Google for an authorized workspace.
2. The API creates the OAuth transaction and returns a provider authorization
   URL from fixed configuration.
3. Google authenticates the user and displays requested consent.
4. The callback consumes state, verifies PKCE/provider response, and exchanges
   the code server-to-server.
5. The service verifies the granted scopes are an allowed subset and include
   those required for the selected connector.
6. Provider credentials are envelope-encrypted and a connection plus exact
   scope rows are created atomically.
7. The initial sync job is queued through the transactional outbox, and the
   callback redirects with 303 to the stored allowlisted UI route.

Gmail and Drive use the narrowest read-only scopes that support the selected
features. Consent is separated when combined scopes reduce user trust or
provider approval likelihood. The product never asks for send, modify, delete,
sharing, or broad account-management authority. The exact production scope
allowlist must be pinned in provider configuration and tested before a provider
connector is enabled.

Incremental authorization may add an allowed scope only after a new explicit
consent. A callback that returns broader, missing, or unexpected scopes fails
closed and does not schedule sync. Revoked or invalid grants mark the connection
`reauthorization_required` without an infinite refresh loop.

## GitHub App installation flow

Repository access uses a GitHub App, not a classic OAuth token. The initiation
creates a single-use installation transaction with the same state, session,
workspace, expiry, and redirect rules. The user installs the app only on
selected repositories. Callback handling verifies the installation belongs to
the expected GitHub App, records the installation ID and selected repository
identities, and schedules reconciliation.

The private key remains in the secrets/KMS boundary. Workers create short-lived
installation access tokens only when needed, keep them in memory, validate the
repositories/permissions returned by GitHub, and never persist or expose the
token. Webhooks verify the GitHub signature against the exact raw body, enforce
delivery-ID replay protection, bound body size before parsing, and treat events
as hints. Scheduled reconciliation removes access and derived content when an
installation or repository is removed.

## Provider credential envelope

Every persisted Google refresh/access credential is protected by per-record
envelope encryption:

1. Generate a random 256-bit data-encryption key (DEK).
2. Encrypt a versioned credential payload with an authenticated cipher such as
   AES-256-GCM using a unique nonce.
3. Bind provider, connection ID, workspace ID, credential-record ID, and schema
   version as authenticated additional data.
4. Ask the configured KMS key to wrap the DEK and store ciphertext, nonce,
   wrapped DEK, KMS key/version reference, and encryption schema version.
5. Zero/discard plaintext credentials and DEKs as soon as the provider call
   completes.

Only a narrow credential service can decrypt. General repositories, API
responses, jobs, idempotency records, audit events, and connectors at rest
cannot access envelopes. KMS permission is limited to the API callback and
authorized provider worker identities with encryption-context checks.

Encryption-key rotation rewraps DEKs without decrypting provider plaintext
where supported. Credential-schema rotation decrypts and re-encrypts one
record through the credential service. Both operations are idempotent,
audited, resumable, and retain the previous key only for the reviewed rollback
window. Failure never writes plaintext or drops the last valid ciphertext.

Provider access tokens are kept in memory and preferably not persisted. If a
provider requires temporary persistence, it uses the same envelope and expires
at the provider timestamp. Refresh is single-flight per connection; a worker
must recheck connection/workspace state immediately before decrypting and again
before committing provider results.

## Disconnect, revocation, and compromise

Disconnect requires recent reauthentication, exact confirmation, membership
authorization, and idempotency. Its cutoff transaction changes the connection
to `deleting`, prevents new sync/search access, clears the usable credential
reference and scope grants, cancels runnable jobs, creates the deletion request,
and writes outbox/audit records. Provider revocation is attempted with bounded
retries; local credential deletion and access cutoff do not wait indefinitely
for the provider.

The deletion worker removes encrypted credential material and derived content,
reconciles object storage, and exposes content-free progress. Reconnecting
creates a new authorization transaction and connection authority; it cannot
resurrect a deleting connection or its old credentials.

Suspected credential disclosure immediately disables the connection, revokes
the provider grant when possible, cancels work, rotates affected application
keys/secrets, and alerts operators without putting secrets in the incident
record. Session compromise follows the family/all-family revocation rules.

## Audit, redaction, and failure behavior

Authentication audit events include safe action/outcome codes, opaque user and
session IDs when known, request ID, normalized network risk metadata, and UTC
time. OAuth events may include provider and opaque connection/installation IDs.
They never include email, password/hash, raw token, cookie, code, state, nonce,
PKCE verifier, authorization URL, provider response, credential ciphertext, or
KMS material.

Expected provider/auth failures become stable problem codes. Client responses
do not distinguish unknown email from bad password, disclose linked providers,
or reveal whether a guessed workspace/resource exists. Dependency failure is
fail-closed: the system never issues a session, accepts stale membership, stores
plaintext credentials, or schedules sync because Redis, KMS, database, email,
or a provider is unavailable.

## Authentication and OAuth test matrix

The implementation is incomplete until automated tests prove:

1. Registration enumeration resistance, password policy, Argon2id storage and
   upgrade, verification expiry/single-use, and idempotent workspace creation.
2. Generic/timing-normalized login failure, account-state gates, session
   fixation prevention, cookie attributes, and native keychain transport.
3. Access claim validation, logout, absolute/idle expiry, one-success concurrent
   refresh, refresh replay family revocation, password-reset revocation, and
   stale authorization-version rejection.
4. Origin and CSRF enforcement, mixed-auth rejection, reauthentication purpose,
   expiry, single use, and exact destructive confirmation.
5. Role matrix, cross-user/workspace/resource isolation, transaction-local RLS
   context, membership changes during pagination/replay/SSE, and worker
   authority derived only from durable jobs.
6. OAuth state/session/provider binding, PKCE `S256`, nonce and issuer checks,
   expiry, single use, callback error replay, fixed redirect, open-redirect
   rejection, and account-linking safety.
7. Exact Google scope allowlists, missing/unexpected scope failure, encrypted
   persistence, refresh single-flight, invalid-grant behavior, and no browser
   provider-token exposure.
8. GitHub App identity/installation/repository checks, short-lived in-memory
   tokens, webhook signature/delivery replay, and access reconciliation.
9. Envelope round-trip, wrong additional-data/key failure, KMS denial,
   rotation/resume, ciphertext uniqueness, and proof that repositories/logs/
   queues/errors never contain plaintext credentials.
10. Disconnect cutoff races, provider-revocation failure, job cancellation,
    derived-data deletion, reconnect isolation, and account deletion.
11. Rate-limit, audit-event, email-link, error-redaction, and dependency
    fail-closed behavior.

Tests use synthetic identities and provider responses. CI never requires live
user credentials. Provider sandbox/live smoke tests are separate, opt-in,
redacted, and use disposable least-privilege accounts.

## Requirement coverage

| Requirement | Authentication contribution |
| --- | --- |
| `AUTH-001` | Complete identity, password, verification, session, refresh, logout, workspace, and recovery contract. |
| `GOOGLE-001` | Safe Google consent, exact read-only scopes, state/PKCE validation, encrypted credentials, and refresh behavior. |
| `GITHUB-001` | GitHub App installation identity, selected repository authority, short-lived tokens, and verified webhooks. |
| `SYNC-001` | Requires active connection authority before credential use and prevents refresh/retry loops. |
| `CONNECTION-001` | Defines immediate disconnect cutoff, revocation, credential deletion, and reconnect isolation. |
| `ACCOUNT-001` | Defines reauthentication and global session/provider revocation before deletion. |
| `SAFETY-001` | Prevents identity enumeration, confused-deputy OAuth, token leakage, and caller-selected tenant authority. |

## Stage 09 completion criteria

This specification is complete when application identity and every secret
lifecycle have explicit issuance, storage, validation, rotation, revocation,
redaction, failure, and test rules; workspace authority and provider callbacks
cannot be caller-selected; and the first authentication vertical slice is
bounded. The implemented subset now includes application authentication,
Google connection authorization, encrypted Google credentials, safe access-token
refresh during Gmail jobs, and durable Gmail initial synchronization. Password
recovery, Google login, GitHub authorization, provider revocation, production
KMS rotation, and the full security review remain later work.

Backfill `B4` delivered the first authentication vertical slice; the current
provider work extends that boundary without changing this document's ownership.
