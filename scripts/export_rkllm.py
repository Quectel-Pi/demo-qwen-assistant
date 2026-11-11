#!/usr/bin/env python3
"""Qwen3.5-0.8B -> rkllm 转换（可指定 optimization_level）。

不同于 rknn-llm 自带 export_rkllm.py 的地方：
  1. optimization_level 可配置（自带脚本写死 1）
  2. dataset 路径显式传入（自带脚本写死 data/inputs.json）

在 multimodal_model_demo 目录下运行（校准集用相对路径）：
  python scripts/export_rkllm.py --path /path/to/Qwen3.5-0.8B-MIT \
      --target-platform rk3576 --quantized_dtype w4a16 --num_npu_core 2 --optimization_level 0
"""
import argparse
import os
import sys

from rkllm.api import RKLLM


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--path", required=True, help="HuggingFace 模型目录")
    p.add_argument("--target-platform", default="rk3576")
    p.add_argument("--num_npu_core", type=int, default=2)
    p.add_argument("--quantized_dtype", default="w4a16")
    p.add_argument("--quantized_algorithm", default="normal")
    p.add_argument("--optimization_level", type=int, default=0, choices=[0, 1])
    p.add_argument("--device", default="cpu")
    p.add_argument("--dataset", default="data/inputs.json",
                   help="校准集 json（input_embeds 格式）")
    p.add_argument("--savepath", default=None)
    args = p.parse_args()

    if not os.path.exists(args.dataset):
        print(f"ERROR: dataset not found: {args.dataset}", file=sys.stderr)
        return 1

    savepath = args.savepath or os.path.join(
        "rkllm",
        f"{os.path.basename(os.path.normpath(args.path)).lower()}"
        f"_{args.quantized_dtype}_{args.target_platform}.rkllm",
    )
    os.makedirs(os.path.dirname(savepath) or ".", exist_ok=True)

    llm = RKLLM()
    ret = llm.load_huggingface(model=args.path, device=args.device)
    if ret != 0:
        print("Load model failed!", file=sys.stderr)
        return ret

    ret = llm.build(
        do_quantization=True,
        optimization_level=args.optimization_level,
        quantized_dtype=args.quantized_dtype,
        quantized_algorithm=args.quantized_algorithm,
        target_platform=args.target_platform,
        num_npu_core=args.num_npu_core,
        extra_qparams=None,
        dataset=args.dataset,
    )
    if ret != 0:
        print("Build model failed!", file=sys.stderr)
        return ret

    ret = llm.export_rkllm(savepath)
    if ret != 0:
        print("Export model failed!", file=sys.stderr)
        return ret

    print(f"OK -> {savepath} ({os.path.getsize(savepath)/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
