# Universal AI Search

Universal AI Search is a read-only search and question-answering product for
local files, Gmail, Google Drive, and GitHub. It combines keyword and vector
search, then returns grounded answers with links back to the original sources.

The repository is being built incrementally from the specifications in
[`universal_ai_search_documentation/`](universal_ai_search_documentation/00_README.md).
Only one numbered document is implemented at a time. A stage is complete only
after its implementation, tests, and documentation all pass review.

## Current status

- Current stage: `00_README.md` — repository and documentation bootstrap
- Product status: pre-implementation
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
6. Review the diff, then commit only after explicit approval.

Run the documentation gate with:

```sh
./scripts/validate-docs.sh
```

## Docker plan

Docker starts in the project-foundation milestone, not at the end of the build.
The foundation stage will add Docker Compose for PostgreSQL with pgvector,
Redis, MinIO, and a test mail server, plus repeatable application builds. The
local topology is specified in
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
