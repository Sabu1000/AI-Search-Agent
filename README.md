# Universal AI Search

Universal AI Search is a read-only search and question-answering product for
local files, Gmail, Google Drive, and GitHub. It combines keyword and vector
search, then returns grounded answers with links back to the original sources.

The repository is being built incrementally from the specifications in
[`universal_ai_search_documentation/`](universal_ai_search_documentation/00_README.md).
Only one numbered document is implemented at a time. A stage is complete only
after its implementation, tests, and documentation all pass review.

## Current status

- Completed stages: `00_README.md` through `04_DATABASE_SCHEMA.md`
- Next stage: `05_API_SPECIFICATION.md`
- Product status: tested project foundation with runnable web, API, worker, and
  desktop application shells; search and database behavior are fully specified
  but product features are not implemented yet
- Safety boundary: version 1 is read-only; retrieved content is untrusted data
  and cannot trigger actions

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

## Documentation-driven workflow

The numbered documents are the source of truth and are implemented in order.
For each stage:

1. Read the stage and its referenced dependencies.
2. Turn its requirements into concrete acceptance criteria.
3. Implement only that stage's approved scope.
4. Run its automated tests, linters, and relevant manual checks.
5. Update documentation and the master task list.
6. Review the diff, commit the completed stage, and push it before continuing.

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

Run the containerized backend and desktop Rust checks with:

```sh
./scripts/test-backend.sh
./scripts/test-desktop-rust.sh
```

## Docker foundation

Docker is part of the foundation rather than a final packaging step. Docker
Compose runs PostgreSQL with pgvector, Redis, MinIO, Mailpit, the FastAPI
service, and the production Next.js build. Separate test targets provide a
repeatable Python quality suite and Tauri Rust compile check. The local topology
is specified in
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
