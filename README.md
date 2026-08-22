# Universal AI Search

Universal AI Search is a read-only search and question-answering product for
local files, Gmail, Google Drive, and GitHub. It combines keyword and vector
search, then returns grounded answers with links back to the original sources.

The repository is being specified and built incrementally from the documents in
[`universal_ai_search_documentation/`](universal_ai_search_documentation/00_README.md).
Numbered-document completion and product-feature completion are tracked
separately so a validated design is never presented as working product code.

## Current status

- Specification status: the numbered design set is validated; implementation
  evidence is tracked separately in the status ledger
- Implementation status: foundation is partial (`27/30` master tasks), the
  authentication phase is partial (`12/18` master tasks), the database phase
  is partial (`12/15` master tasks), the connector framework is partial (`7/11`
  master tasks), indexing is complete for normalized text/Markdown (`10/10`),
  search is complete for the local-index backend (`9/9`), and end-to-end MVP
  acceptance is `1/11`
- Backfill status: `B0` through `B6` are complete; `B7` is active, with Google
  authorization plus bounded Gmail full/incremental synchronization and advanced
  email parsing, attachment extraction, normalization, deletion reconciliation,
  and error recovery implemented
- Next implementation slice: Google Drive DOCX extraction (`P6-005`)
- Product status: tested project foundation, an implemented 33-table
  PostgreSQL schema with forced tenant RLS, an implemented Python connector
  SDK, a tested fail-closed API platform layer, and a working six-endpoint
  email/password authentication flow with a minimal web UI, durable indexing,
  tenant-safe hybrid search, Google OAuth connection authorization, and Gmail
  full/incremental sync with safe MIME parsing, stable attachment sources, and
  searchable sender/recipient metadata in the index. Completed Gmail scans now
  reconcile provider absences, deletions purge derived index data, and transient
  failures use bounded durable retries. Google Drive now traverses selected
  folder trees, extracts bounded PDF text, and safely indexes descriptors for
  unparseable PDFs; remaining Drive formats, provider UIs, and the remaining
  40 product endpoints remain to be built
- Safety boundary: version 1 is read-only; retrieved content is untrusted data
  and cannot trigger actions

See the
[implementation status ledger](universal_ai_search_documentation/IMPLEMENTATION_STATUS.md)
for requirement-by-requirement evidence, explicit missing features, and the
dependency-ordered backfill sequence.

## MVP

The MVP will provide:

- Web and Tauri desktop applications
- Local-folder, Gmail, Google Drive, and GitHub connectors
- Hybrid keyword and semantic search
- AI answers with clickable citations
- Connector revocation and data deletion
- Production deployment, monitoring, and operational controls

See the [project specification](universal_ai_search_documentation/01_PROJECT_SPEC.md)
for the complete requirements and measurable success criteria.

## Specification and implementation workflow

The numbered documents are the design source of truth and are specified in
order. Production work follows executable dependencies recorded in the status
ledger. For every design or implementation checkpoint:

1. Read the stage and its referenced dependencies.
2. Turn its requirements into concrete acceptance criteria.
3. Record whether each item is design-only, partial, or implemented.
4. Implement only the dependency-safe approved scope.
5. Run its automated tests, linters, and relevant manual checks.
6. Update the status ledger, documentation, and master task list.
7. Review the diff, commit the checkpoint, and push it before continuing.

## Local development

Prerequisites are Node.js 22, pnpm 11, and Docker Desktop with Docker Compose.
Python 3.12 and Rust stable are needed only for host-side API and Tauri work;
their foundation checks can also run in the supplied containers.

Start the complete local stack with:

```sh
cp .env.example .env
pnpm install --frozen-lockfile
./scripts/dev-up.sh
```

Compose runs the Alembic migration as a one-shot service before the API starts.
The API readiness probe also rejects a database whose revision does not match
the application.

The services are then available at:

- Web application: <http://localhost:3000>
- API readiness: <http://localhost:8000/health/ready>
- MinIO console: <http://localhost:9001>
- Mailpit: <http://localhost:8025>

Stop the stack without deleting its named volumes with:

