#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$repository_root/compose.database-test.yaml"
project_name="universal-ai-search-database-test"

cleanup() {
  docker compose --project-name "$project_name" --file "$compose_file" \
    down --volumes --remove-orphans
}

trap cleanup EXIT INT TERM
cleanup

docker compose --project-name "$project_name" --file "$compose_file" \
  up --build --abort-on-container-exit --exit-code-from database-test
