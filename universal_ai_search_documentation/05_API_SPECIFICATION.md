# API Specification

## Conventions
- Base path: `/v1`
- JSON request and response bodies
- Cursor pagination
- Idempotency keys for write endpoints
- Problem Details style errors
- SSE for streaming chat

## Authentication
- Short-lived access token in secure HTTP-only cookie or Authorization header
- Rotating refresh token stored as a hash server-side
- CSRF protection for cookie-authenticated writes

## Core endpoints

### Authentication
- `POST /v1/auth/register`
- `POST /v1/auth/login`
- `POST /v1/auth/refresh`
- `POST /v1/auth/logout`
- `GET /v1/auth/me`
- `POST /v1/auth/password/request-reset`
- `POST /v1/auth/password/reset`

### Connections
- `GET /v1/connections`
- `POST /v1/connections/google/authorize`
- `GET /v1/connections/google/callback`
- `POST /v1/connections/github/authorize`
- `GET /v1/connections/github/callback`
- `POST /v1/connections/{id}/sync`
- `DELETE /v1/connections/{id}`
- `GET /v1/connections/{id}/status`

### Search
- `POST /v1/search`
- `POST /v1/search/suggestions`
- `GET /v1/search/history`
- `DELETE /v1/search/history`

### Chat
- `POST /v1/conversations`
- `GET /v1/conversations`
- `GET /v1/conversations/{id}`
- `DELETE /v1/conversations/{id}`
- `POST /v1/conversations/{id}/messages:stream`

### Sources
- `GET /v1/sources`
- `GET /v1/sources/{id}`
- `DELETE /v1/sources/{id}`
- `POST /v1/sources/{id}/reindex`

### Desktop
- `POST /v1/devices/register`
- `POST /v1/devices/{id}/heartbeat`
- `POST /v1/devices/{id}/manifests`
- `POST /v1/devices/{id}/uploads:sign`
- `POST /v1/devices/{id}/changes`
- `DELETE /v1/devices/{id}`

### Privacy
- `POST /v1/account/export`
- `DELETE /v1/account`
- `GET /v1/account/deletion-status`

## Search request
```json
{
  "query": "What did Maya decide about payment retries?",
  "providers": ["gmail", "github"],
  "filters": {
    "date_from": "2026-01-01T00:00:00Z",
    "repository": null,
    "file_types": []
  },
  "limit": 20
}
```

## Search response
```json
{
  "request_id": "uuid",
  "answer": "...",
  "results": [{"source_id":"uuid","title":"...","provider":"gmail","url":"...","snippet":"...","score":0.91}],
  "citations": [{"claim_index":0,"source_id":"uuid","chunk_id":"uuid"}],
  "latency_ms": 1820
}
```

## Error shape
```json
{
  "type": "https://docs.example.com/errors/connection-expired",
  "title": "Connection expired",
  "status": 409,
  "code": "CONNECTION_EXPIRED",
  "detail": "Reconnect Google to continue syncing.",
  "request_id": "uuid"
}
```
