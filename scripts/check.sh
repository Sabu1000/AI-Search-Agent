#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

"$repository_root/scripts/validate-docs.sh"
"$repository_root/scripts/validate-project-spec.sh"
"$repository_root/scripts/validate-architecture.sh"
"$repository_root/scripts/validate-search-design.sh"
"$repository_root/scripts/validate-database-schema.sh"
"$repository_root/scripts/validate-api-specification.sh"
"$repository_root/scripts/validate-connector-framework.sh"
"$repository_root/scripts/validate-implementation-status.sh"
"$repository_root/scripts/validate-foundation.sh"

printf '%s\n' 'All project checks passed.'
