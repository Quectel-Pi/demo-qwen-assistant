# 文档截图（2026-11-11）

用于飞书文档「Qwen3.5 多模态语音助手」的界面截图，均来自设备真实运行，
设备侧原始目录：`/userdata/rkchat-shots3`、`/userdata/rkchat-osk-20261111`。

| 文件 | 对应内容 |
|---|---|
| `01-main-ui.png` | 主界面（状态栏 / 对话区 / 底部按钮） |
| `02-text-chat.png` | 纯文字问答 |
| `03-camera-preview.png` | 点亮摄像头后的实时预览 |
| `04-vision-answer.png` | 看图问答回答 |
| `07-osk-open.png` | 触摸输入框唤起的 GNOME 系统键盘 |

采集方式：以桌面用户 `w` 启动一个静音（`_speak_async` 置空）的 ChatApp 实例，
真实走 socket 推理；截图走 GNOME `org.gnome.Shell.Screenshot` D-Bus 接口；
采集期间临时关闭通知横幅，结束后恢复并重启正式 UI。
