#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
specification="$repository_root/universal_ai_search_documentation/01_PROJECT_SPEC.md"

if [ ! -f "$specification" ]; then
  printf '%s\n' 'ERROR: 01_PROJECT_SPEC.md is missing.' >&2
  exit 1
fi

failures=0

for heading in \
  '## Product principles' \
  '## Functional requirements and acceptance criteria' \
  '## Cross-cutting behavior' \
  '## Non-goals for MVP' \
  '## Success metrics' \
  '## MVP limits and required behavior' \
  '## Core screens and traceability' \
  '## Requirement ownership in later documents' \
  '## Stage 01 completion criteria'
do
  if ! grep -Fqx "$heading" "$specification"; then
    printf 'ERROR: project specification is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for requirement in \
  AUTH-001 \
  DESKTOP-001 \
  GOOGLE-001 \
  GITHUB-001 \
  SYNC-001 \
  SEARCH-001 \
  ANSWER-001 \
  CITATION-001 \
  CONNECTION-001 \
  ACCOUNT-001 \
  SAFETY-001
do
  definition_count=$(grep -Ec "^\\| \`$requirement\` \\|" "$specification" || true)
  reference_count=$(grep -Foc "\`$requirement\`" "$specification" || true)

  if [ "$definition_count" -ne 1 ]; then
    printf 'ERROR: requirement %s must have exactly one definition; found %s.\n' "$requirement" "$definition_count" >&2
    failures=$((failures + 1))
  fi

  if [ "$reference_count" -lt 2 ]; then
    printf 'ERROR: requirement %s is not traced beyond its definition.\n' "$requirement" >&2
    failures=$((failures + 1))
  fi
done

for threshold in \
  '>= 0.85' \
  '>= 0.95' \
  '<= 0.03' \
  '<= 6 seconds' \
  '>= 0.98' \
  '>= 0.995' \
  '<= 24 hours'
do
  if ! grep -Fq "\`$threshold\`" "$specification"; then
    printf 'ERROR: project specification is missing release threshold: %s\n' "$threshold" >&2
    failures=$((failures + 1))
  fi
done

for owner_document in \
  02_SYSTEM_ARCHITECTURE.md \
  03_SEARCH_ENGINE_DESIGN.md \
  04_DATABASE_SCHEMA.md \
  05_API_SPECIFICATION.md \
  08_SECURITY_AND_PRIVACY.md \
  09_AUTH_AND_OAUTH.md \
  10_DESKTOP_AGENT.md \
  12_TESTING_AND_EVALUATION.md \
  14_PRODUCT_ROADMAP.md \
  16_COST_MODEL.md
do
  if [ ! -f "$repository_root/universal_ai_search_documentation/$owner_document" ]; then
    printf 'ERROR: requirement owner document is missing: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi

  if ! grep -Fq "]($owner_document)" "$specification"; then
    printf 'ERROR: project specification does not link to owner: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi
done

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$specification"; then
  printf '%s\n' 'ERROR: project specification contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Project specification validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Project specification validation passed: requirements are defined, traced, and measurable.'
