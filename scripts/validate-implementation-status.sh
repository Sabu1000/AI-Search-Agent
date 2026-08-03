#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
status_file="$repository_root/universal_ai_search_documentation/IMPLEMENTATION_STATUS.md"
failures=0

if [ ! -f "$status_file" ]; then
  printf '%s\n' 'ERROR: IMPLEMENTATION_STATUS.md is missing.' >&2
  exit 1
fi

for heading in \
  '## Status vocabulary' \
  '## Numbered-document audit' \
  '## MVP requirement audit' \
  '## Implemented code inventory' \
  '## Explicitly unimplemented inventory' \
  '## Backfill sequence' \
  '## Evidence commands'
do
  if ! grep -Fqx "$heading" "$status_file"; then
    printf 'ERROR: implementation status is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for stage in 00_README 01_PROJECT_SPEC 02_SYSTEM_ARCHITECTURE 03_SEARCH_ENGINE_DESIGN 04_DATABASE_SCHEMA 05_API_SPECIFICATION 06_CONNECTOR_FRAMEWORK
do
  if ! grep -Fq "| \`$stage.md\` |" "$status_file"; then
    printf 'ERROR: implementation status is missing audited stage: %s\n' "$stage" >&2
    failures=$((failures + 1))
  fi
done

project_specification="$repository_root/universal_ai_search_documentation/01_PROJECT_SPEC.md"
project_requirements=$(sed -n \
  's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' \
  "$project_specification" | sort -u)
status_requirements=$(sed -n \
  's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' \
  "$status_file" | sort -u)

if [ "$project_requirements" != "$status_requirements" ]; then
  printf '%s\n' 'ERROR: implementation status must audit every MVP requirement exactly once.' >&2
  failures=$((failures + 1))
fi

for requirement in $project_requirements
do
  mapping_count=$(grep -Foc "| \`$requirement\` |" "$status_file" || true)
  if [ "$mapping_count" -ne 1 ]; then
    printf 'ERROR: requirement %s must have exactly one status row; found %s.\n' \
      "$requirement" "$mapping_count" >&2
    failures=$((failures + 1))
  fi
done

for truthful_count in \
  '0/11' \
  '0/33' \
  '49' \
  '27/30' \
  '5/11' \
  '25 tests at 99% line coverage'
do
  if ! grep -Fq "\`$truthful_count\`" "$status_file"; then
    printf 'ERROR: implementation status is missing audited count: %s\n' \
      "$truthful_count" >&2
    failures=$((failures + 1))
  fi
done

for backfill_item in B0 B1 B2 B3 B4 B5 B6 B7
do
  if ! grep -Fq "| \`$backfill_item\` |" "$status_file"; then
    printf 'ERROR: implementation status is missing backfill item: %s\n' \
      "$backfill_item" >&2
    failures=$((failures + 1))
  fi
done

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$status_file"; then
  printf '%s\n' 'ERROR: implementation status contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Implementation status validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Implementation status validation passed: stages, MVP requirements, counts, evidence, and backfill order are tracked.'
