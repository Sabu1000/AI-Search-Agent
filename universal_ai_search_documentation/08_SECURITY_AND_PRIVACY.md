# Security and Privacy

## Security posture
The product processes private email, files, and source code. Security is a launch requirement, not a later enhancement.

## Required controls
- TLS everywhere
- Encryption at rest
- KMS-backed envelope encryption for OAuth credentials
- Argon2id password hashing
- Short-lived access tokens
- Rotating refresh tokens
- Secure HTTP-only cookies
- CSRF protection
- Strict CORS allowlist
- Content Security Policy
- Rate limiting
- Audit logging
- Dependency and container scanning
- Secret scanning
- Database backups and restore tests

## Authorization
- Every object belongs to a workspace
- API resolves workspace membership before access
- Database RLS enforces workspace isolation
- Search filters by workspace before vector lookup
- Connector scopes are read-only and least privilege

## Data retention
- OAuth credentials deleted immediately on disconnect
- Indexed content deleted through a tracked deletion job
- Soft delete may be used only during a short recovery window
- User-facing deletion status must be visible
- Backups expire according to a documented retention period

## Privacy controls
- Source preview before local-folder indexing
- Per-folder and per-repository selection
- Exclusion patterns
- One-click connector deletion
- Full account export
- Full account deletion

## Prompt-injection defense
- Retrieved content is marked untrusted
- No tool execution from retrieved instructions
- No outbound URL fetches based on retrieved text
- Read-only product behavior
- Structured model outputs validated server-side
- Citation IDs must match supplied context

## Logging policy
Never log:
- OAuth tokens
- passwords
- full document bodies
- email bodies
- model prompts containing private context

Redact sensitive metadata from errors and traces.
