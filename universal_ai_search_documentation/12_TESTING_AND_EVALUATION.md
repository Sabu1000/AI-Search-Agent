# Testing and Evaluation

## Test layers
- Unit tests
- Integration tests
- Connector contract tests
- End-to-end browser tests
- Desktop integration tests
- Security tests
- Load tests
- Retrieval and answer evaluations

## Required unit coverage
- Query planning
- Ranking
- Permission filters
- Chunking
- citation validation
- token encryption
- cursor handling
- deletion workflows

## Connector test fixtures
Use deterministic fake providers for:
- pagination
- rate limits
- token expiration
- malformed files
- deletions
- permission changes
- duplicate webhook events

## Retrieval benchmark
Create at least 300 labeled questions across:
- exact identifier lookup
- semantic lookup
- date/person filtering
- code search
- email search
- cross-source synthesis
- conflicting sources
- no-answer cases

## Metrics
- Recall@5 and Recall@10
- Mean reciprocal rank
- Citation precision
- Citation recall
- Unsupported claim rate
- Answer completeness
- Query latency
- Cost per query

## Release gates
No release if:
- cross-tenant test fails
- citation correctness drops below 95%
- unsupported claim rate exceeds 3%
- deletion workflow fails
- OAuth reconnect path fails
- P95 API latency regresses beyond agreed threshold
