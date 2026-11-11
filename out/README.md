# Qwen3.5-0.8B-MIT → RK3576 转换产物

生成时间：2026-11-04
转换参数：`target_platform=RK3576` / `quantized_dtype=W4A16` / `num_npu_core=2` / `optimization_level=0`

## 产物清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `qwen3.5-0.8b-mit_w4a16_rk3576.rkllm` | 898 MB | LLM 主体（24 层，含 tokenizer + embedding），W4A16，2 NPU 核，opt=0（速度优先） |
| `qwen3.5_vision_rk3576.rknn` | 214 MB | Vision encoder + merger（12 层 ViT，448×448，float 权重） |
| `demo_Linux_aarch64/` | 15 MB | 板端 C++ demo（`demo` / `imgenc` / `audioenc` + `lib/librkllmrt.so`、`librknnrt.so`） |

## 板端部署

```bash
adb push out/demo_Linux_aarch64 /data/
adb push out/*.rkllm out/*.rknn /data/models/
adb shell
cd /data/demo_Linux_aarch64
export LD_LIBRARY_PATH=./lib
ln -sfn /data/models models

# 纯文本
./demo "" "" "" "" models/qwen3.5-0.8b-mit_w4a16_rk3576.rkllm 2048 4096 2 rk3576

# 图像（Qwen3.5 的 3 个特殊 token）
./demo demo.jpg models/qwen3.5_vision_rk3576.rknn "" "" \
  models/qwen3.5-0.8b-mit_w4a16_rk3576.rkllm 2048 4096 2 rk3576 \
  "<|vision_start|>" "<|vision_end|>" "<|image_pad|>"
```

参数顺序：`image_path img_encoder audio_path aud_encoder rkllm max_new_tokens max_context_len npu_cores platform [img_start img_end img_content ...]`

约束：`max_context_len > 文本 token 数 + 图像 token 数 + max_new_tokens`。

## 参考性能（RK3576，官方 benchmark，Qwen3.5 0.8B w4a16）

| Seqlen | New tokens | TTFT | Tokens/s | 内存 |
|---|---|---|---|---|
| 128 | 64 | 1369 ms | 18.8 | 690 MB |

Vision encoder（448×448）约 690 ms（NPU）。

## 复现

```bash
bash scripts/00_setup_env.sh                 # 建 .venv310 + 装 rkllm/rknn toolkit
bash scripts/01_convert_all.sh               # 校准集 -> rkllm -> onnx -> rknn
bash scripts/02_build_deploy.sh              # 交叉编译 C++ demo 并打包到 out/
```

只用 `optimization_level=0` 时直接调：

```bash
cd rknn-llm/examples/multimodal_model_demo
../../.venv310/bin/python ../../../scripts/export_rkllm.py \
  --path /home/leox/RKNN-MODEL/Qwen3.5-0.8B-MIT \
  --target-platform rk3576 --quantized_dtype w4a16 \
  --num_npu_core 2 --optimization_level 0 --dataset data/inputs.json
```

## 环境备注

- `.venv310` 中 torch 被升到 2.6.0+cpu（rkllm-toolkit 要求），rknn-toolkit2 2.5.0 声明 `torch<=2.4.0` 会报冲突警告，但实际转换正常。
- `transformers==5.8.0` + torch 2.4 会因 `torch.library.custom_op` schema 推断失败而 import 崩，必须 2.6.0。
- `download.pytorch.org` 直连 SSL 中断，本机需走代理 `http://127.0.0.1:7897`。
- 自带 `export_rkllm.py` 把 `optimization_level` 写死为 1，且 dataset 固定 `data/inputs.json`；已改用 `scripts/export_rkllm.py`（`data/inputs.json` 软链到 `llm_inputs.json`）。
