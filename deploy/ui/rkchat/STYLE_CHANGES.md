# 任务C：style.css 视觉美化 — 改动说明

文件：`deploy/ui/rkchat/style.css`（324 行 → 419 行）
部署位置：`/data/rkllm-chat/ui/rkchat/style.css`（已同步，md5 一致）
约束：只改 CSS，未触碰 `rkchat.py`（任务B 的 ui-fixer 负责）

---

## 一、先说一个发现：真正的「文字显示不全」根因不在 CSS

按任务描述「优化 UI 上文字显示不全」，我先做了实际测量（板端 GTK 内省 + Gdk 直取像素），
结论是 **CSS 层面的文字并没有被裁切**。所有 label 的 `alloc.height >= layout.pixel_height`，
6 条测试气泡（含超长英文单词、长 URL、多行代码块）全部通过。

真正的问题是**布局**：

```
默认态（预览隐藏）：
  Box(topbar)        @0,0    1280x61   vis=True
  ScrolledWindow     @0,61   1280x660  vis=True   <- 对话区
  Box(preview_box)   @0,240  1280x481  vis=False  <- 隐藏但仍占位！
  Box(inputbar)      @0,721  1280x79   vis=True
```

截图实测：**y=240..700 这 460px 是一片纯背景色，非背景像素 = 0**。
即对话区名义上有 660px，实际内容只能画在顶部 180px 内，剩余空间被隐藏的预览控件吃掉。

根因是 `rkchat.py` 里：

```python
self.preview_box.show_all()
self.preview_box.set_no_show_all(True)      # <- 这行让 show_all() 再也无法生效
...
root.pack_start(self.preview_box, True, True, 0)   # expand=True 持续占位
```

`no_show_all=True` 会让 `show_all()` 对该控件无效（我实测 `app.preview_box.show_all()` 后
`get_visible()` 仍为 False），但 `pack_start(expand=True)` 的占位不受影响。

**这需要改 rkchat.py，不在我的授权范围内，已写进下面「给 ui-fixer 的需求」。**

CSS 侧我做了能做的：压缩纵向 padding，把对话区从 660px 抠到 **669px**（+9px）。

---

## 二、CSS 改动清单

### 1. 建立色板与设计约定（文件头注释）
原来颜色是散落的字面量，同类元素用了近似但不一致的值（如 `#22262e` vs `#23272d`）。
现在固化 4 层背景 + 3 档圆角 + 4 档字号，写在文件头，后续改动有据可依。

| 层 | 旧 | 新 | 用途 |
|---|---|---|---|
| layer0 | `#14161a` | `#14161a` | 窗口、对话区底 |
| layer1 | `#1c1f25` | `#1b1e24` | 顶栏、输入栏 |
| layer2 | `#22262e` / `#23272d` | `#23272f` | 气泡、输入框（合并了两个近似值） |
| layer3 | `#2a2f38` / `#2a2e36` | `#2c313a` | 按钮、滚动条 |
| border | `#2a2e36` / `#333842` | `#333944` / `#2a2f38` | 统一为「边框/分隔」两档 |

### 2. 字号层级收敛为 4 档：19 / 15 / 14 / 12
旧版正文是 17px，在 1280x800 上偏大（1700 字的长回答一屏显示不满）。
正文降到 15px，让同样高度多显示约 2 行；标题 19px、按钮 14px、元信息 12px 维持层级。

### 3. 纵向空间优化（1280x800 的关键）
| 元素 | 旧 | 新 | 省下 |
|---|---|---|---|
| topbar padding | `10px 18px` | `8px 16px` | 4px |
| topbar 总高 | 61px | **58px** | 3px |
| inputbar padding | `12px 18px` | `10px 16px` | 4px |
| inputbar 总高 | 79px | **73px** | 6px |
| 对话区可用高 | 660px | **669px** | +9px |

