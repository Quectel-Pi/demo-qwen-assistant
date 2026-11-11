// SPDX-License-Identifier: Apache-2.0
//
// rkllm_service.cpp —— RK3576 常驻多模态推理服务
//
// 为什么需要它：官方 demo（/data/rkllm_qwen35/demo）把「要分析的图片」
// 固定在启动入参里，换一张图必须整个进程重启（约 7 秒）。交互页面要求
// 「点一下按钮 → 立刻分析摄像头当前画面」，7 秒不可接受。
//
// 本服务直接调用 rkllm / rknn 的 C API，每一轮推理单独构造 RKLLMInput，
// 图片编码结果作为 image_embed 传入，因此运行期换图只需重跑 vision encoder
// （约 0.8 秒），LLM 权重始终常驻内存。
//
// 对外的 IPC 是 Unix domain socket + 一行一个 JSON：
//   请求 {"cmd":"ask","prompt":"...","image":"/tmp/a.jpg","keep_history":1,"max_new":256}
//   响应 {"ev":"tok","t":"..."} 流式若干，最后 {"ev":"done",...}
//   控制 {"cmd":"abort"} {"cmd":"clear"} {"cmd":"ping"}

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <atomic>
#include <mutex>
#include <string>
#include <vector>

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"

#include "rkllm.h"
#include "rknn_api.h"

#include "rkllm_service.h"

// ── 小工具 ─────────────────────────────────────────────────────────

static double now_ms() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

// Diagnostic identity of the actual embedding passed to RKLLM, not a filename.
static uint64_t embedding_hash(const std::vector<float> &embedding) {
    uint64_t hash = UINT64_C(14695981039346656037);
    const unsigned char *bytes = reinterpret_cast<const unsigned char *>(embedding.data());
    for (size_t i = 0; i < embedding.size() * sizeof(float); ++i) {
        hash ^= bytes[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

// 日志统一走 stderr，stdout 保留给 rkllm 运行时自己的输出。
static void logf_(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "[svc] ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
    fflush(stderr);
}

// 把任意字节串转成合法的 JSON 字符串内容（不含两侧引号）。
// rkllm 按 UTF-8 增量吐字，理论上不会切坏多字节字符（有 RKLLM_RUN_WAITING），
// 但仍按字节转义，非法序列退回 '?'，避免整个 IPC 行被污染。
static std::string json_escape(const char *s, size_t n) {
    std::string out;
    out.reserve(n + 16);
    for (size_t i = 0; i < n; ++i) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += (char)c;
                }
        }
    }
    return out;
}

static std::string json_escape(const std::string &s) {
    return json_escape(s.data(), s.size());
}

// ── 极简 JSON 取值 ─────────────────────────────────────────────────
// 只处理本服务自己发出的扁平请求对象，不引入第三方 JSON 库。
static bool json_find(const std::string &js, const char *key, size_t *vb, size_t *ve) {
    std::string pat = std::string("\"") + key + "\"";
    size_t p = js.find(pat);
    if (p == std::string::npos) return false;
    p = js.find(':', p + pat.size());
    if (p == std::string::npos) return false;
    ++p;
    while (p < js.size() && (js[p] == ' ' || js[p] == '\t')) ++p;
    if (p >= js.size()) return false;
    if (js[p] == '"') {
        size_t q = p + 1;
        while (q < js.size() && js[q] != '"') {
            if (js[q] == '\\') ++q;
            ++q;
        }
        *vb = p + 1;
        *ve = q;
        return true;
    }
    size_t q = p;
    while (q < js.size() && js[q] != ',' && js[q] != '}') ++q;
    *vb = p;
    *ve = q;
    return true;
}

static std::string json_str(const std::string &js, const char *key, const char *dflt = "") {
    size_t b, e;
    if (!json_find(js, key, &b, &e)) return dflt;
    std::string raw = js.substr(b, e - b);
    std::string out;
    for (size_t i = 0; i < raw.size(); ++i) {
        if (raw[i] == '\\' && i + 1 < raw.size()) {
            char c = raw[++i];
            if (c == 'n') out += '\n';
            else if (c == 't') out += '\t';
            else if (c == 'r') out += '\r';
            else if (c == 'u' && i + 4 < raw.size()) {
                // 只解 BMP 常见情况，够用（路径/文本里极少出现）
                unsigned cp = strtoul(raw.substr(i + 1, 4).c_str(), nullptr, 16);
                i += 4;
                if (cp < 0x80) {
                    out += (char)cp;
                } else if (cp < 0x800) {
                    out += (char)(0xC0 | (cp >> 6));
                    out += (char)(0x80 | (cp & 0x3F));
                } else {
                    out += (char)(0xE0 | (cp >> 12));
                    out += (char)(0x80 | ((cp >> 6) & 0x3F));
                    out += (char)(0x80 | (cp & 0x3F));
                }
            } else {
                out += c;
            }
        } else {
            out += raw[i];
        }
    }
    return out;
}

