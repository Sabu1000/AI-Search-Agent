# Universal AI Search

Universal AI Search is a read-only search and question-answering product for
local files, Gmail, Google Drive, and GitHub. It combines keyword and vector
search, then returns grounded answers with links back to the original sources.

The repository is being specified and built incrementally from the documents in
[`universal_ai_search_documentation/`](universal_ai_search_documentation/00_README.md).
Numbered-document completion and product-feature completion are tracked
separately so a validated design is never presented as working product code.

## Current status

- Specification status: `00_README.md` through `06_CONNECTOR_FRAMEWORK.md`,
  plus the dependency-safe Stage 08/09 security and authentication checkpoint,
  are validated
- Implementation status: foundation is partial (`27/30` master tasks), the
  authentication phase is partial (`12/18` master tasks), the database phase
  is partial (`12/15` master tasks), the Connector SDK is partial (`5/11`
  master tasks), and end-to-end MVP acceptance is `1/11`
- Backfill status: database runtime `B1`, API platform primitives `B2`, and the
  Stage 08/09 security/authentication design checkpoint `B3`, and the
  authentication vertical slice `B4` are complete
- Next numbered specification and implementation slice:
  `07_INDEXING_PIPELINE.md` (`B5`)
- Product status: tested project foundation, an implemented 33-table
  PostgreSQL schema with forced tenant RLS, an implemented Python connector
  SDK, a tested fail-closed API platform layer, and a working six-endpoint
  email/password authentication flow with a minimal web UI; indexing, search,
  providers, and the remaining 43 product endpoints remain to be built
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
