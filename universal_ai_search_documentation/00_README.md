# Universal AI Search — Documentation Pack

This folder is the implementation blueprint for a deployable AI search product that lets users connect local files, Gmail, Google Drive, and GitHub, then search and ask questions across those sources with citations.

## Implementation order

1. [`01_PROJECT_SPEC.md`](01_PROJECT_SPEC.md)
2. [`02_SYSTEM_ARCHITECTURE.md`](02_SYSTEM_ARCHITECTURE.md)
3. [`03_SEARCH_ENGINE_DESIGN.md`](03_SEARCH_ENGINE_DESIGN.md)
4. [`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md)
5. [`05_API_SPECIFICATION.md`](05_API_SPECIFICATION.md)
6. [`06_CONNECTOR_FRAMEWORK.md`](06_CONNECTOR_FRAMEWORK.md)
7. [`07_INDEXING_PIPELINE.md`](07_INDEXING_PIPELINE.md)
8. [`08_SECURITY_AND_PRIVACY.md`](08_SECURITY_AND_PRIVACY.md)
9. [`09_AUTH_AND_OAUTH.md`](09_AUTH_AND_OAUTH.md)
10. [`10_DESKTOP_AGENT.md`](10_DESKTOP_AGENT.md)
11. [`11_DEPLOYMENT_AND_INFRASTRUCTURE.md`](11_DEPLOYMENT_AND_INFRASTRUCTURE.md)
12. [`12_TESTING_AND_EVALUATION.md`](12_TESTING_AND_EVALUATION.md)
13. [`13_OBSERVABILITY_AND_OPERATIONS.md`](13_OBSERVABILITY_AND_OPERATIONS.md)
14. [`14_PRODUCT_ROADMAP.md`](14_PRODUCT_ROADMAP.md)
15. [`15_IMPLEMENTATION_PLAN.md`](15_IMPLEMENTATION_PLAN.md)
16. [`16_COST_MODEL.md`](16_COST_MODEL.md)
17. [`17_INCIDENT_RUNBOOKS.md`](17_INCIDENT_RUNBOOKS.md)
18. [`18_CODING_STANDARDS.md`](18_CODING_STANDARDS.md)
19. [`19_LOCAL_DEVELOPMENT.md`](19_LOCAL_DEVELOPMENT.md)
20. [`20_THREAT_MODEL.md`](20_THREAT_MODEL.md)

## MVP scope

- User accounts
- Web application
- Tauri desktop application
- Local folder indexing
- Gmail read-only connector
- Google Drive read-only connector
- GitHub App connector
- Hybrid search
- AI answers with clickable citations
- Data deletion and connector revocation
- Production deployment and monitoring

## Guiding principle

The product is read-only in version 1. Retrieved content is untrusted data and cannot trigger actions.

## Stage workflow

Implement the numbered documents in order. Before moving to the next document:

1. Convert the current document into explicit acceptance criteria.
2. Implement its requirements without pulling later features forward unless they are prerequisites.
3. Add and run automated tests and relevant manual checks.
4. Update the documentation and [`MASTER-TASK-LIST.md`](MASTER-TASK-LIST.md).
5. Review the complete diff, commit and push the finished stage under the
   repository owner's current authorization, then check in before starting the
   next document.

The definition of done in [`15_IMPLEMENTATION_PLAN.md`](15_IMPLEMENTATION_PLAN.md)
applies to every product feature.

## Docker placement

Docker is foundation work. Phase 1 tasks `P1-015` through `P1-018` establish
Docker Compose and local services, and `P1-029` verifies container builds. Use
[`19_LOCAL_DEVELOPMENT.md`](19_LOCAL_DEVELOPMENT.md) for the local environment
and [`11_DEPLOYMENT_AND_INFRASTRUCTURE.md`](11_DEPLOYMENT_AND_INFRASTRUCTURE.md)
for production images, scanning, and deployment.

## Stage 00 completion criteria

- A root `README.md` explains the product, status, workflow, and Docker plan.
- Every numbered document from `00` through `20` exists.
- The implementation-order links resolve to local files.
- `./scripts/validate-docs.sh` passes.
