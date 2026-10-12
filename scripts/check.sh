#!/bin/sh
# The static checks CI and the pre-commit hook run. Tests run separately: uv run pytest -q
set -e
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
