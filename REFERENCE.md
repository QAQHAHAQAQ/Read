# 参考文档

面向想改代码、或者遇到问题要深挖的人。日常使用看 [README.md](README.md) 就够了。

## 配置文件

`settings.json` 和脚本放在同一目录，首次改设置时自动生成。删掉它就会恢复默认。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `hotkey_read` | string | `ctrl+alt+r` | 读剪贴板的热键 |
| `hotkey_shot` | string | `ctrl+alt+a` | 截图取字的热键 |
| `voice` | string | `zh-CN-XiaoxiaoNeural` | 音色，取值见下方音色表 |
| `rate` | int | `8` | 语速百分比，`-30` ~ `40` |

热键字符串的写法：小写，用 `+` 连接，修饰键在前。例如 `ctrl+alt+r`、`win+shift+f9`、`mouse4`。

可用的键名：字母、数字、`f1`~`f12`、`space`、`enter`、`tab`、`esc`、`backspace`、`insert`、`delete`、`home`、`end`、`pageup`、`pagedown`、`left`、`up`、`right`、`down`，以及符号 `` ` `` `-` `=` `[` `]` `\` `;` `'` `,` `.` `/`。

鼠标侧键写 `mouse4`（后退键）或 `mouse5`（前进键）。

### 音色表

| 参数值 | 名字 | 特点 |
|---|---|---|
| `zh-CN-XiaoxiaoNeural` | 晓晓 | 女声，自然，默认 |
| `zh-CN-YunxiNeural` | 云希 | 男声，年轻 |
| `zh-CN-YunjianNeural` | 云健 | 男声，浑厚 |
| `zh-CN-XiaoyiNeural` | 晓伊 | 女声，温柔 |
| `zh-CN-YunyangNeural` | 云扬 | 男声，播音腔 |

## 语音合成：双引擎

主引擎是 `edge-tts`（微软 Edge 浏览器"大声朗读"背后的那个服务），音色好，但**要联网，而且会被限流**。

实测现象：短时间内密集请求，服务端会随机返回空音频，`edge-tts` 抛 `NoAudioReceived`。加 0.8 秒间隔也挡不住（实测 6 段里仍有 2 段被拒），等 45 秒后恢复正常。跟代理无关——带代理和不带代理都会出现。

所以设计成双引擎：

```
synth(text, path)
  ├─ 先试 _synth_edge()   → 成功返回 .mp3 路径
  └─ 失败 → _synth_sapi() → 返回 .wav 路径
```

`_synth_sapi` 用系统自带的 SAPI（`Microsoft Huihui Desktop`，中文系统默认就有），完全离线、0.1 秒出结果，音色比晓晓差但不会哑。

上层不需要知道用的是哪个引擎，`_play_one` 按扩展名选 MCI 类型：`.wav → waveaudio`，其余 → `mpegvideo`。

三个配套措施：

1. **请求间隔** `SYNTH_GAP = 0.8` 秒，降低被拒概率
2. **失败重试 3 次**，退避 1 / 4 / 9 秒
3. **连续两段失败就停**，界面提示"语音合成失败"，不再静默跳过

第三条是关键。早期版本失败后直接 `continue` 跳过该段，结果是进度条照走、界面照常、就是没声音，用户完全不知道发生了什么。

### 调用 SAPI 的注意事项

在子线程里用 `win32com` 必须先初始化 COM，否则报错：

```python
import pythoncom
pythoncom.CoInitialize()
try:
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Format.Type = 22            # 22kHz 16bit 单声道
    stream.Open(wav_path, 3, False)    # 3 = 创建并写入
    speaker.AudioOutputStream = stream
    speaker.Speak(text)
    stream.Close()
finally:
    pythoncom.CoUninitialize()
```

## 热键：两套机制

### 键盘组合 —— RegisterHotKey

Windows 原生接口，注册在**工作线程**上（热键是线程级绑定，所以注册和注销必须在同一个线程里做）。

`HotkeyManager._loop()` 用 `PeekMessageW` 轮询而不是 `GetMessageW` 阻塞，这样才能在运行中响应改键请求。

改键流程：先注销该动作的旧热键 → 注册新的 → 失败则回滚旧的并返回原因。错误码 `1409`（`ERROR_HOTKEY_ALREADY_REGISTERED`）对应"已被其他程序占用"。

### 鼠标侧键 —— 低级钩子

`RegisterHotKey` **不支持 XBUTTON**（只认左、中、右）。要支持侧键只能自己装钩子：

```python
WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
```

回调里从 `MSLLHOOKSTRUCT.mouseData` 的高 16 位取键号（`1` = XBUTTON1，`2` = XBUTTON2），命中绑定就起一个线程触发动作，然后 `CallNextHookEx` 放行。

三个容易踩的点：

- 回调**必须持有引用**（存到 `self._hook_proc`），否则被 GC 回收后钩子静默失效
- 回调里不能做耗时操作，否则整机鼠标都会卡——所以只起线程，不干活
- 钩子是全局的，不区分窗口

