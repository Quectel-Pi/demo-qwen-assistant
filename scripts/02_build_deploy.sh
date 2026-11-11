#!/usr/bin/env bash
# 交叉编译板端 C++ demo 并打包到 out/
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY="$ROOT/rknn-llm/examples/multimodal_model_demo/deploy"
OUT="$ROOT/out"

echo "== 交叉编译 (aarch64-linux-gnu) =="
bash "$DEPLOY/build-linux.sh" 2>&1 | tail -15

echo "== 打包到 $OUT/demo_Linux_aarch64 =="
rm -rf "$OUT/demo_Linux_aarch64"
cp -r "$DEPLOY/install/demo_Linux_aarch64" "$OUT/"
ls -lh "$OUT/demo_Linux_aarch64" "$OUT/demo_Linux_aarch64/lib"
echo "== BUILD DONE =="
