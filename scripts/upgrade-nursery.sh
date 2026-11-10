#!/bin/sh
# Takes the newest omsync-nursery release (the nursery's `release` branch) into omsync:
#
#   sh scripts/upgrade-nursery.sh
#
# It re-locks the package, runs the checks and the tests, and commits uv.lock when they pass.
# When anything fails it puts the old uv.lock back. It needs a clean pyproject.toml and uv.lock.
# It only takes releases inside the version range that pyproject.toml gives omsync-nursery.
set -e
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain -- pyproject.toml uv.lock)" ]; then
  echo "pyproject.toml or uv.lock has uncommitted changes; commit or discard them first" >&2
  exit 1
fi

# The commit uv.lock records for the nursery, after the "#" of its git source.
locked_commit() {
  sed -n '/^name = "omsync-nursery"$/,/^$/p' uv.lock | sed -n 's/^source = .*#\([0-9a-f]*\)".*/\1/p'
}

# The nursery version uv.lock records.
locked_version() {
  sed -n '/^name = "omsync-nursery"$/,/^$/p' uv.lock | sed -n 's/^version = "\(.*\)"$/\1/p'
}

old_commit="$(locked_commit)"
if ! uv lock --upgrade-package omsync-nursery; then
  git checkout -- uv.lock
  exit 1
fi
new_commit="$(locked_commit)"

if [ "$old_commit" = "$new_commit" ]; then
  echo "omsync already has the newest nursery release"
  exit 0
fi

# uv lock does not apply a version range to a package with a [tool.uv.sources] entry, so the
# range is checked here. A release outside it is a contract change.
if ! uv run --no-sync python - "$(locked_version)" <<'PY'
import sys
import tomllib

from packaging.requirements import Requirement

deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
spec = next(r for r in map(Requirement, deps) if r.name == "omsync-nursery").specifier
if sys.argv[1] not in spec:
    sys.exit(f"omsync-nursery {sys.argv[1]} is outside {spec} in pyproject.toml: a contract change")
PY
then
  echo "Update the host code and widen the range in one commit, then run this again" >&2
  git checkout -- uv.lock
  exit 1
fi

restore() {
  echo "Upgrade failed; putting the old uv.lock back" >&2
  git checkout -- uv.lock
  uv sync || true
  exit 1
}

uv sync && sh scripts/check.sh && uv run pytest -q || restore

git commit -q -m "chore(scenes): upgrade omsync-nursery to v$(locked_version)" -- uv.lock
git log --oneline -1
