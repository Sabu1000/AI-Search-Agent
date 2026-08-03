#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
architecture="$repository_root/universal_ai_search_documentation/02_SYSTEM_ARCHITECTURE.md"
project_specification="$repository_root/universal_ai_search_documentation/01_PROJECT_SPEC.md"
decision="$repository_root/universal_ai_search_documentation/architecture/decisions/0001-modular-monolith.md"

if [ ! -f "$architecture" ]; then
  printf '%s\n' 'ERROR: 02_SYSTEM_ARCHITECTURE.md is missing.' >&2
  exit 1
fi

failures=0

for heading in \
  '## Architecture principles' \
  '## System context' \
  '## Technology baseline' \
  '## Repository and module boundaries' \
  '## Component responsibilities' \
  '## Data ownership' \
  '## Request path' \
  '## Ingestion path' \
  '## Deletion path' \
  '## Tenant isolation and trust boundaries' \
  '## Synchronous and asynchronous boundaries' \
  '## Deployment units' \
  '## Docker placement' \
  '## Failure and degradation behavior' \
  '## Availability, recovery, and observability targets' \
  '## Microservice extraction criteria' \
  '## Product requirement coverage' \
  '## Stage 02 completion criteria'
do
  if ! grep -Fqx "$heading" "$architecture"; then
    printf 'ERROR: system architecture is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for deployment_unit in web api worker-sync worker-index worker-delete desktop
do
  definition_count=$(grep -Ec "^\\| \`$deployment_unit\` \\|" "$architecture" || true)

  if [ "$definition_count" -ne 1 ]; then
    printf 'ERROR: deployment unit %s must have exactly one definition; found %s.\n' "$deployment_unit" "$definition_count" >&2
    failures=$((failures + 1))
  fi
done

if [ ! -f "$project_specification" ]; then
  printf '%s\n' 'ERROR: 01_PROJECT_SPEC.md is missing; requirement coverage cannot be verified.' >&2
  failures=$((failures + 1))
else
  specification_requirements=$(sed -n 's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' "$project_specification" | sort -u)
  architecture_requirements=$(sed -n 's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' "$architecture" | sort -u)

  if [ "$specification_requirements" != "$architecture_requirements" ]; then
    printf '%s\n' 'ERROR: architecture requirement mappings do not exactly match the project specification.' >&2
    printf 'Project specification IDs:\n%s\n' "$specification_requirements" >&2
    printf 'Architecture mapping IDs:\n%s\n' "$architecture_requirements" >&2
    failures=$((failures + 1))
  fi

  for requirement in $specification_requirements
  do
    coverage_count=$(grep -Ec "^\\| \`$requirement\` \\|" "$architecture" || true)

    if [ "$coverage_count" -ne 1 ]; then
      printf 'ERROR: requirement %s must have exactly one architecture mapping; found %s.\n' "$requirement" "$coverage_count" >&2
      failures=$((failures + 1))
    fi
  done
fi

for durable_term in \
  'PostgreSQL is authoritative' \
  'transactional outbox' \
  'Queue messages contain identifiers'
do
  if ! grep -Fq "$durable_term" "$architecture"; then
    printf 'ERROR: architecture is missing durability rule: %s\n' "$durable_term" >&2
    failures=$((failures + 1))
  fi
done

for owner_document in \
  04_DATABASE_SCHEMA.md \
  11_DEPLOYMENT_AND_INFRASTRUCTURE.md \
  19_LOCAL_DEVELOPMENT.md
do
  if [ ! -f "$repository_root/universal_ai_search_documentation/$owner_document" ]; then
    printf 'ERROR: architecture owner document is missing: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi

  if ! grep -Fq "]($owner_document)" "$architecture"; then
    printf 'ERROR: system architecture does not link to owner: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi
done

if [ ! -f "$decision" ]; then
  printf '%s\n' 'ERROR: modular-monolith architecture decision record is missing.' >&2
  failures=$((failures + 1))
else
  for decision_marker in \
    '- Status: Accepted' \
    '## Context' \
    '## Decision' \
    '## Consequences' \
    '## Extraction triggers' \
    '## Rejected alternatives'
  do
    if ! grep -Fqx -- "$decision_marker" "$decision"; then
      printf 'ERROR: architecture decision is missing marker: %s\n' "$decision_marker" >&2
      failures=$((failures + 1))
    fi
  done
fi

if ! grep -Fq '](architecture/decisions/0001-modular-monolith.md)' "$architecture"; then
  printf '%s\n' 'ERROR: system architecture does not link to the accepted decision record.' >&2
  failures=$((failures + 1))
fi

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$architecture" "$decision"; then
  printf '%s\n' 'ERROR: architecture documentation contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'System architecture validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'System architecture validation passed: boundaries, units, durability, and requirement coverage are defined.'
