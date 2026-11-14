#!/bin/sh
# The static checks CI and the pre-commit hook run: ruff lint, ruff format, Biome for the
# JavaScript and CSS, and import layers. Tests run separately: uv run pytest -q
# It runs every check, shows all the failures, and exits non-zero if any check failed.
cd "$(dirname "$0")/.."

failed=0
step() {
    echo "== $*"
    "$@" || failed=1
}

step uv run ruff check .
step uv run ruff format --check .
step sh scripts/biome.sh check .
step uv run lint-imports

if [ "$failed" -ne 0 ]; then
    echo "check: failed" >&2
    exit 1
fi
echo "check: passed"
