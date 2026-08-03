#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
schema="$repository_root/universal_ai_search_documentation/04_DATABASE_SCHEMA.md"
project_specification="$repository_root/universal_ai_search_documentation/01_PROJECT_SPEC.md"
postgres_init="$repository_root/infrastructure/docker/postgres-init/001-extensions.sql"

if [ ! -f "$schema" ]; then
  printf '%s\n' 'ERROR: 04_DATABASE_SCHEMA.md is missing.' >&2
  exit 1
fi

failures=0

for heading in \
  '## Goals and ownership' \
  '## Extensions and database baseline' \
  '## Naming and type conventions' \
  '## Core relational invariants' \
  '## Relationship overview' \
  '## Identity and workspace tables' \
  '## Connection and desktop tables' \
  '## Source and index tables' \
  '## Search and conversation tables' \
  '## Job, deletion, quota, and audit tables' \
  '## Atomic lifecycle transactions' \
  '## Foreign-key deletion behavior' \
  '## Row-level security and database roles' \
  '## Required indexes' \
  '## Quotas, retention, and data minimization' \
  '## Migration and rollback strategy' \
  '## Database test matrix' \
  '## Requirement coverage' \
  '## Stage 04 completion criteria'
do
  if ! grep -Fqx "$heading" "$schema"; then
    printf 'ERROR: database schema is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for table in \
  users \
  auth_identities \
  one_time_tokens \
  sessions \
  workspaces \
  workspace_members \
  oauth_transactions \
  connections \
  connection_scopes \
  connection_cursors \
  source_collections \
  devices \
  device_folders \
  provider_events \
  sources \
  source_people \
  source_collection_memberships \
  document_versions \
  chunks \
  embedding_profiles \
  chunk_embeddings \
  search_requests \
  conversations \
  messages \
  message_claims \
  citations \
  jobs \
  job_attempts \
  outbox_events \
  deletion_requests \
  workspace_usage \
  audit_events
do
  definition_count=$(grep -Ec "^### $table$" "$schema" || true)

  if [ "$definition_count" -ne 1 ]; then
    printf 'ERROR: table %s must have exactly one definition; found %s.\n' "$table" "$definition_count" >&2
    failures=$((failures + 1))
  fi
done

rls_section=$(sed -n '/^## Row-level security and database roles$/,/^## Required indexes$/p' "$schema")

for tenant_table in \
  workspace_members \
  oauth_transactions \
  connections \
  connection_scopes \
  connection_cursors \
  source_collections \
  devices \
  device_folders \
  provider_events \
  sources \
  source_people \
  source_collection_memberships \
  document_versions \
  chunks \
  chunk_embeddings \
  search_requests \
  conversations \
  messages \
  message_claims \
  citations \
  jobs \
  job_attempts \
  outbox_events \
  deletion_requests \
  workspace_usage
do
  if ! awk -v heading="### $tenant_table" '
    $0 == heading { inside = 1; next }
    /^### / && inside { exit }
    inside && /`workspace_id`/ { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$schema"; then
    printf 'ERROR: tenant table %s does not define workspace_id directly.\n' "$tenant_table" >&2
    failures=$((failures + 1))
  fi

  if ! printf '%s\n' "$rls_section" | grep -Fq "\`$tenant_table\`"; then
    printf 'ERROR: tenant table %s is missing from the forced-RLS catalog.\n' "$tenant_table" >&2
    failures=$((failures + 1))
  fi
done

for invariant in \
  'Every tenant-owned table contains `workspace_id` directly' \
  'composite foreign keys that reject cross-workspace relationships' \
  'sources.current_document_version_id' \
  'chunk content in place is forbidden.' \
  'Provider cursors advance in the same transaction' \
  'Disconnect and deletion state excludes content' \
  'FORCE ROW LEVEL SECURITY' \
  'USING` and `WITH CHECK' \
  'NOBYPASSRLS'
do
  if ! grep -Fq "$invariant" "$schema"; then
    printf 'ERROR: database schema is missing invariant: %s\n' "$invariant" >&2
    failures=$((failures + 1))
  fi
done

for transaction in \
  '### Searchable-version promotion' \
  '### Sync cursor advancement' \
  '### Session rotation' \
  '### Disconnect and deletion'
do
  if ! grep -Fqx "$transaction" "$schema"; then
    printf 'ERROR: database schema is missing transaction: %s\n' "$transaction" >&2
    failures=$((failures + 1))
  fi
done

for database_feature in \
  'VECTOR(1536)' \
  'TSVECTOR generated and stored' \
  'vector_cosine_ops' \
  'DEFERRABLE INITIALLY DEFERRED' \
  'SECURITY DEFINER' \
  'CREATE INDEX CONCURRENTLY' \
  'FOR UPDATE SKIP LOCKED' \
  'expand/contract' \
  'lock_timeout' \
  'statement_timeout'
do
  if ! grep -Fq "$database_feature" "$schema"; then
    printf 'ERROR: database schema is missing database behavior: %s\n' "$database_feature" >&2
    failures=$((failures + 1))
  fi
done

if [ ! -f "$project_specification" ]; then
  printf '%s\n' 'ERROR: 01_PROJECT_SPEC.md is missing; requirement coverage cannot be verified.' >&2
  failures=$((failures + 1))
else
  specification_requirements=$(sed -n 's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' "$project_specification" | sort -u)
  schema_requirements=$(sed -n 's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' "$schema" | sort -u)

  if [ "$specification_requirements" != "$schema_requirements" ]; then
    printf '%s\n' 'ERROR: database requirement mappings do not exactly match the project specification.' >&2
    printf 'Project specification IDs:\n%s\n' "$specification_requirements" >&2
    printf 'Database mapping IDs:\n%s\n' "$schema_requirements" >&2
    failures=$((failures + 1))
  fi

  for requirement in $specification_requirements
  do
    mapping_count=$(grep -Ec "^\| \`$requirement\` \|" "$schema" || true)

    if [ "$mapping_count" -ne 1 ]; then
      printf 'ERROR: requirement %s must have exactly one database mapping; found %s.\n' "$requirement" "$mapping_count" >&2
      failures=$((failures + 1))
    fi
  done
fi

for owner_document in \
  05_API_SPECIFICATION.md \
  06_CONNECTOR_FRAMEWORK.md \
  07_INDEXING_PIPELINE.md \
  08_SECURITY_AND_PRIVACY.md
do
  if [ ! -f "$repository_root/universal_ai_search_documentation/$owner_document" ]; then
    printf 'ERROR: database owner document is missing: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi

  if ! grep -Fq "]($owner_document)" "$schema"; then
    printf 'ERROR: database schema does not link to owner: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi
done

if [ ! -f "$postgres_init" ]; then
  printf '%s\n' 'ERROR: PostgreSQL extension initialization is missing.' >&2
  failures=$((failures + 1))
else
  for extension in citext pg_trgm vector
  do
    if ! grep -Eq "CREATE EXTENSION IF NOT EXISTS \"?$extension\"?" "$postgres_init"; then
      printf 'ERROR: local PostgreSQL does not initialize extension: %s\n' "$extension" >&2
      failures=$((failures + 1))
    fi
  done
fi

if grep -Fq '<MODEL_DIMENSION>' "$schema"; then
  printf '%s\n' 'ERROR: database schema still contains an unresolved embedding dimension.' >&2
  failures=$((failures + 1))
fi

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$schema"; then
  printf '%s\n' 'ERROR: database schema contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Database schema validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Database schema validation passed: tables, tenant keys, lifecycles, indexes, migrations, and requirement coverage are defined.'
