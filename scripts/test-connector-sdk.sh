#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

docker build \
  --file "$repository_root/infrastructure/docker/connector-sdk-test.Dockerfile" \
  --tag universal-ai-search-connector-sdk-test \
  "$repository_root"

docker run --rm universal-ai-search-connector-sdk-test
