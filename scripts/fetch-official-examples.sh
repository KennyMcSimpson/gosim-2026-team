#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/training/official-examples"
ZIP="$DIR/gosim-observer-examples.zip"
SUM="$ZIP.sha256"
URL="${GOSIM_EXAMPLES_URL:-https://github.com/gosimfoundation/hackathon-survey26/releases/download/examples-2026-10-02/gosim-observer-examples.zip}"
OUT="${1:-$ROOT/.cache/gosim-observer-examples}"

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $# -le 1 ]] || fail "Usage: $0 [destination]"
[[ -f "$SUM" ]] || fail "Missing pinned checksum: $SUM"
[[ ! -e "$OUT" && ! -L "$OUT" ]] || fail "Destination already exists: $OUT"
command -v unzip >/dev/null || fail "unzip is required"
if command -v sha256sum >/dev/null; then
  HASH=(sha256sum)
elif command -v shasum >/dev/null; then
  HASH=(shasum -a 256)
else
  fail "sha256sum or shasum is required"
fi

mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
STAGE="$(mktemp -d "$(dirname "$OUT")/.gosim-examples.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT

CHECK_DIR="$DIR"
if [[ "${REFETCH:-0}" == "1" || ! -f "$ZIP" ]]; then
  command -v curl >/dev/null || fail "curl is required to download"
  mkdir "$STAGE/download"
  printf 'Downloading %s\n' "$URL"
  curl -fsSL -o "$STAGE/download/gosim-observer-examples.zip" "$URL"
  CHECK_DIR="$STAGE/download"
fi

# A mutable upstream release must still match the repository's pinned bytes.
(cd "$CHECK_DIR" && "${HASH[@]}" -c "$SUM")
mkdir "$STAGE/unpacked"
unzip -q "$CHECK_DIR/gosim-observer-examples.zip" -d "$STAGE/unpacked"
EXTRACTED="$STAGE/unpacked/gosim-observer-examples"
[[ -d "$EXTRACTED" ]] || fail "Unexpected archive layout"
[[ ! -e "$OUT" && ! -L "$OUT" ]] || fail "Destination already exists: $OUT"
mv "$EXTRACTED" "$OUT"
printf 'Extracted to: %s\n' "$OUT"
printf 'Next: cd "%s" && python3 runner/verify_engine.py\n' "$OUT"
