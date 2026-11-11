#!/usr/bin/env bash
# Qwen3.5-0.8B 全量转换：校准集 -> LLM(rkllm) -> Vision(onnx -> rknn) -> out/
# 用法: bash scripts/01_convert_all.sh [--skip-vision]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv310/bin/python"
DEMO="$ROOT/rknn-llm/examples/multimodal_model_demo"
MODEL="$ROOT/Qwen3.5-0.8B-MIT"
OUT="$ROOT/out"
LOGS="$ROOT/logs"
mkdir -p "$OUT" "$LOGS"

TARGET="rk3576"
DTYPE="w4a16"
CORES=2
OPT=0
VISION_SIZE=448
SKIP_VISION=0
[ "${1:-}" = "--skip-vision" ] && SKIP_VISION=1

cd "$DEMO"

echo "== [1/5] 生成 input_embeds 校准集 =="
if [ -f "$DEMO/data/llm_inputs.json" ]; then
  echo "  已存在 data/llm_inputs.json，跳过"
else
  "$PY" data/make_input_embeds_for_quantize.py --path "$MODEL" --model_type qwen3.5 \
    2>&1 | tee "$LOGS/01_calib.log" | tail -5
fi
# 自带 export_rkllm.py 读死 data/inputs.json
ln -sfn llm_inputs.json "$DEMO/data/inputs.json"

echo "== [2/5] LLM -> rkllm ($DTYPE / $TARGET / npu=$CORES / opt=$OPT) =="
"$PY" "$ROOT/scripts/export_rkllm.py" \
  --path "$MODEL" --target-platform "$TARGET" --num_npu_core "$CORES" \
  --quantized_dtype "$DTYPE" --optimization_level "$OPT" \
  --device cpu --dataset data/inputs.json 2>&1 | tee "$LOGS/02_rkllm.log" | grep -aE "^OK ->|failed|Error" || true

if [ "$SKIP_VISION" = "0" ]; then
  echo "== [3/5] Vision -> onnx (${VISION_SIZE}x${VISION_SIZE}) =="
  "$PY" export/export_vision.py --path "$MODEL" --model_name qwen3.5 \
    --height "$VISION_SIZE" --width "$VISION_SIZE" --batch_size 1 --device cpu \
    2>&1 | tee "$LOGS/03_onnx.log" | grep -aE "Exported to|Output shape|Error" || true

  echo "== [4/5] Vision onnx -> rknn ($TARGET) =="
  "$PY" export/export_vision_rknn.py --path "./onnx/qwen3.5_vision.onnx" \
    --model_name qwen3.5 --height "$VISION_SIZE" --width "$VISION_SIZE" \
    --batch_size 1 --target-platform "$TARGET" \
    2>&1 | tee "$LOGS/04_rknn.log" | grep -aE "rknn building done|Error" || true
else
  echo "== [3-4/5] 跳过 Vision =="
fi

echo "== [5/5] 汇总到 $OUT =="
cp -f "$DEMO/rkllm/"*.rkllm "$OUT/" 2>/dev/null || true
cp -f "$DEMO/rknn/"*vision*.rknn "$OUT/" 2>/dev/null || true
ls -lh "$OUT"
echo "== CONVERT DONE =="