static long json_int(const std::string &js, const char *key, long dflt) {
    size_t b, e;
    if (!json_find(js, key, &b, &e)) return dflt;
    std::string raw = js.substr(b, e - b);
    if (raw.empty()) return dflt;
    return strtol(raw.c_str(), nullptr, 10);
}

// ── 写 socket：整行 JSON，失败静默（客户端可能已断开）─────────────
static bool send_line(int fd, const std::string &payload) {
    std::string line = payload;
    line += "\n";
    size_t off = 0;
    while (off < line.size()) {
        ssize_t n = ::send(fd, line.data() + off, line.size() - off, MSG_NOSIGNAL);
        if (n <= 0) {
            if (errno == EINTR) continue;
            return false;
        }
        off += (size_t)n;
    }
    return true;
}

// ── 视觉编码器 ─────────────────────────────────────────────────────
// 输入：任意尺寸 RGB 图。内部先 expand2square（灰底 127.5）再缩放到 IMG_SIDE，
// 与官方 demo 的预处理口径一致，只是把「实际送入尺寸」对齐到模型声明的 448，
// 避免 demo 里 392 输入 / 448 size 的越界读。
class VisionEncoder {
public:
    ~VisionEncoder() { release(); }

    bool init(const char *model_path) {
        FILE *fp = fopen(model_path, "rb");
        if (!fp) {
            logf_("打开 vision 模型失败: %s", model_path);
            return false;
        }
        fseek(fp, 0, SEEK_END);
        long len = ftell(fp);
        fseek(fp, 0, SEEK_SET);
        std::vector<char> buf((size_t)len);
        size_t rd = fread(buf.data(), 1, (size_t)len, fp);
        fclose(fp);
        if (rd != (size_t)len) {
            logf_("读取 vision 模型不完整");
            return false;
        }

        int ret = rknn_init(&ctx_, buf.data(), (uint32_t)len, 0, nullptr);
        if (ret < 0) {
            logf_("rknn_init 失败 ret=%d", ret);
            return false;
        }

        rknn_input_output_num io_num;
        ret = rknn_query(ctx_, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
        if (ret != RKNN_SUCC || io_num.n_input < 1 || io_num.n_output < 1) {
            logf_("rknn_query 失败 ret=%d", ret);
            return false;
        }

        rknn_tensor_attr in_attr;
        memset(&in_attr, 0, sizeof(in_attr));
        in_attr.index = 0;
        if (rknn_query(ctx_, RKNN_QUERY_INPUT_ATTR, &in_attr, sizeof(in_attr)) != RKNN_SUCC) {
            logf_("查询输入属性失败");
            return false;
        }
        if (in_attr.fmt == RKNN_TENSOR_NCHW) {
            in_h_ = in_attr.dims[2];
            in_w_ = in_attr.dims[3];
            in_c_ = in_attr.dims[1];
        } else {
            in_h_ = in_attr.dims[1];
            in_w_ = in_attr.dims[2];
            in_c_ = in_attr.dims[3];
        }

        rknn_tensor_attr out_attr;
        memset(&out_attr, 0, sizeof(out_attr));
        out_attr.index = 0;
        if (rknn_query(ctx_, RKNN_QUERY_OUTPUT_ATTR, &out_attr, sizeof(out_attr)) != RKNN_SUCC) {
            logf_("查询输出属性失败");
            return false;
        }
        // 输出形如 [196, 1024]：196 个视觉 token，每个 1024 维。
        n_tokens_ = (size_t)out_attr.dims[0];
        embed_len_ = (size_t)out_attr.dims[1];
        if (n_tokens_ == 0 || embed_len_ == 0 || n_tokens_ > 4096 || embed_len_ > 8192) {
            logf_("输出维度异常: %zu x %zu", n_tokens_, embed_len_);
            return false;
        }

        logf_("vision encoder 就绪: 输入 %dx%dx%d, 输出 %zu x %zu",
              in_w_, in_h_, in_c_, n_tokens_, embed_len_);
        return true;
    }

    void release() {
        if (ctx_) {
            rknn_destroy(ctx_);
            ctx_ = 0;
        }
    }

    size_t n_tokens() const { return n_tokens_; }
    size_t embed_len() const { return embed_len_; }
    int side() const { return in_w_; }

    // 把一张图片编码成 float32 embedding（调用方持有 result）。
    bool encode(const char *path, std::vector<float> *result) {
        int w = 0, h = 0, ch = 0;
        stbi_uc *pix = stbi_load(path, &w, &h, &ch, 3);
        if (!pix) {
            logf_("解码图片失败: %s (%s)", path, stbi_failure_reason());
            return false;
        }
        if (w <= 0 || h <= 0) {
            stbi_image_free(pix);
            return false;
        }

        const int side = in_w_;
        std::vector<uint8_t> out((size_t)side * side * 3, 127);

        // expand2square：短边居中，空白填中性灰 127（与建模时一致）
        const int sq = w > h ? w : h;
        const int x_off = (sq - w) / 2;
        const int y_off = (sq - h) / 2;

        // 最近邻 + 面积平均的简化版：对每个目标像素，映射回源方块坐标取点。
        // 448x448 = 20 万像素，CPU 上约 2~3ms，不是瓶颈。
        const double scale = (double)sq / (double)side;
        for (int dy = 0; dy < side; ++dy) {
            double sy = (dy + 0.5) * scale - 0.5 - y_off;
            int iy = (int)(sy + 0.5);
            if (iy < 0 || iy >= h) {
                iy = -1;  // 落在补边区域
            }
            for (int dx = 0; dx < side; ++dx) {
                double sx = (dx + 0.5) * scale - 0.5 - x_off;
                int ix = (int)(sx + 0.5);
                uint8_t *dst = &out[((size_t)dy * side + dx) * 3];
                if (iy < 0 || ix < 0 || ix >= w) {
                    dst[0] = dst[1] = dst[2] = 127;
                    continue;
                }
                const uint8_t *src = pix + ((size_t)iy * w + ix) * 3;
                dst[0] = src[0];
                dst[1] = src[1];
                dst[2] = src[2];
            }
        }
        stbi_image_free(pix);

        rknn_input inputs[1];
        memset(inputs, 0, sizeof(inputs));
        inputs[0].index = 0;
        inputs[0].type = RKNN_TENSOR_UINT8;
        inputs[0].fmt = RKNN_TENSOR_NHWC;
        inputs[0].size = (uint32_t)(side * side * 3);
        inputs[0].buf = out.data();

        int ret = rknn_inputs_set(ctx_, 1, inputs);
        if (ret < 0) {
            logf_("rknn_inputs_set 失败 ret=%d", ret);
            return false;
        }
        ret = rknn_run(ctx_, nullptr);
        if (ret < 0) {
            logf_("rknn_run 失败 ret=%d", ret);
            return false;
        }

        rknn_output outputs[1];
        memset(outputs, 0, sizeof(outputs));
        outputs[0].want_float = 1;
        ret = rknn_outputs_get(ctx_, 1, outputs, nullptr);
        if (ret < 0) {
            logf_("rknn_outputs_get 失败 ret=%d", ret);
            return false;
        }

        size_t n = n_tokens_ * embed_len_;
        size_t bytes = outputs[0].size;
        if (bytes < n * sizeof(float)) {
            logf_("encoder 输出偏小: %zu < %zu", bytes, n * sizeof(float));
            rknn_outputs_release(ctx_, 1, outputs);
            return false;
        }
        result->resize(n);
        memcpy(result->data(), outputs[0].buf, n * sizeof(float));
        rknn_outputs_release(ctx_, 1, outputs);
        return true;
    }

private:
    rknn_context ctx_ = 0;
    int in_w_ = 0, in_h_ = 0, in_c_ = 0;
    size_t n_tokens_ = 0, embed_len_ = 0;
};

// ── LLM 引擎 ───────────────────────────────────────────────────────
// rkllm 的 callback 是 C 函数指针 + userdata，这里用 userdata 指回当前
// 请求的上下文，把 token 增量直接推给 socket。
struct AskCtx {
    int fd = -1;
    std::atomic<bool> *abort_flag = nullptr;
    double t_start = 0.0;
    double t_first = 0.0;
    std::string text;
    int tokens = 0;
    bool closed = false;   // 客户端断开后不再尝试写
    // 本轮开始前已有的 KV cache 长度。中断后回退到这里，
    // 这样被中断的半截回答不会污染下一轮上下文。
    int kv_before = -1;
    // 本轮是否被用户中断。callback 在 pause 后会收到 FINISH，
    // 就地发 done 时必须带上这个标记，否则客户端不知道该丢上下文。
    std::atomic<bool> aborted{false};
    std::atomic<bool> done_sent{false};
};

class LlmEngine {
public:
    bool init(const char *model_path, int max_ctx, int max_new, int npu_cores) {
        RKLLMParam param = rkllm_createDefaultParam();
        param.model_path = model_path;
        param.top_k = 1;
        param.max_new_tokens = max_new;
        param.max_context_len = max_ctx;
        param.skip_special_token = true;
        param.is_async = false;
        // 1.3.0 里 ignore_eos_token 是新增字段；保持默认 false 才能正常遇到 EOS 停
        param.ignore_eos_token = false;
        param.extend_param.base_domain_id = 1;
        // 下面五group必须显式设：本板 vendor 的 librkllmrt 里
        // rkllm_createDefaultParam 并不填这些字段（实测全 0），
        // 全 0 会被校验拒绝（"number of enabled CPUs must be >= NPU cores"）。
        // 取值照搬官方 demo 的已验证配置：CPU4~7（A72 大核）跑推理。
        param.extend_param.embed_flash = 1;
        param.extend_param.n_batch = 1;
        param.extend_param.use_cross_attn = 0;
        param.extend_param.enabled_cpus_num = 4;
        param.extend_param.enabled_cpus_mask =
            (1u << 4) | (1u << 5) | (1u << 6) | (1u << 7);
        (void)npu_cores;   // NPU 核数由 driver 全局调度，模型侧无需指定

        // 1.3.0 开始 callback 是结构体，不再是裸函数指针
        memset(&cb_, 0, sizeof(cb_));
        cb_.result_callback = &LlmEngine::callback;
        cb_.result_userdata = this;

        double t0 = now_ms();
        int ret = rkllm_init(&handle_, &param, &cb_);
        if (ret != 0) {
            logf_("rkllm_init 失败 ret=%d (模型 %s)", ret, model_path);
            handle_ = nullptr;
            return false;
        }
        logf_("LLM 就绪: 加载 %.0f ms, ctx=%d, max_new=%d",
              now_ms() - t0, max_ctx, max_new);
        return true;
    }

