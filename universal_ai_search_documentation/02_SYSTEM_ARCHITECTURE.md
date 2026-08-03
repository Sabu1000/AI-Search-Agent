# System Architecture

## Architecture style
Start as a modular monolith with separate worker processes. Avoid microservices until scale or team boundaries justify them.

## Components

### Web application
- Next.js and TypeScript
- Server-rendered authentication pages
- Search UI, chat UI, source browser, connection management
- Server-Sent Events for answer streaming

### API application
- FastAPI
- Authentication and authorization
- OAuth callbacks
- Search orchestration
- Source and account APIs
- Signed upload URLs for desktop ingestion

### Worker application
- Background synchronization
- Parsing and normalization
- Chunking and embedding generation
- Deletion workflows
- Retry and reconciliation jobs

### Desktop application
- Tauri shell
- React UI
- Rust filesystem access
- Folder permissions, scanning, hashing, watching, local queue

### Data stores
- PostgreSQL with pgvector
- Redis for jobs, locks, rate limiting, and short-lived cache
- S3-compatible object storage for encrypted extracted content and large artifacts

### External providers
- OpenAI API
- Gmail API
- Google Drive API
- GitHub App APIs and webhooks
- Stripe in beta

## Request path
1. User sends query.
2. API validates session and ownership.
3. Query planner extracts intent and filters.
4. Keyword and vector retrieval run in parallel.
5. Candidates are merged and reranked.
6. Context builder selects source passages.
7. LLM produces a structured answer.
8. Citation validator confirms cited source IDs.
9. API streams answer and citations.

## Ingestion path
1. Connector emits a normalized source item.
2. Worker writes source metadata.
3. Parser creates normalized document text.
4. Chunker creates structure-aware chunks.
5. Embeddings are generated in batches.
6. Keyword and vector indexes are updated.
7. Source is marked searchable.

## Tenant isolation
Every tenant-owned row contains `workspace_id`. Every query includes `workspace_id` before retrieval. No global retrieval followed by application-side filtering is allowed.

## Deployment units
- `web`
- `api`
- `worker-sync`
- `worker-index`
- `worker-delete`
- `desktop`

## Availability targets
- Public beta API: 99.5%
- Production API: 99.9%
- Recovery point objective: 15 minutes
- Recovery time objective: 2 hours
