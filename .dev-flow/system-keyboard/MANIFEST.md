# 系统键盘修复

- 用户授权：设备上运行的UI还是弹出内置的键盘而不是系统键盘，请一次性修复。
- 方案：默认不构造应用虚拟键盘；在当前登录桌面用户的原生 Wayland 会话运行，使用 GTK Wayland 输入法协议请求 GNOME OSK，中文由 IBus libpinyin 提供。
- 不改模型、推理参数、常驻推理服务或桌面自动登录。
- 保留应用自带键盘仅作显式 `--keyboard builtin` 的回退选项，不做自动回退。
- 设备证据：GNOME 48.7，桌面会话 user=w uid=1001 Type=wayland。标准 GTK 原生 Wayland 窗口接受真实内核触摸事件后系统键盘实际出现，截图 `logs/keyboard-fix/probe-touch.png`。
- 验证：静态语法、启动参数、真实 Wayland UI、触摸与 IBus 输入、系统键盘截图、输入框可见、退出清理；最终保持 UI 运行。
- reasoning：保留当前会话设置；未强制切模型或思考档位。
- 提交：未请求，不 commit/push。
- 实现：PASS，rkchat.py 默认不构造自带键盘；desktop_session.py/run.sh 统一真实桌面用户与原生输入法。
- build：PASS，7/7 单元测试 + Python/shell 语法。
- runtime：PASS，首次触摸弹 GNOME OSK；真实按键输入 hello、IBus 候选上屏你好；输入框下沿522 < 键盘顶部533，无遮挡；正式 UI 留在设备运行，推理服务 active。
- 结果报告：`logs/keyboard-fix/RESULT.md`；截图和输入事件证据保存在同目录。
- Open Items：无本次修复阻塞项；保留用户自行体验，未宣称主观观感已验证。
