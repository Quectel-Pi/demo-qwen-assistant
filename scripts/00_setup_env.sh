#!/usr/bin/env bash
# RKNN/RKLLM 转换环境准备（Ubuntu 22.04 + Python 3.10 venv）
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VENV="$ROOT/.venv310"
TS="https://pypi.tuna.tsinghua.edu.cn/simple"
TORCH_IDX="https://download.pytorch.org/whl/cpu"
# download.pytorch.org 在本机直连会 SSL 中断，走本地代理；无代理时置空
PIP_PROXY="${PIP_PROXY-http://127.0.0.1:7897}"
PROXY_OPT=()
[ -n "$PIP_PROXY" ] && PROXY_OPT=(--proxy "$PIP_PROXY")

echo "== 1/4 venv =="
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip -i "$TS"

echo "== 2/4 torch 2.6.0+cpu =="
"$VENV/bin/pip" install -q --progress-bar off "${PROXY_OPT[@]}" \
  torch==2.6.0+cpu torchvision==0.21.0+cpu \
  --index-url "$TORCH_IDX" --extra-index-url "$TS"

echo "== 3/4 deps =="
"$VENV/bin/pip" install -q --progress-bar off \
  "numpy>=1.23.1,<=1.26.4" "transformers==5.8.0" "datasets==4.1.1" "pyarrow==21.0.0" \
  "tqdm>=4.64.0" "sentencepiece==0.2.0" "accelerate>=1.0.1,<=1.5.2" \
  "protobuf>=4.21.6,<=4.25.4" "transformers_stream_generator==0.0.5" "einops==0.4.1" \
  "scipy>=1.5.4" "tiktoken>=0.7.0,<=0.9.0" "tabulate==0.9.0" "Jinja2==3.1.4" \
  "safetensors==0.5.3" "colorlog==6.8.2" "datamodel_code_generator==0.26.0" \
  "jsonschema==4.23.0" "flatbuffers==24.3.25" "pillow==10.4.0" \
  "optimum==1.23.3" "jsonlines==4.0.0" "timm==1.0.19" "easydict==1.13" \
  "addict==2.4.0" "matplotlib>=3.9.4" "onnx==1.18.0" "onnxruntime>=1.16.0" \
  "rknn-toolkit2==2.5.0" -i "$TS"

echo "== 4/4 rkllm-toolkit (--no-deps, 依赖已装) =="
"$VENV/bin/pip" install --no-deps \
  "$ROOT/rknn-llm/rkllm-toolkit/packages/rkllm_toolkit-1.3.1-cp310-cp310-linux_x86_64.whl"

echo "== verify =="
"$VENV/bin/python" - <<'PY'
import torch, transformers, numpy, onnx
from rkllm.api import RKLLM
from rknn.api import RKNN
print("torch", torch.__version__)
print("transformers", transformers.__version__)
print("numpy", numpy.__version__, "onnx", onnx.__version__)
print("RKLLM OK", RKLLM.__module__)
print("RKNN OK", RKNN.__module__)
PY
echo "== DONE =="
