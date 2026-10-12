#!/usr/bin/env bash
# Builds src/omsync/web/static/css/app.css from src/omsync/web/styles/app.css.
#
# The Tailwind CSS v4 standalone CLI is pinned below and downloaded once into .tools/
# (gitignored). Its SHA-256 digest is checked before it runs. The built CSS is committed,
# so `omsync serve` works without Node or this script.
set -euo pipefail

TAILWIND_VERSION="4.3.3"  # published 2026-07-16; the newest v4 release at least two weeks old

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT/.tools"
INPUT="$ROOT/src/omsync/web/styles/app.css"
OUTPUT="$ROOT/src/omsync/web/static/css/app.css"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)
    ASSET="tailwindcss-macos-arm64"
    SHA256="cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d" ;;
  Darwin-x86_64)
    ASSET="tailwindcss-macos-x64"
    SHA256="7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1" ;;
  Linux-x86_64)
    ASSET="tailwindcss-linux-x64"
    SHA256="dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a" ;;
  *)
    echo "Unsupported platform: $(uname -s) $(uname -m)" >&2
    exit 1 ;;
esac

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

CLI="$TOOLS_DIR/tailwindcss-v$TAILWIND_VERSION"
if [[ ! -x "$CLI" ]]; then
  mkdir -p "$TOOLS_DIR"
  TMP="$(mktemp "$TOOLS_DIR/download.XXXXXX")"
  trap 'rm -f "$TMP"' EXIT
  curl -fsSL --retry 3 -o "$TMP" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v$TAILWIND_VERSION/$ASSET"
  actual="$(sha256_of "$TMP")"
  if [[ "$actual" != "$SHA256" ]]; then
    echo "Checksum mismatch for $ASSET: expected $SHA256, got $actual" >&2
    exit 1
  fi
  chmod +x "$TMP"
  mv "$TMP" "$CLI"
  trap - EXIT
fi

"$CLI" --input "$INPUT" --output "$OUTPUT" --minify
echo "Tailwind CSS v$TAILWIND_VERSION: wrote ${OUTPUT#"$ROOT"/} ($(wc -c < "$OUTPUT" | tr -d ' ') bytes)"
