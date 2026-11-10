#!/bin/sh
# Runs an omsync command on the live nursery checkout (../omsync-nursery) instead of the release
# locked in uv.lock, so art in progress shows before a release, colour changes included:
#
#   sh scripts/dev-nursery.sh serve
#
# A plain `uv run omsync serve` goes back to the locked release.
set -e
cd "$(dirname "$0")/.."
exec uv run --with-editable "${NURSERY:-../omsync-nursery}" omsync "$@"
