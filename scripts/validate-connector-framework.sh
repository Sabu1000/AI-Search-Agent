#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
specification="$repository_root/universal_ai_search_documentation/06_CONNECTOR_FRAMEWORK.md"
project_specification="$repository_root/universal_ai_search_documentation/01_PROJECT_SPEC.md"
failures=0

if [ ! -f "$specification" ]; then
  printf '%s\n' 'ERROR: 06_CONNECTOR_FRAMEWORK.md is missing.' >&2
  exit 1
fi

for heading in \
  '## Goals and ownership' \
  '## Implemented SDK package' \
  '## Canonical providers and source identity' \
  '## Connector protocol' \
  '## Credential security boundary' \
  '## Sync context and selection enforcement' \
  '## Normalized document contract' \
  '## Change stream contract' \
  '## Full and incremental synchronization' \
  '## Pagination and backpressure' \
  '## Retry and error taxonomy' \
  '## Registry and construction' \
  '## Provider-specific requirements' \
  '## Health, logging, and metrics boundary' \
  '## Connector certification test kit' \
  '## Requirement coverage' \
  '## Stage 06 completion criteria'
do
  if ! grep -Fqx "$heading" "$specification"; then
    printf 'ERROR: connector framework is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for provider in gmail google_drive github local_files
do
  if ! grep -Fq "\`$provider\`" "$specification"; then
    printf 'ERROR: connector framework is missing canonical provider: %s\n' "$provider" >&2
    failures=$((failures + 1))
  fi
done

for change_type in UPSERT DELETE PERMISSION_CHANGED CURSOR_ADVANCED
do
  if ! grep -Fq "\`$change_type\`" "$specification"; then
    printf 'ERROR: connector framework is missing change type: %s\n' "$change_type" >&2
    failures=$((failures + 1))
  fi
done

for error_type in \
  AuthenticationError \
  PermissionDeniedError \
  RateLimitError \
  ProviderUnavailableError \
  MalformedItemError \
  CursorInvalidError \
  ContractViolationError
do
  if ! grep -Fq "\`$error_type\`" "$specification"; then
    printf 'ERROR: connector framework is missing error contract: %s\n' "$error_type" >&2
    failures=$((failures + 1))
  fi
done

for required_file in \
  packages/connector-sdk/pyproject.toml \
  packages/connector-sdk/src/uas_connector_sdk/models.py \
  packages/connector-sdk/src/uas_connector_sdk/protocol.py \
  packages/connector-sdk/src/uas_connector_sdk/errors.py \
  packages/connector-sdk/src/uas_connector_sdk/retry.py \
  packages/connector-sdk/src/uas_connector_sdk/registry.py \
  packages/connector-sdk/src/uas_connector_sdk/contract.py \
  packages/connector-sdk/src/uas_connector_sdk/testing.py \
  packages/connector-sdk/tests/test_contract.py \
  packages/connector-sdk/tests/test_retry.py \
  infrastructure/docker/connector-sdk-test.Dockerfile \
  scripts/test-connector-sdk.sh
do
  if [ ! -f "$repository_root/$required_file" ]; then
    printf 'ERROR: connector SDK file is missing: %s\n' "$required_file" >&2
    failures=$((failures + 1))
  fi
done

for implementation_contract in \
  'class Connector(Protocol)' \
  'class ConnectorRegistry' \
  'class RetryPolicy' \
  'async def validate_change_stream' \
  'class FakeConnector' \
  'class Credentials' \
  'class NormalizedDocument'
do
  if ! grep -R -Fq "$implementation_contract" \
    "$repository_root/packages/connector-sdk/src/uas_connector_sdk"; then
    printf 'ERROR: connector SDK implementation is missing: %s\n' \
      "$implementation_contract" >&2
    failures=$((failures + 1))
  fi
done

expected_requirements='CONNECTION-001
DESKTOP-001
GITHUB-001
GOOGLE-001
SAFETY-001
SYNC-001'
connector_requirements=$(sed -n \
  's/^| `\([A-Z][A-Z-]*-[0-9][0-9][0-9]\)` |.*/\1/p' \
  "$specification" | sort -u)

if [ "$connector_requirements" != "$expected_requirements" ]; then
  printf '%s\n' 'ERROR: connector requirement mappings are incomplete or unexpected.' >&2
  failures=$((failures + 1))
fi

if [ ! -f "$project_specification" ]; then
  printf '%s\n' 'ERROR: project specification is missing.' >&2
  failures=$((failures + 1))
else
  for requirement in $expected_requirements
  do
    if ! grep -Fq "| \`$requirement\` |" "$project_specification"; then
      printf 'ERROR: connector requirement is not declared by project spec: %s\n' \
        "$requirement" >&2
      failures=$((failures + 1))
    fi
  done
fi

for contract in \
  'exactly one `CURSOR_ADVANCED`' \
  'must be last' \
  'SHA-256' \
  'SecretStr' \
  'full jitter' \
  '99% line coverage' \
  './scripts/test-connector-sdk.sh'
do
  if ! grep -Fq "$contract" "$specification"; then
    printf 'ERROR: connector framework is missing required behavior: %s\n' "$contract" >&2
    failures=$((failures + 1))
  fi
done

if grep -Eiq '(^|[^[:alpha:]])(TODO|TBD)([^[:alpha:]]|$)' "$specification"; then
  printf '%s\n' 'ERROR: connector framework contains unresolved TODO or TBD markers.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Connector framework validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Connector framework validation passed: SDK, lifecycle, stream, retry, safety, providers, and tests are defined.'