    void release() {
        if (handle_) {
            rkllm_destroy(handle_);
            handle_ = nullptr;
        }
    }

    bool clear_kv() {
        if (!handle_) return false;
        return rkllm_clear_kv_cache(handle_, 1, nullptr, nullptr) == 0;
    }

    // 当前 KV cache 里的 token 数；拿不到返回 -1。
    int kv_size() const {
        if (!handle_) return -1;
        // n_batch 固定为 1，但接口要求数组按 n_batch 分配，多留几个更稳
        int sizes[8] = {0};
        if (rkllm_get_kv_cache_size(handle_, sizes) != 0) return -1;
        return sizes[0];
    }

    // 把 KV cache 回退到 mark（本轮开始前的长度）。
    // 只在“本轮被 paused（callback 返回 1）”之后有效，
    // 所以调用方必须确认推理已经停下来。
    bool rollback_kv(int mark) {
        if (!handle_ || mark < 0) {
            logf_("KV 回退跳过: handle=%p mark=%d", (void *)handle_, mark);
            return false;
        }
        int cur = kv_size();
        logf_("KV 回退尝试: mark=%d cur=%d", mark, cur);
        if (cur < 0) {
            logf_("KV 回退失败: rkllm_get_kv_cache_size 不可用");
            return false;
        }
        if (cur <= mark) return true;          // 没多出东西，无需回退
        int start[1] = {mark};
        int end[1] = {cur};
        int ret = rkllm_clear_kv_cache(handle_, 0, start, end);
        int after = kv_size();
        logf_("KV 区间清理 ret=%d, 清理后 cur=%d (目标 %d)", ret, after, mark);
        if (ret != 0) return false;
        // 有些实现返回成功但没真清，以实际长度为准
        return after < 0 || after <= mark + 1;
    }