### 4. 气泡：不对称圆角 + AI 气泡加描边
```css
.bubble-user { border-top-right-radius: 8px; }   /* 右上角切角，指向右侧 */
.bubble-ai   { border-top-left-radius: 8px; }    /* 左上角切角，指向左侧 */
.bubble-ai   { border: 1px solid #2c313a; }      /* 深底上给个边界，否则色块发飘 */
```
三种气泡（user/ai/err）用同一组 `border-radius/padding/font-size` 基线，只在颜色上分叉，
避免原来 user 和 ai 圆角一致、error 却单独用 12px 的不一致。

### 5. 交互态补全
- 输入框 focus：描边换 `#3b82f6` + `box-shadow: 0 0 0 2px rgba(59,130,246,.22)` 外圈
  （原来只有 1px 描边变色，深色底上几乎看不出）
- 按钮 `:focus` 焦点环（接鼠标/键盘时可见）
- 按钮 `:active` 按下态，之前只有 hover
- `:disabled` 统一：`#1f232a` 底 + `#5a6270` 字
- `placeholder` 伪类单独压暗（`#6f7783`）

### 6. 滚动条
```css
scrollbar slider { min-width: 10px; min-height: 32px; margin: 2px; }
scrollbar slider:hover  { background-color: #4a5261; }
scrollbar slider:active { background-color: #5a6270; }
```
触摸屏要能拖，宽度 8→10px，最小长度 32px；补了 hover/active。

### 7. 虚拟键盘分层
键盘是「临时层」，底色用 `#171a20`（比输入栏再暗一档）沉下去，形成深度差。
普通键 `#2f3540` / 功能键 `#262b34` / 回车 `#2563eb` 三档不变，但字号 19→18、15→14，
并在 compact 变体里**补齐了原版缺失的 `.vkbd-cand.vkbd-compact`**（候选词条在紧凑模式下
之前完全没有压缩规则，会撑高键盘）。

---

## 三、对比度校验（WCAG）

| 位置 | 对比度 | 等级 |
|---|---|---|
| 正文 / AI气泡 | 12.20 | AAA |
| 正文 / 对话区底 | 14.75 | AAA |
| 状态文字 / 顶栏 | 6.55 | AA |
| 副标题 / 顶栏 | 4.61 | AA |
| 元信息 / 对话区底 | 4.00 | AA-large |
| 占位提示 / 输入框 | 3.31 | AA-large |
| 错误文字 / 错误气泡 | 8.06 | AAA |
| 成功/警告/危险指示灯 | 9.58 / 10.00 / 6.04 | AAA / AAA / AA |

全部不低于 AA-large（3.0），正文类全部 AAA。

---

## 四、验证方法与环境坑

板端 `xwd -id <窗格>` 与 `ffmpeg -f x11grab :0` **抓到的都是黑屏或错误内容**——
它们抓的是 mutter 的 frame 窗口，不是 Xwayland 里的 GTK 内容窗口。
我早期一度据此误判「标题没渲染」，后来发现是抓图方式的问题。

可靠做法是用 Gdk 直接从 GTK 窗口取像素：

```python
pb = Gdk.pixbuf_get_from_window(win.get_window(), 0, 0, w, h)
pb.savev(path, "png", [], [])
```

另外板端系统 Python 无 PIL，图像分析要拉回本机做。

启动 GUI 必须 `GDK_BACKEND=x11` + `DISPLAY=:0` +
`XAUTHORITY=/run/user/1001/.mutter-Xwaylandauth.*`，否则 GTK 走 Wayland，窗口不在 X11 树里。

---

## 五、给 ui-fixer（任务B）的需求：预览区占位 bug

**这是「文字显示不全」的真正根因，必须改 rkchat.py，我无权修改。**

`rkchat.py` 约 285-300 行：

```python
self.preview_box.show_all()
self.preview_box.set_no_show_all(True)     # 问题1
...
root.pack_start(self.preview_box, True, True, 0)   # 问题2：expand=True 持续占位
```

