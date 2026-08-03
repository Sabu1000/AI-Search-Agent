# Threat Model

## Assets
- OAuth refresh tokens
- user documents and email
- source code
- embeddings
- account sessions
- audit records
- encryption keys

## Trust boundaries
- browser to API
- desktop app to API
- API to provider
- API to model provider
- worker to database and storage
- tenant to tenant

## Major threats
- cross-tenant retrieval
- stolen OAuth credentials
- malicious desktop client
- prompt injection in indexed content
- webhook forgery
- replayed sync requests
- path traversal or parser exploit
- secret ingestion from repositories
- insecure account deletion
- excessive provider scopes
- supply-chain compromise

## Required mitigations
- database RLS
- signed and expiring desktop requests
- webhook signature verification
- parser sandboxing where practical
- strict MIME and extension checks
- file-size limits
- read-only provider scopes
- encrypted credentials
- model context isolation
- no tool execution from content
- dependency scanning
- regular restore and deletion tests

## Security testing
- tenant isolation tests in CI
- OAuth state and replay tests
- webhook replay tests
- malicious PDF and archive corpus
- prompt-injection benchmark
- authorization fuzzing
- desktop protocol tampering tests
- annual external penetration test before enterprise launch