    void abort() {
        if (handle_) rkllm_abort(handle_);
    }

    bool running() const { return handle_ && rkllm_is_running(handle_) != 0; }

    // 一轮推理。prompt 必须以 <image> 开头才会走多模态分支。
    bool ask(const std::string &prompt, const std::vector<float> *img,
             size_t n_img_tokens, int img_side, int keep_history,
             int max_new, int fd, std::atomic<bool> *abort_flag, AskCtx *out_ctx) {
        if (!handle_) return false;

        out_ctx->fd = fd;
        out_ctx->abort_flag = abort_flag;
        out_ctx->t_start = now_ms();
        out_ctx->t_first = 0.0;
        out_ctx->text.clear();
        out_ctx->tokens = 0;
        out_ctx->closed = false;

        RKLLMInput in;
        memset(&in, 0, sizeof(in));
        in.role = "user";
        in.enable_thinking = false;

        // prompt_input 必须活到 rkllm_run 返回
        std::string flat = prompt;
        for (size_t i = 0; i < flat.size(); ++i) {
            if (flat[i] == '\n') flat[i] = ' ';
        }

        if (img && !img->empty() && flat.find("<image>") != std::string::npos) {
            in.input_type = RKLLM_INPUT_MULTIMODAL;
            in.multimodal_input.prompt = (char *)flat.c_str();
            // 1.3.0 把图像标签从 RKLLMParam 挪进了 multimodal_input.image
            in.multimodal_input.image.image_embed = const_cast<float *>(img->data());
            in.multimodal_input.image.n_image_tokens = n_img_tokens;
            in.multimodal_input.image.n_image = 1;
            in.multimodal_input.image.image_start = "<|vision_start|>";
            in.multimodal_input.image.image_end = "<|vision_end|>";
            in.multimodal_input.image.image_content = "<|image_pad|>";
            in.multimodal_input.image.image_width = (size_t)img_side;
            in.multimodal_input.image.image_height = (size_t)img_side;
        } else {
            in.input_type = RKLLM_INPUT_PROMPT;
            in.prompt_input = flat.c_str();
        }

        RKLLMInferParam ip;
        memset(&ip, 0, sizeof(ip));
        ip.mode = RKLLM_INFER_GENERATE;
        ip.keep_history = keep_history;

        cur_ctx_ = out_ctx;
        // 记下本轮开始前的 cache 位置，供中断后回退
        out_ctx->kv_before = kv_size();
        int ret = rkllm_run(handle_, &in, &ip, nullptr);
        cur_ctx_ = nullptr;

        if (ret != 0) {
            logf_("rkllm_run 失败 ret=%d", ret);
            return false;
        }
        return true;
    }

