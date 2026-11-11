# GNOME 系统键盘修复与验证

## 修复

- `deploy/ui/rkchat/rkchat.py`：默认 keyboard=system，不导入/构造 vkbd，不连接自带键盘的焦点事件。`--keyboard builtin` 才允许回退。启动后预聚焦 GtkEntry，避免首次触摸只获取焦点。
- `deploy/ui/rkchat/desktop_session.py`：发现当前已登录图形会话；root 启动时经 runuser 切到实际桌面用户，不读取 root 的 dconf。当前板端为 w/1001/Wayland，使用 GDK_BACKEND=wayland、GTK_IM_MODULE=wayland；IBus 由 GNOME 内部处理。开启用户会话的系统 OSK，保留已有输入源并按需补充 us/libpinyin。
- `deploy/ui/rkchat/run.sh`：统一启动入口；disabled 的推理服务允许手动 start，不再错误地把 enabled 当运行条件。
- `deploy/DEPLOY.sh`：部署和校验清单加入 desktop_session.py。
- `deploy/README.md`：撤销旧的强制 X11/root 启动说明，记录系统键盘与显式后备模式。

## 实测证据

- 当前 GNOME Shell 48.7 的 Wayland 键盘逻辑监听 compositor `input-panel-state`。旧版 root/X11/GTK_IM_MODULE=ibus 绕开这条链路，且应用始终构造并弹出自带键盘。
- 最终启动：`[run] desktop=w uid=1001 backend=wayland im=wayland`；`[rkchat] keyboard=system backend=GdkWaylandDisplay uid=1001`。
- 键盘测试使用实际内核输入设备事件，而非调用 Button.clicked 或直接 Entry.set_text；临时 uinput 设备均在 finally 中销毁。
- 导入正式 ChatApp 的验证程序证实 `builtin_keyboard=false`。测试程序只替换语音预加载避免同时加载第二份模型，不改键盘和输入路径。
- 实际点 GNOME OSK 的 h/e/l/l/o 按键后：committed_text=h→he→hel→hell→hello。
- 切换 IBus libpinyin 后实际点系统键盘拼音按键：preedit=年→你→你和→你哈→你好；空格选择后 committed_text 包含“你好”。详见 input-verification.jsonl 和 system-pinyin-candidates.png。
- 首次触摸即可弹 OSK，截图 single-touch.png；不是只确认 GSettings=true。
- 正式应用最终截图：输入框表面 x=333,y=472,width=858,height=50，下沿522；系统键盘顶部约533，字母按键从584开始，所以输入框在键盘上方，没有被遮住。`final-ui-input.png` 和 `final-ui-answer.png`。
- 正式 UI 用实际输入事件输入“你好”并触摸发送，界面出现新对话气泡，进程保持运行且日志无 Traceback/Error；没有用 socket 代替 UI 测试。
- 单元测试7/7通过；Python AST、shell语法通过。
- 部署的 rkchat.py/desktop_session.py/run.sh MD5 均与本地一致。

## 状态

- 正式 UI 留在设备上运行，用户 w，PID 422626（PID 随重启变化）。常驻推理服务 active，模型和 max-new 参数没有变动。
- 自动朗读默认开启不变，麦克风仍由“语音”按钮控制。
- 备份 `/userdata/backup-system-keyboard-20260420-232815/ui/`；同目录保存桌面设置快照。
- 没有改 GDM 自动登录、没有修改 GNOME 安全策略或启用 unsafe-mode，没有安装新软件，没有 commit/push。
- 图像验证采用 GNOME 合成器 Screenshot API，临时截图客户端使用独立 D-Bus 名称，不替换已有客户端，不修改截图权限。因模型不支持看图，没有声称主观外观已审定；记录的是截图像素坐标、OCR 与实际输入事件证据。
