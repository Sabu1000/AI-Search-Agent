#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
design="$repository_root/universal_ai_search_documentation/03_SEARCH_ENGINE_DESIGN.md"

if [ ! -f "$design" ]; then
  printf '%s\n' 'ERROR: 03_SEARCH_ENGINE_DESIGN.md is missing.' >&2
  exit 1
fi

failures=0

for heading in \
  '## Goals and scope' \
  '## Non-negotiable invariants' \
  '## Search modes and terminology' \
  '## End-to-end retrieval flow' \
  '## Query planner contract' \
  '## Query normalization and exact terms' \
  '## Filter semantics' \
  '## Candidate eligibility and tenant isolation' \
  '## Retrieval lanes' \
  '## Fusion, reranking, and deterministic ordering' \
  '## Deduplication and result shaping' \
  '## Context selection' \
  '## Grounded answer contract' \
  '## No-results and insufficient-evidence behavior' \
  '## Prompt-injection and output safety' \
  '## Caching and consistency' \
  '## Failure and degradation behavior' \
  '## Performance and observability' \
  '## Evaluation and change control' \
  '## Requirement coverage' \
  '## Stage 03 completion criteria'
do
  if ! grep -Fqx "$heading" "$design"; then
    printf 'ERROR: search design is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for invariant in \
  'Global retrieval followed by application-side' \
  'Explicit user filters can narrow access but can never expand it.' \
  'Search only fully promoted document versions' \
  'Return an honest no-results or insufficient-evidence outcome' \
  'never shared across workspaces'
do
  if ! grep -Fq "$invariant" "$design"; then
    printf 'ERROR: search design is missing invariant: %s\n' "$invariant" >&2
    failures=$((failures + 1))
  fi
done

for provider in gmail google_drive github local_files
do
  if ! grep -Fq "\`$provider\`" "$design"; then
    printf 'ERROR: search design is missing canonical provider: %s\n' "$provider" >&2
    failures=$((failures + 1))
  fi
done

for lane in 'Exact/title' Keyword Vector Trigram
do
  lane_count=$(grep -Ec "^\| $lane \|" "$design" || true)

  if [ "$lane_count" -ne 1 ]; then
    printf 'ERROR: retrieval lane %s must have exactly one definition; found %s.\n' "$lane" "$lane_count" >&2
    failures=$((failures + 1))
  fi
done

for contract_field in \
  planner_version \
  normalized_query \
  semantic_query \
  exact_terms \
  needs_live_search \
  answer_markdown \
  claims \
  citations \
  insufficient_evidence \
  follow_up_queries
do
  if ! grep -Fq "\"$contract_field\"" "$design"; then
    printf 'ERROR: search design is missing contract field: %s\n' "$contract_field" >&2
    failures=$((failures + 1))
  fi
done

for reason in \
  no_authorized_results \
  weak_retrieval_evidence \
  conflicting_evidence \
  required_dependency_unavailable \
  grounding_validation_failed
do
  if ! grep -Fq "\`$reason\`" "$design"; then
    printf 'ERROR: search design is missing insufficient-evidence reason: %s\n' "$reason" >&2
    failures=$((failures + 1))
  fi
done

for requirement in SEARCH-001 ANSWER-001 CITATION-001 SAFETY-001
do
  mapping_count=$(grep -Ec "^\| \`$requirement\` \|" "$design" || true)

  if [ "$mapping_count" -ne 1 ]; then
    printf 'ERROR: requirement %s must have exactly one search mapping; found %s.\n' "$requirement" "$mapping_count" >&2
    failures=$((failures + 1))
  fi
done

for threshold in '>= 0.85' '>= 0.95' '<= 0.03' '<= 6 seconds'
do
  if ! grep -Fq "\`$threshold\`" "$design"; then
    printf 'ERROR: search design is missing release threshold: %s\n' "$threshold" >&2
    failures=$((failures + 1))
  fi
done

for owner_document in \
  04_DATABASE_SCHEMA.md \
  05_API_SPECIFICATION.md \
  07_INDEXING_PIPELINE.md \
  08_SECURITY_AND_PRIVACY.md \
  12_TESTING_AND_EVALUATION.md \
  20_THREAT_MODEL.md
do
  if [ ! -f "$repository_root/universal_ai_search_documentation/$owner_document" ]; then
    printf 'ERROR: search owner document is missing: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi

  if ! grep -Fq "]($owner_document)" "$design"; then
    printf 'ERROR: search design does not link to owner: %s\n' "$owner_document" >&2
    failures=$((failures + 1))
  fi
done

for behavior in \
  'rrf(candidate) = sum(lane_weight / (60 + rank_in_lane))' \
  'At most 300 unique candidates enter fusion.' \
  'between 8 and 15 passages' \
  '12,000 tokens' \
  'Every material claim' \
  'A response is never marked complete when' \
  'Given the same query plan'
do
  if ! grep -Fq "$behavior" "$design"; then
    printf 'ERROR: search design is missing required behavior: %s\n' "$behavior" >&2
    failures=$((failures + 1))
  fi
done

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$design"; then
  printf '%s\n' 'ERROR: search design contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Search-engine design validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Search-engine design validation passed: authorization, retrieval, ranking, grounding, and evaluation contracts are defined.'
