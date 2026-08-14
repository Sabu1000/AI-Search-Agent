#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
specification="$repository_root/universal_ai_search_documentation/07_INDEXING_PIPELINE.md"
failures=0

for heading in \
  '## Goals and ownership' \
  '## Pipeline state machine' \
  '## Input and extraction contract' \
  '## Normalization and language' \
  '## Chunking contract' \
  '## Deterministic identity and deduplication' \
  '## Embedding contract' \
  '## Durable queue and worker authority' \
  '## Atomic persistence and promotion' \
  '## Reindexing and permission changes' \
  '## Limits and failure behavior' \
  '## Observability and privacy' \
  '## Test matrix and release gates' \
  '## Requirement coverage' \
  '## Stage 07 completion criteria'
do
  if ! grep -Fqx "$heading" "$specification"; then
    printf 'ERROR: indexing specification is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for contract in \
  'NormalizedDocument' 'Unicode NFC' '400–800' 'UUIDv5' 'SimHash' \
  '1536' 'finite' 'FOR UPDATE SKIP LOCKED' 'NOBYPASSRLS' \
  'current_document_version_id' 'dead_letter' 'UNSUPPORTED_MEDIA_TYPE' \
  'old ready version' 'fake connector' 'B5'
do
  if ! grep -Fqi "$contract" "$specification"; then
    printf 'ERROR: indexing specification is missing contract: %s\n' "$contract" >&2
    failures=$((failures + 1))
  fi
done

for requirement in GOOGLE-001 GITHUB-001 DESKTOP-001 SYNC-001 SEARCH-001 ANSWER-001 CITATION-001 CONNECTION-001 ACCOUNT-001 SAFETY-001
do
  if ! grep -Fq "\`$requirement\`" "$specification"; then
    printf 'ERROR: indexing specification does not map requirement: %s\n' "$requirement" >&2
    failures=$((failures + 1))
  fi
done

if grep -Eqi '\b(TODO|TBD|FIXME)\b' "$specification"; then
  printf '%s\n' 'ERROR: indexing specification contains unresolved placeholders.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Indexing specification validation failed with %s error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Indexing pipeline specification validated.'
