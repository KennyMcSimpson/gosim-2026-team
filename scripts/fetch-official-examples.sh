#!/usr/bin/env bash
# 校验（可选重新下载）官方 gosim-observer-examples.zip 并解压到仓库根目录旁的工作副本。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/third-party/gosim-observer-examples"
ZIP="$DIR/gosim-observer-examples.zip"
SUM="$DIR/gosim-observer-examples.zip.sha256"
URL="${GOSIM_EXAMPLES_URL:-https://github.com/gosimfoundation/hackathon-survey26/releases/download/examples-2026-10-02/gosim-observer-examples.zip}"
OUT="${1:-$ROOT/.cache/gosim-observer-examples}"

mkdir -p "$DIR" "$(dirname "$OUT")"

if [[ "${REFETCH:-0}" == "1" ]] || [[ ! -f "$ZIP" ]]; then
  echo "Downloading $URL ..."
  curl -fsSL -o "$ZIP" "$URL"
fi

echo "Verifying sha256 ..."
(
  cd "$DIR"
  shasum -a 256 -c gosim-observer-examples.zip.sha256
)

rm -rf "$OUT"
mkdir -p "$OUT"
unzip -q "$ZIP" -d "$(dirname "$OUT")"
# zip 顶层为 gosim-observer-examples/
if [[ -d "$(dirname "$OUT")/gosim-observer-examples" && "$(dirname "$OUT")/gosim-observer-examples" != "$OUT" ]]; then
  # 若 OUT 不是默认名，移动
  if [[ "$(basename "$OUT")" != "gosim-observer-examples" ]]; then
    rm -rf "$OUT"
    mv "$(dirname "$OUT")/gosim-observer-examples" "$OUT"
  fi
fi
echo "Extracted to: $OUT"
echo "Next: cd $OUT && python3 runner/verify_engine.py"
