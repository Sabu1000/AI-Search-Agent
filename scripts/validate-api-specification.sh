#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
specification="$repository_root/universal_ai_search_documentation/05_API_SPECIFICATION.md"
project_specification="$repository_root/universal_ai_search_documentation/01_PROJECT_SPEC.md"
database_schema="$repository_root/universal_ai_search_documentation/04_DATABASE_SCHEMA.md"

if [ ! -f "$specification" ]; then
  printf '%s\n' 'ERROR: 05_API_SPECIFICATION.md is missing.' >&2
  exit 1
fi

failures=0

for heading in \
  '## Goals and ownership' \
  '## Protocol and base conventions' \
  '## Versioning and generated contracts' \
  '## Authentication modes' \
  '## Workspace and authorization context' \
  '## Standard request and response headers' \
  '## Validation and serialization' \
  '## Idempotency and optimistic concurrency' \
  '## Cursor pagination, filtering, and sorting' \
  '## Rate limits and quotas' \
  '## HTTP status and error contract' \
  '## Endpoint catalog' \
  '## Authentication contracts' \
  '## Connection contracts' \
  '## Asynchronous operation contract' \
  '## Search contracts' \
  '## Conversation and SSE contracts' \
  '## Source and citation contracts' \
  '## Desktop synchronization contracts' \
  '## Export and account-deletion contracts' \
  '## Webhook and provider-callback safety' \
  '## CORS, caching, and transport security' \
  '## Observability and privacy' \
  '## Contract and security testing' \
  '## Requirement coverage' \
  '## Stage 05 completion criteria'
do
  if ! grep -Fqx "$heading" "$specification"; then
    printf 'ERROR: API specification is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for endpoint in \
  'POST /v1/auth/register' \
  'POST /v1/auth/email/verify' \
  'POST /v1/auth/email/resend' \
  'POST /v1/auth/login' \
  'POST /v1/auth/refresh' \
  'POST /v1/auth/logout' \
  'GET /v1/auth/me' \
  'POST /v1/auth/reauthenticate' \
  'POST /v1/auth/password/request-reset' \
  'POST /v1/auth/password/reset' \
  'GET /v1/connections' \
  'GET /v1/connections/{connection_id}' \
  'GET /v1/connections/{connection_id}/status' \
  'POST /v1/connections/google/authorize' \
  'GET /v1/connections/google/callback' \
  'POST /v1/connections/github/authorize' \
  'GET /v1/connections/github/callback' \
  'PUT /v1/connections/{connection_id}/selections' \
  'POST /v1/connections/{connection_id}/sync' \
  'DELETE /v1/connections/{connection_id}' \
  'POST /v1/search' \
  'POST /v1/search/suggestions' \
  'GET /v1/search/history' \
  'DELETE /v1/search/history' \
  'POST /v1/conversations' \
  'GET /v1/conversations' \
  'GET /v1/conversations/{conversation_id}' \
  'DELETE /v1/conversations/{conversation_id}' \
  'POST /v1/conversations/{conversation_id}/messages:stream' \
  'GET /v1/sources' \
  'GET /v1/sources/{source_id}' \
  'DELETE /v1/sources/{source_id}' \
  'POST /v1/sources/{source_id}/reindex' \
  'GET /v1/operations/{operation_id}' \
  'POST /v1/operations/{operation_id}:retry' \
  'GET /v1/usage' \
  'POST /v1/account/export' \
  'DELETE /v1/account' \
  'GET /v1/account/deletion-status' \
  'POST /v1/devices/registration-challenges' \
  'POST /v1/devices/register' \
  'GET /v1/devices' \
  'GET /v1/devices/{device_id}' \
  'POST /v1/devices/{device_id}/heartbeat' \
  'POST /v1/devices/{device_id}/manifests' \
  'POST /v1/devices/{device_id}/uploads:sign' \
  'POST /v1/devices/{device_id}/changes' \
  'DELETE /v1/devices/{device_id}' \
  'POST /v1/webhooks/github'
