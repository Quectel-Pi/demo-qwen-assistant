#!/usr/bin/env python3
"""Build a faithful report without equating transport success to answer accuracy."""
import csv
import json
from pathlib import Path
import re
import statistics

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/Path((ROOT/'reports/full-test/latest-local-path.txt').read_text().strip())
main=json.loads((BASE/'device/summary.json').read_text())
follow=json.loads((BASE/'followup/summary.json').read_text())
raw=main['records']+follow['records']
by_id={r['id']:r for r in raw}
requests=[r for r in main['records'] if 'wall_ms' in r]
text=[r for r in requests if not r.get('image_sent')]
vision=[r for r in requests if r.get('image_sent') and r['id']!='missing_image']
repeats=[r for r in raw if r['id'].startswith('repeat_')]


def normalize(text):
    return ''.join(ch.lower() for ch in text if ch.isalnum())


def edit_distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        new=[i]
        for j,y in enumerate(b,1):
            new.append(min(new[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=new
    return row[-1]

acoustic=by_id['speaker_microphone_acoustic_loop']
a,b=normalize(acoustic['expected']),normalize(acoustic['transcription'])
cer=edit_distance(a,b)/len(a)
summary={
 'records':len(raw),
 'raw_counts':{s:sum(r['status']==s for r in raw) for s in ['PASS','FAIL','MANUAL']},
 'notes':['Raw counts mix transport, answer quality and test setup; not a model accuracy rate.',
          'Initial keyboard check timed out before Chinese input; retry passed.',
          'Initial stop check never sent abort because the answer was shorter than the trigger; retry passed.',
          'Camera and speech PASS means transport/playback completed, not whole-answer semantic correctness.'],
 'text':{'n':len(text),'ttft_median_ms':statistics.median(r['ttft_ms'] for r in text),
         'wall_min_ms':min(r['wall_ms'] for r in text),'wall_max_ms':max(r['wall_ms'] for r in text)},
 'vision':{'n':len(vision),'ttft_median_ms':statistics.median(r['ttft_ms'] for r in vision),
           'wall_min_ms':min(r['wall_ms'] for r in vision),'wall_max_ms':max(r['wall_ms'] for r in vision)},
 'memory_min_sample_MiB':min(r['resource']['MemAvailable_MiB'] for r in main['records']),
 'stability':{'requests':len(repeats),'completed':sum(bool(r.get('tokens')) and not r.get('errors') for r in repeats),
              'ui_RSS_MiB_samples':[r['resource']['ui_RSS_MiB'] for r in repeats]},
 'acoustic_asr':{'expected':acoustic['expected'],'actual':acoustic['transcription'],
                 'normalization':'Remove punctuation/space and lowercase; do not map A to 一',
                 'character_errors':edit_distance(a,b),'reference_characters':len(a),'CER':cer},
 'runtime_scope':{'main_test_seconds':main['seconds'],'followup_test_seconds':follow['seconds'],
                  'not_covered':['24h soak','power-cycle recovery','network isolation',
                                 'human hands-free dictation and voice interruption','subjective image truth/audibility']}
}
(BASE/'SUMMARY.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
with (BASE/'SCENARIOS.csv').open('w',newline='') as f:
    fields=['run','id','category','status','quality_pass','note','prompt','answer','wall_ms','ttft_ms','tokens','spoken_completed','image_sent']
    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
    for run,records in [('main',main['records']),('followup',follow['records'])]:
        for r in records:writer.writerow({'run':run,**{k:r.get(k) for k in fields if k!='run'}})

report=f'''# Quectel Pi M2 当前模型综合真机测试

## 结论

设备交互链路可用，但当前模型回答质量不够稳定，不能把“请求完成”当成“回答正确”。
系统键盘、输入显示、摄像头采集、TTS/ASR软件链路、真实停止/恢复和重连通过补测。
发现明确的数学错误、无关拒答、名字记忆失败、格式不遵循、中英混杂与跑题，以及声学识别错字。

测试仅针对当前已部署模型，不切回原版，不改 max-new/context/量化或模型权重。
没有进行公平的原版/BF16 A/B，因此不推断错误究竟来自量化、权重编辑、提示模板还是运行时。

## 条件和范围

- 板卡：Quectel Pi M2，RK3576，内存约3900 MiB。
- 模型：Qwen3.5-0.8B-MIT，W4A16；LLM 与 vision 均用当前已部署文件。
- 运行时：rkllm 1.3.1；context 4096，max-new 256，NPU 2核；保持当前服务配置。
- UI：用户w，原生Wayland，系统键盘/IBus；正式UI默认自动朗读开启。
- 主轮 {main['seconds']:.1f} 秒，补测 {follow['seconds']:.1f} 秒，另有取证与分析时间。
- 共 {len(raw)} 条检查记录（44主轮+11补测；包含重复项、框架检查和人工复核项），不是55道标准能力题。
- 除特定朗读/并发用例，质量题在测试实例中临时静音，以控制测试时间；没有改正式UI默认朗读配置。
- 场景由正式 ChatApp 的发送/接收/渲染路径驱动；键盘通过内核输入事件；不是把 socket 回答冒充UI测试。
- 设备时钟为2026-04-21，主机运行目录按2026-11-09命名。耗时用monotonic计算，不受时钟日期差影响。

## 已验证的功能

1. 系统键盘：GdkWaylandDisplay、uid1001、不构造应用键盘；真实输入 hello 和中文“你好”。初次窗口错过中文测试，补测通过。
2. 长文本布局：固定长中文、640字符左右无空格长链接、多行40行代码的Pango布局均未超出Label分配；这些是明确标注的布局样本，不是模型生成结果。
3. 摄像头：真实 `/dev/video73` 采集，2秒内帧序号增加 {by_id['camera_preview']['frame_delta']}；这不是UI显示帧率测量。
4. 图像上下文：首轮实时图像描述，关闭摄像头后追问背景；后续 `image_sent=false`，确实没有重新传图。
5. 异常输入：空问题和未知命令返回error，不存在图片路径触发 `image encode failed` 后降级回答，没有服务崩溃。
6. 真实停止：补测收到首个token就停止，实际返回 `aborted=1, ctx=reset`，停止后可回答“继续测试。”。
7. 连接恢复：只重连UI socket，不重启服务；恢复后回答“北京”。
8. TTS：生成 {by_id['tts_synthesis']['samples']} 个采样点，{by_id['tts_synthesis']['sample_rate']} Hz，有效RMS={by_id['tts_synthesis']['rms']:.4f}。
9. 数字语音回环：TTS音频直接送ASR，识别为“{by_id['tts_asr_roundtrip']['transcription']}”。这是软件回环，不是真人语音准确率。
10. 麦克风：实际 hw:0,0 采集 {by_id['mic_capture']['samples']} 个采样点；声学回环VAD检测到语音。
11. 自动朗读：实际UI回答完成播放；同时开启摄像头与朗读也完成，测试日志没有此前的bad_alloc/terminate崩溃。
12. 稳定性：5次重复请求全部完成、无服务错误，UI RSS采样均为 {repeats[0]['resource']['ui_RSS_MiB']} MiB；不代表长时间无内存泄漏。

两轮测试前后服务PID均为4603，没有重启推理服务。测试窗口已退出，正式UI已恢复。

## 回答质量：明确通过的样本

- 首都：只输出“北京”。
- 指令：只输出“OK”。
- JSON：实际输出 `{{"status": "ok", "count": 3}}`，可解析且字段匹配。
- 翻译：“早上好”输出 Good morning。
- 简单代码：`add(a,b): return a+b`；AST安全白名单通过，3组输入验证通过，未执行导入/文件/网络类生成代码。
- 爱好记忆：记住“我喜欢骑行”后可回答“骑行！”。
- 图像颜色追问：宇航员图追问瓶子颜色，输出“绿色。”。
- 鼓励：“{by_id['comfort']['answer']}”。

以上只是这些固定样本通过，不代表相应能力普遍可靠。

## 回答质量：失败和不稳定项

### 数学，2/2错误

- `12×13`：期望156，实际“{by_id['multiply']['answer']}”。
- 3个苹果再买2个：期望5，实际“{by_id['simple_word_math']['answer']}”。

### 开放式闲聊出现无关拒答

“你能帮我做什么？”实际回答：

> {by_id['capabilities']['answer']}

不应据此宣称根因就是缺system prompt；本轮没有证明这个因果关系。

### 上下文记忆并非全通过

声明“我叫小林，我最喜欢骑行”后，问名字只得到“{by_id['recall_name']['answer']}”；但问爱好能答对。
清空后不再返回小林，清空路径通过。图像追问通过不等于任意多轮记忆可靠。

### 长回答/看图会跑题和中英混杂

- 要求分三点、约150字介绍骑行，实际只回一句话；布局测试通过不能掩盖指令不遵循。
- 静态宇航员图虽命中“宇航员”，随后出现 “I'm not an AI, so I can't answer this question” 等无关英文。
- 实时画面+朗读的并发用例虽播放成功，但中文描述后扩展成大段 Human body ... 英文。
- 因此原始记录中的 static_image / camera_and_speech PASS，只覆盖关键词/链路，不应解读为整体回答质量通过。

### 格式不严格遵循

- 要求小写 `pong`，实际“{by_id['after_reconnect']['answer']}”；是格式失败，不是断线恢复失败。
- 5次要求只输出“测试成功”，实际均为“{repeats[0]['answer']}”；语义接近但不遵循“只输出”。

### 声学回环：听得见不等于识别准确

- 扬声器实际播放：“{acoustic['expected']}”
- 麦克风采集后ASR：“{acoustic['transcription']}”
- 标点空格归一化后字符错误 {edit_distance(a,b)}/{len(a)}，CER约 {cer:.1%}；仅此1条合成语句。
- 录音见 [acoustic-loopback.wav](followup/acoustic-loopback.wav)。声学链路有信号，但识别错字必须保留为问题，不能以“播放耗时正常”判全通过。

## 性能与资源

- 纯文本 {len(text)} 条请求：服务TTFT中位数 {summary['text']['ttft_median_ms']:.0f} ms；UI发送到done约 {summary['text']['wall_min_ms']/1000:.2f}–{summary['text']['wall_max_ms']/1000:.2f} s。
- 含图 {len(vision)} 条有效请求：服务TTFT中位数 {summary['vision']['ttft_median_ms']:.0f} ms；UI整轮约 {summary['vision']['wall_min_ms']/1000:.2f}–{summary['vision']['wall_max_ms']/1000:.2f} s。
- 以上done耗时不含答案结束后的剩余朗读时间；服务TTFT也不等同包含摄像头抓帧/编码的整链路首字时间。
- 测试采样可用内存最低 {summary['memory_min_sample_MiB']} MiB；不是连续测得的真实峰值。
- 没有24小时老化、断电重启、拔网线测试。没有真人免提输入/真人语音打断测试。

## 演示建议

优先展示已预演的短闲聊、实际摄像头画面、明确颜色/背景追问和系统键盘输入。
目前不宜把数学、任意名字记忆、严格格式指令、完全可靠的免提语音识别作为演示卖点。
画面描述是否完全符合现场、现场距离的扬声器音量仍需人工核对。

## 取证文件

- [结构化汇总](SUMMARY.json)
- [全部检查CSV](SCENARIOS.csv)
- [主轮原始数据](device/results.jsonl)
- [补测原始数据](followup/results.jsonl)
- [系统键盘补测截图](followup/keyboard-retry.png)
- [摄像头实时画面与UI](device/live_describe.png)
- [关闭摄像头后的追问](device/live_followup.png)
- [自动朗读场景](device/ui_auto_speech.png)
- [长中文布局](followup/long_chinese.png)
- [长链接布局](followup/long_ascii.png)
- [40行代码布局](followup/multiline_code.png)

原始脚本：`tests/device_full_test.py`、`tests/device_followup_test.py`、`tests/device_test_supervisor.py`。
原始55条记录的PASS/FAIL混合了多种判据，所以不提供一个误导性的“总准确率”。
'''
(BASE/'REPORT.md').write_text(report)
print(BASE/'REPORT.md')
print(json.dumps(summary,ensure_ascii=False,indent=2))
