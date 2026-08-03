# Connector Framework

## Goal
All providers implement the same lifecycle and produce the same normalized document model.

## Required interface
```python
class Connector(Protocol):
    provider: str
    async def authorize_url(self, state: str) -> str: ...
    async def exchange_code(self, code: str) -> Credentials: ...
    async def refresh_credentials(self, credentials: Credentials) -> Credentials: ...
    async def full_sync(self, ctx: SyncContext) -> AsyncIterator[Change]: ...
    async def incremental_sync(self, ctx: SyncContext, cursor: dict) -> AsyncIterator[Change]: ...
    async def fetch_item(self, external_id: str) -> RawItem: ...
    async def normalize(self, item: RawItem) -> NormalizedDocument: ...
    async def health_check(self) -> HealthResult: ...
```

## Change model
- UPSERT source
- DELETE source
- PERMISSION_CHANGED
- CURSOR_ADVANCED

## Normalized document
- external_id
- provider
- source_type
- title
- content
- canonical_url
- mime_type
- author
- created_at
- modified_at
- access_metadata
- provider_metadata

## Connector responsibilities
- Authentication and token refresh
- Provider API pagination
- Rate-limit compliance
- Initial and incremental synchronization
- Deletion detection
- Normalization
- Stable external IDs

## Connector non-responsibilities
- Chunking
- Embedding generation
- Search ranking
- Answer generation

## Retry policy
Use exponential backoff with jitter. Retry 429 and transient 5xx responses. Do not retry invalid credentials, revoked access, or permanent permission errors.

## Gmail
- Import messages and attachments separately
- Store thread ID and message ID
- Remove repeated quoted history and signatures where possible
- Use provider history cursor for incremental sync

## Google Drive
- Export native Docs, Sheets, and Slides to parseable formats
- Preserve folder path and modified time
- Track file IDs and change cursor

## GitHub
- Use a GitHub App
- Scope installation to selected repositories
- Ingest README, docs, source files, issues, pull requests, and review comments
- Exclude binaries, vendored dependencies, generated files, and secrets
- Use webhooks plus scheduled reconciliation

## Connector SDK tests
Each connector must pass authentication, pagination, idempotency, deletion, cursor recovery, rate-limit, token-expiry, and malformed-item tests.