do
  method=${endpoint%% *}
  path=${endpoint#* }
  catalog_count=$(grep -Foc "| \`$method\` | \`$path\` |" "$specification" || true)

  if [ "$catalog_count" -ne 1 ]; then
    printf 'ERROR: endpoint %s %s must have exactly one catalog row; found %s.\n' "$method" "$path" "$catalog_count" >&2
    failures=$((failures + 1))
  fi
done

for auth_control in \
  '__Host-uas_access' \
  '__Host-uas_refresh' \
  '__Host-uas_csrf' \
  'Authorization: Bearer' \
  'Authorization: Device' \
  'Authorization: DeletionReceipt' \
  'X-CSRF-Token' \
  'X-Device-Signature' \
  'Content-Digest' \
  'X-Workspace-ID' \
  'authorization version'
do
  if ! grep -Fqi "$auth_control" "$specification"; then
    printf 'ERROR: API specification is missing authentication control: %s\n' "$auth_control" >&2
    failures=$((failures + 1))
  fi
done

for header in \
  Idempotency-Key \
  If-Match \
  ETag \
  X-Reauth-Token \
  X-Confirm-Action \
  X-Request-ID \
  UAS-Client-Version \
  Retry-After \
  RateLimit-Limit \
  RateLimit-Remaining \
  RateLimit-Reset
do
  if ! grep -Fq "\`$header\`" "$specification"; then
    printf 'ERROR: API specification is missing header contract: %s\n' "$header" >&2
    failures=$((failures + 1))
  fi
done

for pagination_rule in \
  'keyset cursors' \
  'limit` defaults to 25' \
  '1–100' \
  'CURSOR_CONTEXT_MISMATCH' \
  'CURSOR_INVALID'
do
  if ! grep -Fq "$pagination_rule" "$specification"; then
    printf 'ERROR: API specification is missing pagination behavior: %s\n' "$pagination_rule" >&2
    failures=$((failures + 1))
  fi
done

for error_status in 400 401 403 404 409 412 413 422 426 429 500 502 503 504
do
  status_count=$(grep -Ec "^\| $error_status \|" "$specification" || true)

  if [ "$status_count" -ne 1 ]; then
    printf 'ERROR: HTTP error status %s must have exactly one definition; found %s.\n' "$error_status" "$status_count" >&2
    failures=$((failures + 1))
  fi
done

for event in \
  message.started \
  retrieval.completed \
  claim.completed \
  message.insufficient \
  message.completed \
  error
do
  event_count=$(grep -Ec "^\| \`$event\` \|" "$specification" || true)

  if [ "$event_count" -ne 1 ]; then
    printf 'ERROR: SSE event %s must have exactly one definition; found %s.\n' "$event" "$event_count" >&2
    failures=$((failures + 1))
  fi
done

for provider in gmail google_drive github local_files
do
  if ! grep -Fq "\`$provider\`" "$specification"; then
    printf 'ERROR: API specification is missing canonical provider: %s\n' "$provider" >&2
    failures=$((failures + 1))
  fi
done

for contract in \
  'application/problem+json' \
  'OpenAPI 3.1' \
  '30 accepted application' \
  '25,000 indexed' \
  '10 GB' \
  '100 MB' \
  'same key and same request returns the original' \
  'X-Confirm-Action: delete-account' \
  'DELETE MY ACCOUNT' \
  'at least every 15 seconds' \
  'X-Accel-Buffering: no' \
  'absolute local path.'
do
  if ! grep -Fq "$contract" "$specification"; then
    printf 'ERROR: API specification is missing required contract: %s\n' "$contract" >&2
    failures=$((failures + 1))
  fi
done

if [ ! -f "$project_specification" ]; then
  printf '%s\n' 'ERROR: 01_PROJECT_SPEC.md is missing; requirement coverage cannot be verified.' >&2
  failures=$((failures + 1))
else
  project_requirements=$(sed -n 's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' "$project_specification" | sort -u)
  api_requirements=$(sed -n 's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' "$specification" | sort -u)

  if [ "$project_requirements" != "$api_requirements" ]; then
    printf '%s\n' 'ERROR: API requirement mappings do not exactly match the project specification.' >&2
    failures=$((failures + 1))
  fi

  for requirement in $project_requirements
  do
    mapping_count=$(grep -Ec "^\| \`$requirement\` \|" "$specification" || true)

    if [ "$mapping_count" -ne 1 ]; then
      printf 'ERROR: requirement %s must have exactly one API mapping; found %s.\n' "$requirement" "$mapping_count" >&2
      failures=$((failures + 1))
    fi
  done
fi

for owner_document in \
  03_SEARCH_ENGINE_DESIGN.md \
  04_DATABASE_SCHEMA.md \
  06_CONNECTOR_FRAMEWORK.md \
  09_AUTH_AND_OAUTH.md \
  10_DESKTOP_AGENT.md
do
  if [ ! -f "$repository_root/universal_ai_search_documentation/$owner_document" ]; then
    printf 'ERROR: API owner document is missing: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi

  if ! grep -Fq "]($owner_document)" "$specification"; then
    printf 'ERROR: API specification does not link to owner: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi
done

if [ ! -f "$database_schema" ] || ! grep -Fq '### api_idempotency_keys' "$database_schema"; then
  printf '%s\n' 'ERROR: database schema does not support durable API idempotency.' >&2
  failures=$((failures + 1))
fi

if [ ! -f "$database_schema" ] || ! grep -Fq '`receipt_token_hash`' "$database_schema"; then
  printf '%s\n' 'ERROR: database schema does not support hashed deletion receipts.' >&2
  failures=$((failures + 1))
fi

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$specification"; then
  printf '%s\n' 'ERROR: API specification contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'API specification validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'API specification validation passed: endpoints, auth, idempotency, pagination, errors, streaming, and requirement coverage are defined.'