只要绑定了鼠标键就装钩子，改回键盘组合就卸掉，不常驻。

## 截图取字

### 选区

用 `tkinter` 建一个全屏无边框窗口，半透明遮罩（`-alpha 0.38`），鼠标变十字，拖拽画框。

两个坑：

- **不能用 `-fullscreen`**。它和 `overrideredirect(True)` 冲突，报 `TclError: can't set fullscreen attribute`。改用 `geometry(f"{w}x{h}+0+0")`。
- **必须抢前台**。`focus_force()` 不够，得 `SetForegroundWindow(GetParent(root.winfo_id()))`，否则 `Esc` 和拖拽事件都收不到。

tkinter 可以在子线程里跑（主线程被 pywebview 占着），前提是主线程没有别的 Tk 实例。

### OCR

`winsdk` 调 Windows 自带的 `Windows.Media.Ocr`，纯本地，不联网。

```python
engine = OcrEngine.try_create_from_language(Language("zh-Hans"))
result = await engine.recognize_async(bitmap)
```

要点：

- 小图先放大到 1100px 宽再识别，准确率明显提升
- **中文会被按字拆开加空格**（识别出"测 试 ． 识 别"），必须后处理。`tidy_ocr()` 干的活：汉字之间去空格、全角标点前后去空格、`．`还原成`。`、形近字纠正（`囗`→`口`）
- 实测耗时：14 行 498 字约 0.09 秒，冷启动首次 0.25 秒

## 模块职责

| 文件 | 职责 |
|---|---|
| `player_ui.py` | 主程序：窗口、播放调度、截图流程、单实例、看门狗 |
| `ui.html` | 界面：深色无边框、自绘标题栏、状态轮询、热键录制 |
| `read_clip.py` | 文本清理、分段、语音合成（双引擎）、MCI 播放 |
| `screenshot.py` | 全屏选区、OCR、中文断字修正 |
| `hotkeys.py` | 热键注册、改键、冲突检测、鼠标侧键钩子 |
| `make_icon.py` | 生成 `app.ico`（7 个尺寸） |
| `make_shortcut.py` | 建桌面快捷方式 |

## 架构上两个必须知道的点

### 界面状态靠轮询，不是推送

Python 侧不主动调 `evaluate_js`。原因是 pywebview 6.2 里**从子线程调 `evaluate_js` 会抛 `CoreWebView2Controller can only be accessed from the UI thread`**，而且会让进程内存飙到几个 GB。

改成 JS 每 260ms 调 `get_state()` 拉状态：

```js
const s = await window.pywebview.api.get_state();   // {cur, status, rev, total, busy, notice}
```

`rev` 是段落版本号，变了才去拉 `get_segs()` 重新渲染，避免每次轮询都传全文。

### 关窗口要留缓冲

无边框窗口收到 `WM_CLOSE` 不一定触发 pywebview 的关闭流程，所以：

- 界面上的 × 调用 `api.close()` → `window.destroy()`
- `events.closed` 里先记日志、再 `threading.Timer(0.9, os._exit)`，给 WebView2 留收尾时间
- 另外有看门狗：用 `EnumWindows` + `GetWindowThreadProcessId` 找到**本进程**的窗口（不能用 `FindWindowW` 按标题找，会认错残留的同名窗口），连续三次探测不到就退出

早期版本没这些保护，关窗口后进程不退、内存涨到 3.8GB，还留下一块黑屏的死窗口。

## 故障排查

| 现象 | 大概率原因 | 处理 |
|---|---|---|
| 有进度没声音 | 语音合成被拒，且当天版本会静默跳过 | 现在会自动降级到离线语音；仍无声看 `%TEMP%\read_aloud_ui.log` |
| 热键按了没反应 | 被别的软件占用，注册失败 | 看界面键帽是否标红，悬停看原因 |
| 录不进鼠标侧键 | 鼠标驱动把侧键映射成了别的功能 | 去厂商驱动里改回"前进/后退" |
| 窗口黑屏、点不动 | 主进程已退出，剩下没有宿主的 WebView2 窗口 | 重启耳读；系统里残留的那个只能重启电脑清掉 |
| 切回窗口时空白一下 | WebView2 后台挂起渲染 | 正常现象 |
| 启动就退出 | 端口/热键被占，或 `settings.json` 损坏 | 删掉 `settings.json` 重试，看日志 |

日志在 `%TEMP%\read_aloud_ui.log`，记录热键注册结果、截图流程、每段合成失败原因、播放耗时。

## 开发

重新生成图标：

```bash
python make_icon.py     # 输出 app.png（预览）和 app.ico（7 个尺寸）
```

图标是用 PIL 画的：深色圆角底 + 汉字「耳」+ 右下角两道声波弧。试过纯几何图形（闭眼 + 声波），但弧线在小尺寸下糊成一团，汉字反而更清楚。

改完图标要重建快捷方式才生效：

```bash
python make_shortcut.py
```

注意：直接跑脚本不会被安全策略拦，但在 shell 里内联 `win32com` / `WScript.Shell` 相关代码容易被判成 LOLBin 拒绝执行。