```sh
./scripts/dev-down.sh
```

Run documentation, formatting, lint, type, and unit checks with:

```sh
pnpm check
```

Run the containerized Python and desktop Rust checks with:

```sh
./scripts/test-backend.sh
./scripts/test-connector-sdk.sh
./scripts/test-database.sh
./scripts/test-desktop-rust.sh
```

With the local stack running, exercise registration, Mailpit verification,
login, workspace discovery, token rotation, and logout end to end with:

```sh
python3 scripts/smoke-auth.py
```

### Optional Google authorization setup

Google connection authorization is disabled by default, so the normal local
stack and CI never require real provider secrets. To exercise it against a
Google Cloud test project, enable the Gmail and Drive APIs, create a Web OAuth
client with this exact local callback URI, and set the following only in the
untracked `.env` file:

```text
UAS_GOOGLE_OAUTH_ENABLED=true
UAS_GOOGLE_CLIENT_ID=your-client-id
UAS_GOOGLE_CLIENT_SECRET=your-client-secret
UAS_GOOGLE_REDIRECT_URI=http://localhost:8000/v1/connections/google/callback
```

The API requests only pinned Gmail/Drive read-only scopes and refuses missing
or unexpected grants. Google classifies these broad read-only scopes as
restricted, so public deployment requires its OAuth verification and security
review. A Gmail connection now creates a durable full-sync job. The worker
refreshes encrypted credentials when needed, imports the mailbox in bounded
25-message pages, queues each normalized message for indexing, and commits the
Gmail history cursor only after the final page. The worker then polls Gmail
history in bounded pages, reindexes changed messages, tombstones deleted ones,
purges their attachment and derived index data, and falls back to a controlled
full sync if Gmail expires the cursor. Full scans use a per-run marker so a
source is deleted only after an authoritative scan finishes; a failed or partial
scan cannot erase unseen mail. Rate limits honor bounded `Retry-After` values,
other transient failures use bounded exponential full jitter, and exhausted or
permanent failures enter durable terminal states. MIME parsing
prefers plain text, safely falls back to visible HTML, decodes declared character
sets and encoded headers, skips attachment bodies, and removes quoted history or
signatures only at high-confidence boundaries. Attachments are separate stable
sources: bounded textual parts are fetched and indexed, while unsupported binary
or oversized parts retain a searchable descriptor and extraction status without
downloading unsafe content. Allowlisted RFC message relationships, dates,
labels, attachment details, and bounded sender/recipient identities are stored
as structured metadata. Person filters use those identities, and metadata can
refresh without creating a duplicate content version. Drive jobs now traverse one
bounded folder page at a time, keep folder/page state encrypted, index stable file
descriptors with logical paths and owners, and never follow shortcuts outside the
selected tree. PDFs are downloaded through the read-only media endpoint with a
20 MiB limit, parsed into normalized searchable text with page/text bounds, and
retain explicit fallback statuses when empty, encrypted, malformed, or oversized.
DOCX and native Google document extraction are the next Drive steps. There is not
yet a connection UI, and the live Google sandbox
smoke test still requires your own Google test project.

## Docker foundation

Docker is part of the foundation rather than a final packaging step. Docker
Compose runs PostgreSQL with pgvector, the one-shot schema migrator, Redis,
MinIO, Mailpit, the FastAPI service, and the production Next.js build. Separate
test targets provide repeatable Python, PostgreSQL migration/RLS, and Tauri Rust
checks. The local topology is specified in
[`19_LOCAL_DEVELOPMENT.md`](universal_ai_search_documentation/19_LOCAL_DEVELOPMENT.md),
while production image build, scanning, and deployment are covered by
[`11_DEPLOYMENT_AND_INFRASTRUCTURE.md`](universal_ai_search_documentation/11_DEPLOYMENT_AND_INFRASTRUCTURE.md).

## Security

Never commit secrets, OAuth credentials, private keys, production exports, or
real user data. Copy future environment templates to local untracked files and
keep placeholders in `.env.example` only.

## License

No license has been selected yet. Until one is added, the project is not
licensed for redistribution or reuse.
