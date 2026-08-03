#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
failures=0

for required_file in \
  .env.example \
  compose.yaml \
  package.json \
  pnpm-workspace.yaml \
  apps/api/pyproject.toml \
  apps/api/src/universal_ai_search/api/app.py \
  apps/api/src/universal_ai_search/worker/main.py \
  apps/web/package.json \
  apps/web/src/app/page.tsx \
  apps/desktop/package.json \
  apps/desktop/src-tauri/Cargo.toml \
  packages/shared-types/package.json \
  packages/ui/package.json \
  packages/connector-sdk/pyproject.toml \
  infrastructure/docker/backend.Dockerfile \
  infrastructure/docker/connector-sdk-test.Dockerfile \
  infrastructure/docker/database-test.Dockerfile \
  infrastructure/docker/desktop-test.Dockerfile \
  infrastructure/docker/postgres-init/001-extensions.sql \
  infrastructure/docker/web.Dockerfile \
  .github/workflows/ci.yml
do
  if [ ! -f "$repository_root/$required_file" ]; then
    printf 'ERROR: foundation file is missing: %s\n' "$required_file" >&2
    failures=$((failures + 1))
  fi
done

for forbidden_file in .env id_rsa credentials.json
do
  if [ -f "$repository_root/$forbidden_file" ]; then
    printf 'ERROR: forbidden local credential file exists at repository root: %s\n' "$forbidden_file" >&2
    failures=$((failures + 1))
  fi
done

if grep -Fq '"identifier": "com.universalaisearch.desktop"' \
  "$repository_root/apps/desktop/src-tauri/tauri.conf.json"; then
  :
else
  printf '%s\n' 'ERROR: desktop bundle identifier is missing or invalid.' >&2
  failures=$((failures + 1))
fi

if command -v docker >/dev/null 2>&1; then
  if ! docker compose --project-directory "$repository_root" config --quiet; then
    printf '%s\n' 'ERROR: Docker Compose configuration is invalid.' >&2
    failures=$((failures + 1))
  fi
fi

if [ "$failures" -ne 0 ]; then
  printf 'Foundation validation failed with %d error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Foundation validation passed: repository structure and Compose configuration are valid.'