    int current_tokens() const { return cur_ctx_ ? cur_ctx_->tokens : 0; }

private:
    static int callback(RKLLMResult *result, void *userdata, LLMCallState state) {
        // userdata 就是 init 时传进来的 this，比全局单例安全
        LlmEngine *self = static_cast<LlmEngine *>(userdata);
        AskCtx *ctx = self ? self->cur_ctx_ : nullptr;
        if (!ctx) return 0;

        if (state == RKLLM_RUN_NORMAL) {
            if (result && result->text && result->text[0]) {
                if (ctx->t_first == 0.0) {
                    ctx->t_first = now_ms() - ctx->t_start;
                    // 首个内容增量前先告知 UI 首字延迟
                    if (!ctx->closed) {
                        char buf[96];
                        snprintf(buf, sizeof(buf),
                                 "{\"ev\":\"ttft\",\"ms\":%.0f}", ctx->t_first);
                        if (!send_line(ctx->fd, buf)) ctx->closed = true;
                    }
                }
                ctx->text += result->text;
                ctx->tokens++;
                if (!ctx->closed) {
                    std::string msg = "{\"ev\":\"tok\",\"t\":\"";
                    msg += json_escape(result->text, strlen(result->text));
                    msg += "\"}";
                    if (!send_line(ctx->fd, msg)) ctx->closed = true;
                }
            }
            // 用户点了「停止」
            if (ctx->abort_flag && ctx->abort_flag->load()) {
                ctx->aborted.store(true);
                return 1;   // 1 = pause inference
            }
        } else if (state == RKLLM_RUN_FINISH) {
            // 竞态兜底：abort 可能在两次 NORMAL 之间到达，
            // 于是 FINISH 之前没有 NORMAL 回调把 aborted 置位。
            // 这里再查一次标志，否则客户端不知道要丢弃本轮。
            if (ctx->abort_flag && ctx->abort_flag->load()) {
                ctx->aborted.store(true);
            }
            if (!ctx->closed) {
                char buf[320];
                double total = (now_ms() - ctx->t_start) / 1000.0;
                bool ab = ctx->aborted.load();
                snprintf(buf, sizeof(buf),
                         "{\"ev\":\"done\",\"ms\":%.0f,\"ttft\":%.0f,"
                         "\"tokens\":%d,\"chars\":%zu%s%s}",
                         total * 1000.0, ctx->t_first, ctx->tokens,
                         ctx->text.size(),
                         ab ? ",\"aborted\":1" : "",
                         ab ? ",\"ctx\":\"reset\"" : "");
                if (!send_line(ctx->fd, buf)) ctx->closed = true;
            }
        } else if (state == RKLLM_RUN_ERROR) {
            logf_("推理过程报错");
            if (!ctx->closed) {
                if (!send_line(ctx->fd, "{\"ev\":\"error\",\"msg\":\"inference error\"}")) {
                    ctx->closed = true;
                }
            }
        }
        return 0;
    }

