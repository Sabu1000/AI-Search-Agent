# ADR 0001: Modular Backend Monolith with Separate Workers

- Status: Accepted
- Date: 2026-08-03
- Owners: Universal AI Search maintainers

## Context

The product needs an API, several background workloads, four initial connector
types, hybrid retrieval, and strict tenant isolation. The initial team is small,
the domain boundaries will change as the first vertical slice is tested, and the
operational capacity required by each workload is not yet measured.

A single web process would couple latency-sensitive requests to slow provider,
parser, embedding, and deletion work. Starting with independent microservices,
however, would add versioned network contracts, distributed transactions,
additional deployment and observability surfaces, and cross-service tenant
context before those costs are justified.

## Decision

Use one modular Python backend codebase with explicit domain interfaces and
multiple process entry points:

- `api`
- `worker-sync`
- `worker-index`
- `worker-delete`

The processes share versioned domain and infrastructure packages and use the
same immutable backend container image. PostgreSQL is the durable source of
truth. Redis transports jobs and holds ephemeral coordination state. A
transactional outbox connects durable state changes to job publication.

Next.js remains a separate web application, and Tauri remains a separately
released desktop client. Both consume versioned API contracts and never access
backend data stores directly.

Domain boundaries are enforced in code and tests even though backend modules
deploy from one artifact. No module owns another module's tables through ad hoc
queries; cross-domain work goes through application services or defined
repositories.

## Consequences

### Benefits

- Domain changes remain transactional and easy to refactor during the MVP.
- API and worker workloads scale and fail independently at the process level.
- One backend image reduces dependency and configuration drift.
- Local development and end-to-end testing require fewer distributed systems.
- Tenant context, security controls, and observability use one implementation.

### Costs

- A backend deployment updates all backend process types together.
- Poorly enforced module boundaries could become hidden coupling.
- One database is a shared capacity and availability dependency.
- Independent service ownership requires later extraction work if the team
  grows.

### Controls

- Architecture tests enforce allowed module dependencies once code exists.
- Queue payloads contain IDs rather than internal serialized domain objects.
- Worker queues and process pools are separate by workload.
- Database migrations remain backward compatible across rolling deployments.
- Performance and failure metrics are broken down by module and worker type.

## Extraction triggers

Reconsider this decision when measured load requires a different data or
runtime boundary, repeated failures escape process isolation, a dedicated team
needs an independent release lifecycle, or compliance requires a separate trust
zone. Any extraction needs its own accepted ADR, versioned contract, data
ownership plan, migration and rollback plan, and operational readiness review.

## Rejected alternatives

### One synchronous application process

Rejected because provider sync, parsing, embeddings, and deletion are
long-running and would compromise API latency and reliability.

### Microservices from the first release

Rejected because current scale and team ownership do not justify distributed
transactions, additional network authorization, deployment coordination, and
larger operational overhead.
