#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repository_root"

docker build \
  --file infrastructure/docker/desktop-test.Dockerfile \
  --tag universal-ai-search-desktop-test:local \
  .