    static LlmEngine *instance() { return singleton_; }

public:
    static void set_instance(LlmEngine *p) { singleton_ = p; }

private:
    static LlmEngine *singleton_;
    LLMHandle handle_ = nullptr;
    RKLLMCallback cb_;
    AskCtx *cur_ctx_ = nullptr;
};

LlmEngine *LlmEngine::singleton_ = nullptr;

// ── 服务端 ─────────────────────────────────────────────────────────
static VisionEncoder g_vision;
static LlmEngine g_llm;
static std::mutex g_infer_mutex;          // 同一时刻只跑一轮推理
static std::atomic<bool> g_abort(false);
static std::atomic<bool> g_quit(false);
static std::atomic<int> g_clients(0);   // 活跃连接线程数，退出时等它们收尾
static int g_wake_pipe[2] = {-1, -1};   // self-pipe：信号处理里 write 唤醒 accept()

static void on_signal(int sig) {
    (void)sig;
    g_quit.store(true);
    // accept()/recv() 被信号打断后是否重启由 SA_RESTART 决定；显式写 self-pipe
    // 保证阻塞在 accept() 的主线程能被唤醒，而不是拖到 systemd TimeoutStopSec。
    if (g_wake_pipe[1] >= 0) {
        char c = 1;
        ssize_t w = write(g_wake_pipe[1], &c, 1);
        (void)w;
    }
}

// 处理一条请求；返回 false 表示连接应关闭
static bool handle_request(int fd, const std::string &line) {
    std::string cmd = json_str(line, "cmd", "");

    if (cmd == "ping") {
        return send_line(fd, "{\"ev\":\"pong\"}");
    }
    if (cmd == "abort") {
        // 不取推理锁：正在推理时也要能立刻响应
        g_abort.store(true);
        g_llm.abort();
        return send_line(fd, "{\"ev\":\"aborted\"}");
    }
    if (cmd == "clear") {
        std::lock_guard<std::mutex> lk(g_infer_mutex);
        bool ok = g_llm.clear_kv();
        return send_line(fd, ok ? "{\"ev\":\"cleared\"}"
                                : "{\"ev\":\"error\",\"msg\":\"clear failed\"}");
    }
    if (cmd == "kv") {
        // 诊断用：报告当前 KV cache 长度
        char buf[128];
        snprintf(buf, sizeof(buf), "{\"ev\":\"kv\",\"size\":%d,\"running\":%d}",
                 g_llm.kv_size(), g_llm.running() ? 1 : 0);
        return send_line(fd, buf);
    }
    if (cmd == "exit") {
        g_quit.store(true);
        return send_line(fd, "{\"ev\":\"bye\"}");
    }
    if (cmd != "ask") {
        return send_line(fd, "{\"ev\":\"error\",\"msg\":\"unknown cmd\"}");
    }

    // ── ask ──
    std::string prompt = json_str(line, "prompt", "");
    std::string image = json_str(line, "image", "");
    int keep_history = (int)json_int(line, "keep_history", 1);
    int max_new = (int)json_int(line, "max_new", 0);
    if (prompt.empty()) {
        return send_line(fd, "{\"ev\":\"error\",\"msg\":\"empty prompt\"}");
    }
    (void)max_new;   // 生成长度在 init 时定死，改它需要重启服务
    if (image.empty() && prompt.find("<image>") != std::string::npos) {
        return send_line(fd, "{\"ev\":\"error\",\"msg\":\"本轮没有图片，请打开摄像头重新取图\"}");
    }
    if (!image.empty() && prompt.find("<image>") == std::string::npos) {
        return send_line(fd, "{\"ev\":\"error\",\"msg\":\"图片问题缺少<image>标记，本轮未进行推理\"}");
    }

    // 图片编码不占推理锁，可以和上一轮的收尾重叠；
    // 但 vision encoder 是独占的，仍需单独串行化。
    static std::mutex enc_mutex;
    std::vector<float> embed;
    size_t n_tok = 0;
    int side = IMG_SIDE;
    bool has_img = false;

    if (!image.empty() && prompt.find("<image>") != std::string::npos) {
        std::lock_guard<std::mutex> lk(enc_mutex);
        double t0 = now_ms();
        if (g_vision.encode(image.c_str(), &embed)) {
            has_img = true;
            n_tok = g_vision.n_tokens();
            side = g_vision.side();
            logf_("编码 %s: %zu tok, %.0f ms", image.c_str(), n_tok, now_ms() - t0);
        } else {
            logf_("编码失败，本轮不进行推理，禁止复用旧图回答");
            return send_line(fd, "{\"ev\":\"error\",\"msg\":\"图像编码失败，本轮未进行推理，请重新取图\"}");
        }
    }

    std::lock_guard<std::mutex> lk(g_infer_mutex);
    g_abort.store(false);

    if (has_img) {
        // New pixels define a new visual scene. Repeated <image> prompts must
        // not inherit the previous image's token/linear-attention history.
        // Keep history after this run so text-only follow-ups can use this image.
        int before = g_llm.kv_size();
        if (!g_llm.clear_kv()) {
            return send_line(fd, "{\"ev\":\"error\",\"msg\":\"旧画面上下文清理失败，本轮已停止\"}");
        }
        int after = g_llm.kv_size();
        unsigned long long fingerprint = static_cast<unsigned long long>(embedding_hash(embed));
        logf_("新画面上下文刷新: kv=%d->%d embed_hash=%016llx", before, after, fingerprint);
        char event[256];
        snprintf(event, sizeof(event),
                 "{\"ev\":\"image\",\"kv_reset\":1,\"kv_before\":%d,\"kv_after\":%d,"
                 "\"embedding_hash\":\"%016llx\"}", before, after, fingerprint);
        if (!send_line(fd, event)) return false;
    }

    // 本轮开始前先记 cache 长度；它也是“丢弃本轮”时的回退点。
    int kv_mark = g_llm.kv_size();

    AskCtx ctx;
    if (!g_llm.ask(prompt, has_img ? &embed : nullptr, n_tok, side,
                   keep_history, max_new, fd, &g_abort, &ctx)) {
        return send_line(fd, "{\"ev\":\"error\",\"msg\":\"run failed\"}");
    }

    bool aborted = g_abort.load();
    if (aborted) {
        // 等推理真正停下再动 cache（rkllm 要求 paused 后才能操作）
        int spin = 0;
        while (g_llm.running() && spin++ < 200) usleep(5000);   // 最多 1s

        // 这块 vendor 库的 rkllm_clear_kv_cache 区间模式直接返 -1
        // （实测 ret=-1, 清理后长度不变），只能整段清。
        // 整段清的代价：更早的正常上下文也一并丢掉，
        // 所以这里告诉客户端“需要重放历史”，由它下轮自己拼。
        bool ok = g_llm.clear_kv();
        logf_("中断: 已丢弃本轮(mark=%d), 整段清 cache=%s, 客户端需重放历史",
              kv_mark, ok ? "成功" : "失败");

        // callback 在 pause 后会收到 FINISH 并就地发一条 done（带 aborted），
        // 所以这里只在它没发过时补发，避免客户端收到两条 done。
        if (!ctx.closed && !ctx.done_sent.load()) {
            char buf[160];
            snprintf(buf, sizeof(buf),
                     "{\"ev\":\"done\",\"aborted\":1,\"ttft\":%.0f,"
                     "\"tokens\":%d,\"ctx\":\"reset\"}",
                     ctx.t_first, ctx.tokens);
            if (!send_line(fd, buf)) ctx.closed = true;
        }
    }
    return true;
}

static void *client_thread(void *arg) {
    int fd = (int)(intptr_t)arg;
    g_clients.fetch_add(1);
    std::string buf;
    char chunk[8192];
    while (!g_quit.load()) {
        ssize_t n = recv(fd, chunk, sizeof(chunk), 0);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            break;
        }
        buf.append(chunk, (size_t)n);
        size_t nl;
        while ((nl = buf.find('\n')) != std::string::npos) {
            std::string line = buf.substr(0, nl);
            buf.erase(0, nl + 1);
            if (line.empty()) continue;
            if (!handle_request(fd, line)) {
                close(fd);
                g_clients.fetch_sub(1);
                return nullptr;
            }
        }
    }
    close(fd);
    g_clients.fetch_sub(1);
    return nullptr;
}