**问题1**：`set_no_show_all(True)` 之后，`show_all()` 对该控件永久失效。
实测 `app.preview_box.show_all()` 调用后 `get_visible()` 仍返回 `False`，
即摄像头预览根本无法通过 `show_all()` 显示。注释里说「_on_cam_clicked 里要 show_all 一次」，
但那次 show_all 同样是无效的。

**问题2**：`preview_box` 即使 `visible=False`，`pack_start(expand=True)` 仍让它占
`@0,240 1280x481` 的空间，导致对话区实际可用高度只有 180px。

**建议改法**（供参考，请自行验证）：

```python
# 1. 不要用 no_show_all 来挡 show_all()，改用 pack/remove 动态装卸
# 2. 或用 set_vexpand 动态切换：
def _show_preview(self):
    self.preview_box.set_no_show_all(False)
    self.preview_box.show_all()
    self.preview_box.set_vexpand(True)

def _hide_preview(self):
    self.preview_box.set_vexpand(False)
    self.preview_box.hide()
```

预期收益：预览隐藏时对话区从 180px（实际渲染区）恢复到 669px，**这才是真正解决
「内容显示不全」**。我这边已经把 CSS 能压的纵向空间都压出来了（+9px），
但布局占位问题不解决，用户看到的还是大片空白。

---

## 六、改动前后数据

| 指标 | 改前 | 改后 |
|---|---|---|
| 顶栏高 | 61px | 58px |
| 输入栏高 | 79px | 73px |
| 对话区可视高 | 660px | 669px |
| CSS 行数 | 324 | 419 |
| 颜色种类（主色板） | 7 种近似值 | 7 种语义化分层 |
| 正文对比度 | 12.20 (AAA) | 12.20 (AAA) 保持 |
| 缺失的交互态 | focus/active/disabled/placeholder | 全部补齐 |

回归测试：6 条气泡（含超长单词、长 URL、多行代码）零裁切；
键盘态、预览态、紧凑键盘态均渲染正常；CSS 解析无 warning；
本机与板端文件 md5 一致（`0212fffd6836223bb0edee7cc90742d0`）。

---

## 七、与 ui-fixer（任务B）的配合

并行开发中发现 ui-fixer 新增了 `.bubble-pad` 类，用于解决 **GTK3 里 GtkEventBox
上 CSS padding 不生效** 的问题（实测气泡左右 padding 只剩 1px，文字贴边）。

他们把真实内边距改由内层 Box 的 `border-width` 提供，我相应在 CSS 补上：

```css
/* 气泡真实内边距：border-width 在这一层生效 */
.bubble-pad {
    border-width: 9px 13px;
    border-style: solid;
    border-color: transparent;
    background-color: transparent;
}
/* 用户气泡内 pad 必须透明，否则盖住蓝底 */
.bubble-user .bubble-pad { background-color: transparent; border-color: transparent; }
/* system 气泡不需要大边距，单独收紧 */
.bubble-sys.bubble-pad { border-width: 2px 4px; }
```

同时把 `.bubble-user/.bubble-ai/.bubble-err` 的 `padding` 显式设为 0，
避免 CSS padding 与 border-width 叠加成双倍边距。

实测验证：`frame.width - label.width = 26px`（左右各 13px），
`frame.height - label.height = 18px`（上下各 9px），与 `border-width: 9px 13px` 完全一致；
截图像素测量：气泡内文字左留白 16px、右留白 17px（含抗锯齿边缘）。

**文字贴边的真正修复在这里，感谢 ui-fixer 的配合。**

---

## 八、最终交付

| 项 | 值 |
|---|---|
| 文件 | `deploy/ui/rkchat/style.css` |
| 行数 | 324 → 447 |
| md5 | `0212fffd6836223bb0edee7cc90742d0` |
| 部署位置 | `/data/rkllm-chat/ui/rkchat/style.css`（已同步） |
| CSS 解析 | 无 warning |
| selector 删除 | 0（全部保留） |

改动的文件只有 `style.css` 和新增的 `STYLE_CHANGES.md`，未触碰 `rkchat.py`。
