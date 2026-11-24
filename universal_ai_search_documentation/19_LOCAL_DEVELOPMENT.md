# Local Development

## Prerequisites
- Docker Desktop
- Python 3.12
- Node.js 22 LTS
- pnpm
- Rust stable
- Tauri prerequisites

## Local services
Use Docker Compose for:
- PostgreSQL with pgvector
- Redis
- MinIO
- Mail test server

## Suggested repository
```text
apps/
  web/
  api/
  worker/
  desktop/
packages/
  shared-types/
  ui/
  connector-sdk/
connectors/
  gmail/
  google-drive/
  github/
  desktop/
infrastructure/
  terraform/
  docker/
tests/
  evals/
docs/
```

## Environment variables
Maintain `.env.example` with placeholders only. Validate required variables on startup.

## Fixture-only verification

Use a disposable local environment. Do not point the tests at a real mail
server, existing account database, GitHub App, or personal folder.

```sh
pnpm install --frozen-lockfile
pnpm check
pnpm build
./scripts/test-backend.sh
./scripts/test-database.sh
./scripts/test-connector-sdk.sh
./scripts/test-desktop-rust.sh
```

The database script owns its disposable Compose project and removes that
project's volumes when it finishes. The automated GitHub, mail, and desktop
tests use synthetic identities and fixtures. The synthetic
[relevance evaluation](../apps/api/tests_database/fixtures/SEARCH_EVALUATION.md)
is not a live-provider certification.

For a local account smoke test, start the default development stack using
`./scripts/dev-up.sh`, inspect API readiness at
`http://localhost:8000/health/ready`, then run
`python3 scripts/smoke-auth.py`. It registers a synthetic address against
local Mailpit at port 8025 and does not print credentials. Keep development
ports private. The web app is on port 3000 by default.
Do not use the example secrets or test SMTP configuration outside a trusted
local/test environment.

The GitHub App, native device registration, and password recovery flags
default off. Enable only the slice being tested and use disposable keys,
provider installations, and accounts. No flag alone enables desktop file
upload. A live smoke must be reported separately from fixture checks.

## Target desktop vertical slice

This is a target, not a currently runnable path.

1. Start local services.
2. Run migrations.
3. Create account.
4. Upload a Markdown file.
5. Index it.
6. Search it.
7. Ask a question.
8. Display a citation linking to the uploaded source.

This vertical slice must work before adding OAuth connectors.