static void usage(const char *prog) {
    fprintf(stderr,
            "用法: %s [选项]\n"
            "  --llm PATH      rkllm 模型 (默认 %s)\n"
            "  --vision PATH   vision rknn 模型 (默认 %s)\n"
            "  --sock PATH     Unix socket (默认 %s)\n"
            "  --ctx N         上下文长度 (默认 4096)\n"
            "  --max-new N     单轮生成上限 (默认 256)\n"
            "  --cores N       NPU 核数提示 (默认 2)\n",
            prog,
            "/data/rkllm_qwen35/Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm",
            "/data/rkllm_qwen35/Qwen3.5-0.8B_vision_rk3576.rknn",
            RKLLM_SERVICE_DEFAULT_SOCK);
}

int main(int argc, char **argv) {
    std::string llm_path = "/data/rkllm_qwen35/Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm";
    std::string vis_path = "/data/rkllm_qwen35/Qwen3.5-0.8B_vision_rk3576.rknn";
    std::string sock_path = RKLLM_SERVICE_DEFAULT_SOCK;
    int ctx_len = 4096;
    int max_new = 256;
    int cores = 2;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char *name) -> std::string {
            if (i + 1 >= argc) {
                fprintf(stderr, "%s 缺少参数\n", name);
                exit(2);
            }
            return argv[++i];
        };
        if (a == "--llm") llm_path = next("--llm");
        else if (a == "--vision") vis_path = next("--vision");
        else if (a == "--sock") sock_path = next("--sock");
        else if (a == "--ctx") ctx_len = atoi(next("--ctx").c_str());
        else if (a == "--max-new") max_new = atoi(next("--max-new").c_str());
        else if (a == "--cores") cores = atoi(next("--cores").c_str());
        else if (a == "-h" || a == "--help") { usage(argv[0]); return 0; }
        else { fprintf(stderr, "未知参数: %s\n", a.c_str()); usage(argv[0]); return 2; }
    }

    if (pipe(g_wake_pipe) < 0) {
        fprintf(stderr, "创建唤醒管道失败: %s\n", strerror(errno));
        return 1;
    }
    for (int i = 0; i < 2; ++i) fcntl(g_wake_pipe[i], F_SETFL, O_NONBLOCK);

    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    LlmEngine::set_instance(&g_llm);

    double t0 = now_ms();
    if (!g_llm.init(llm_path.c_str(), ctx_len, max_new, cores)) {
        logf_("LLM 初始化失败，退出");
        return 1;
    }
    if (!g_vision.init(vis_path.c_str())) {
        logf_("vision encoder 初始化失败，退出");
        g_llm.release();
        return 1;
    }
    logf_("全部就绪，总加载 %.0f ms", now_ms() - t0);

    int srv = socket(AF_UNIX, SOCK_STREAM, 0);
    if (srv < 0) {
        logf_("socket 创建失败: %s", strerror(errno));
        return 1;
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sock_path.c_str());
    unlink(sock_path.c_str());
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        logf_("bind %s 失败: %s", sock_path.c_str(), strerror(errno));
        return 1;
    }
    // 让桌面普通用户也能连（服务以 root 启动时 socket 默认 0755 但属主 root）
    chmod(sock_path.c_str(), 0666);
    if (listen(srv, 8) < 0) {
        logf_("listen 失败: %s", strerror(errno));
        return 1;
    }
    logf_("监听 %s", sock_path.c_str());

    while (!g_quit.load()) {
        // poll 而非裸 accept：信号只写 self-pipe，让 poll 立即返回并看到 g_quit。
        struct pollfd pfds[2];
        pfds[0].fd = srv;
        pfds[0].events = POLLIN;
        pfds[1].fd = g_wake_pipe[0];
        pfds[1].events = POLLIN;
        int pr = poll(pfds, 2, -1);
        if (pr < 0) {
            if (errno == EINTR) continue;
            logf_("poll 失败: %s", strerror(errno));
            break;
        }
        if (g_quit.load()) break;
        if (pfds[1].revents & POLLIN) {
            char drain[64];
            while (read(g_wake_pipe[0], drain, sizeof(drain)) > 0) {}
            break;
        }
        if (!(pfds[0].revents & POLLIN)) continue;
        int fd = accept(srv, nullptr, nullptr);
        if (fd < 0) {
            if (errno == EINTR) continue;
            logf_("accept 失败: %s", strerror(errno));
            break;
        }
        pthread_t th;
        if (pthread_create(&th, nullptr, client_thread, (void *)(intptr_t)fd) == 0) {
            pthread_detach(th);
        } else {
            close(fd);
        }
    }

    close(srv);
    // 先等所有连接线程退出，再释放引擎。否则仍在 rkllm_run() 里的线程
    // 会回调到已析构的 handle_ / 栈上的 AskCtx，造成段错误。
    for (int i = 0; i < 600 && g_clients.load() > 0; ++i) usleep(10000);  // 最多 6s
    if (g_clients.load() > 0) {
        logf_("仍有 %d 个连接线程未退出，强制收尾", g_clients.load());
    }
    unlink(sock_path.c_str());
    g_llm.release();
    g_vision.release();
    if (g_wake_pipe[0] >= 0) close(g_wake_pipe[0]);
    if (g_wake_pipe[1] >= 0) close(g_wake_pipe[1]);
    logf_("已退出");
    return 0;
}
