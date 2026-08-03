#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repository_root"

docker build \
  --file infrastructure/docker/backend.Dockerfile \
  --target test \
  --tag universal-ai-search-backend-test:local \
  .
