#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
documentation_dir="$repository_root/universal_ai_search_documentation"
documentation_index="$documentation_dir/00_README.md"

failures=0

if [ ! -f "$repository_root/README.md" ]; then
  printf '%s\n' 'ERROR: README.md is missing from the repository root.' >&2
  failures=$((failures + 1))
fi

if [ ! -f "$documentation_index" ]; then
  printf '%s\n' 'ERROR: 00_README.md is missing.' >&2
  exit 1
fi

stage=0
while [ "$stage" -le 20 ]; do
  prefix=$(printf '%02d_' "$stage")
  match_count=$(find "$documentation_dir" -maxdepth 1 -type f -name "${prefix}*.md" | wc -l | tr -d ' ')

  if [ "$match_count" -ne 1 ]; then
    printf 'ERROR: expected exactly one stage %02d document; found %s.\n' "$stage" "$match_count" >&2
    failures=$((failures + 1))
  fi

  stage=$((stage + 1))
done

for document in \
  01_PROJECT_SPEC.md \
  02_SYSTEM_ARCHITECTURE.md \
  03_SEARCH_ENGINE_DESIGN.md \
  04_DATABASE_SCHEMA.md \
  05_API_SPECIFICATION.md \
  06_CONNECTOR_FRAMEWORK.md \
  07_INDEXING_PIPELINE.md \
  08_SECURITY_AND_PRIVACY.md \
  09_AUTH_AND_OAUTH.md \
  10_DESKTOP_AGENT.md \
  11_DEPLOYMENT_AND_INFRASTRUCTURE.md \
  12_TESTING_AND_EVALUATION.md \
  13_OBSERVABILITY_AND_OPERATIONS.md \
  14_PRODUCT_ROADMAP.md \
  15_IMPLEMENTATION_PLAN.md \
  16_COST_MODEL.md \
  17_INCIDENT_RUNBOOKS.md \
  18_CODING_STANDARDS.md \
  19_LOCAL_DEVELOPMENT.md \
  20_THREAT_MODEL.md
do
  if [ ! -f "$documentation_dir/$document" ]; then
    printf 'ERROR: documentation file is missing: %s\n' "$document" >&2
    failures=$((failures + 1))
  fi

  if ! grep -Fq "]($document)" "$documentation_index"; then
    printf 'ERROR: 00_README.md does not link to %s.\n' "$document" >&2
    failures=$((failures + 1))
  fi
done

if [ "$failures" -ne 0 ]; then
  printf 'Documentation validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Documentation validation passed: stages 00-20 are present and linked.'
