#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

"$repository_root/scripts/validate-docs.sh"
"$repository_root/scripts/validate-project-spec.sh"

printf '%s\n' 'All project checks passed.'
