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

## First development milestone
1. Start local services.
2. Run migrations.
3. Create account.
4. Upload a Markdown file.
5. Index it.
6. Search it.
7. Ask a question.
8. Display a citation linking to the uploaded source.

This vertical slice must work before adding OAuth connectors.
