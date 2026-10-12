#!/bin/sh
# Builds a disposable data directory from the synthetic statements in tests/fixtures, then prints
# the variables that point omsync at it. Usage: scripts/dev-data.sh [DIR]
set -e
cd "$(dirname "$0")/.."
tmp=${TMPDIR:-/tmp}
dir=${1:-$(mktemp -d "${tmp%/}/omsync-dev.XXXXXX")}
if [ -e "$dir/omsync.db" ]; then
    echo "dev-data: $dir already has a database" >&2
    exit 1
fi
export OMSYNC_DATA_DIR="$dir" OMSYNC_CONFIG="$dir/omsync.toml"
fixtures=tests/fixtures/statements

uv run omsync db upgrade >/dev/null
uv run omsync accounts add --institution testbank --institution-name "Test Bank" \
    --statement-account 000012345678 --name Checking --type checking >/dev/null
uv run omsync accounts add --institution acmecard --institution-name "Acme Card" \
    --statement-account 000098765432 --name Card --type credit_card >/dev/null
uv run omsync accounts add --institution boa --institution-name "Bank of America" \
    --statement-account 1234 --name "BoA Checking" --type checking >/dev/null

mkdir -p "$dir/statements/TESTBANK" "$dir/statements/ACMECARD" "$dir/statements/BOA/1234"
cp "$fixtures/ofx1_checking.ofx" "$dir/statements/TESTBANK/"
cp "$fixtures/ofx2_card.ofx" "$dir/statements/ACMECARD/"
cp "$fixtures/boa_checking.csv" "$dir/statements/BOA/1234/"
uv run omsync import --apply | tail -n 3

echo
echo "export OMSYNC_DATA_DIR=$dir OMSYNC_CONFIG=$dir/omsync.toml"
