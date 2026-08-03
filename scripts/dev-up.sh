#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repository_root"

docker compose up --detach --build
docker compose ps

printf '%s\n' 'Universal AI Search is starting at http://localhost:3000.'
printf '%s\n' 'API readiness is available at http://localhost:8000/health/ready.'
