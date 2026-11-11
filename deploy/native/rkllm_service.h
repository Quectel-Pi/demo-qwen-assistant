// SPDX-License-Identifier: Apache-2.0
// rkllm_service.h —— 板端常驻多模态推理服务协议定义
//
// 设计要点：官方 demo 二进制把图片写死在启动入参里，换图必须重启进程（约 7s）。
// 本服务直接调用 librkllmrt 的 C API，每一轮推理单独构造 RKLLMInput，
// 因此可以在运行期任意更换图片（成本只剩 vision encoder 的 0.8s）。
//
// 传输：Unix domain socket，行分隔的 JSON。请求/响应都是单行 JSON，
// 文本增量以 {"ev":"tok","t":"..."} 流式推送，便于 UI 逐字渲染。

#ifndef RKLLM_SERVICE_H_
#define RKLLM_SERVICE_H_

#define RKLLM_SERVICE_DEFAULT_SOCK "/run/rkllm-chat.sock"

// 视觉编码器输入是实际送入（已 expand2square + resize）的方形图尺寸。
// 原本 392 是为了保持宽高比、四边补灰；这里预缩放成等面积正方形，
// 细节分辨率提升约 12%，视觉 token 数仍是 196。
#define IMG_SIDE 420

// 视觉 token 数：与 encoder 输出 dims[0] 一致（imgenc 打印 196x1024）。
// Qwen3.5 的 projector 把 196 个 patch 归并成 49 个 LLM 侧 token。
#define IMG_TOKENS 196

// encoder 每个视觉 token 的 embedding 维度（vision rknn 输出 dims[1]）。
#define IMG_EMBED_LEN 1024

// 把执行到一半被中断的请求在 UI 上标记出来
#define RKLLM_ABORTED_MARK "\x01\x01"

#endif  // RKLLM_SERVICE_H_
