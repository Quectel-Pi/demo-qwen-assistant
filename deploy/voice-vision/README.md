# 语音后端源码与设备选择

本目录归档设备 `/data/voice-vision/talk.py`。ASR、TTS、VAD 模型和 sherpa/ONNX
依赖仍使用设备原有 `/data/voice-vision/models`、`py`，没有更换模型。

本次后端改动仅为：`Speaker.play()` 用 `check=True` 调用 aplay，使播放失败能
传回 UI；之前非零退出码也可能被误当作播放成功。原有扬声器时钟速率补偿保持不变。

## 麦克风默认策略

选择逻辑在 `ui/rkchat/voice.py`，不是在模型里：

1. 显式 `MIC_DEVICE` 环境变量优先。
2. 否则选 `/proc/asound/card*/usbid` 标识的 USB 采集设备，使用稳定声卡名：
   当前设备是 `plughw:CARD=U1080P,DEV=0`（UGREEN 摄像头麦克风）。
3. 无 USB 麦克风时回退 `hw:0,0`。扬声器继续使用既有 `hw:0,0`。

ES8389 同一声卡录放音共用时钟，不能把并发录放音当作可靠的全双工。
采集和播放在不同声卡时允许语音打断；同卡回退时自动使用半双工，仍可点击
UI 的停止按钮打断。不要强行同时打开该声卡的 arecord 和 aplay。

显式选设备示例：

```sh
MIC_DEVICE=plughw:CARD=U1080P,DEV=0 ./run.sh
# 无USB麦克风，明确使用板载输入（半双工）
MIC_DEVICE=hw:0,0 ./run.sh
```

采集启动后先等到真实 PCM，再丢弃短暂上电瞬态，随后才提示“在听”。
不会以“已创建 arecord 进程”冒充“麦克风已经可用”。

## 未改变的配置

没有修改声卡永久混音器增益。-6 dB 增益试验未改善识别，因此已恢复原值。
没有修改 DTS、MCLK、模型权重或解码参数。

USB 路由对照中录音削波比例从约 2.1% 降至 0%；ASR 仍可能有错字。
消除录放音争用不是提高模型准确率的保证，具体声音效果仍取决于环境和距离。
