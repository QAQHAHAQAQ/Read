#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
耳读 —— 图形界面版。
复制文字 → Ctrl+Alt+R，或点「读剪贴板」；可暂停、跳段、调语速音色。
"""

import os
import sys
import time
import json
import ctypes
import tempfile
import threading
import subprocess
from ctypes import wintypes

if getattr(sys, "frozen", False):
    # 打包成 exe 之后：资源在 _MEIPASS，配置写在 exe 旁边
    HERE = os.path.dirname(sys.executable)
    RES = getattr(sys, "_MEIPASS", HERE)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    RES = HERE

if RES not in sys.path:
    sys.path.insert(0, RES)

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import read_clip as rc
import screenshot as shot
from hotkeys import HotkeyManager, combo_readable

UI_HTML = os.path.join(RES, "ui.html")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
LOGFILE = os.path.join(tempfile.gettempdir(), "read_aloud_ui.log")
HOTKEY_PID = os.path.join(tempfile.gettempdir(), "read_aloud_hotkey.pid")
UI_PID = os.path.join(tempfile.gettempdir(), "read_aloud_ui.pid")

DEFAULTS = {
    "hotkey_read": "ctrl+alt+r",
    "hotkey_shot": "ctrl+alt+a",
    "voice": rc.VOICE,
    "rate": 8,
}

SYNTH_GAP = 0.8          # 两次语音合成的最小间隔（秒），防微软限流


def load_settings():
    out = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in DEFAULTS:
            if k in data and data[k] not in (None, ""):
                out[k] = data[k]
    except Exception:
        pass
    return out


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f"保存设置失败: {e}")


def log(msg):
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


class Player:
    def __init__(self, settings=None):
        settings = settings or load_settings()
        self.segs = []
        self.cur = -1
        self.status = "idle"          # idle / playing / paused
        self.voice = settings.get("voice") or rc.VOICE
        self.rate_num = int(settings.get("rate", 8))
        self.rate = f"{self.rate_num:+d}%"
        self.rev = 0
        self.busy = False             # 正在截图识别
        self.notice = ""              # 给界面的一句提示
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread = None
        self._tmpdir = tempfile.mkdtemp(prefix="reader_ui_")
        self._seq = 0

    # ── 加载 ────────────────────────────────────────────
    def load(self, raw):
        self.stop()
        text = rc.clean(raw or "")
        self.segs = rc.split_text(text, limit=45) if text else []
        self.cur = -1
        self.status = "idle"
        self.rev += 1
        return self.segs

    # ── 控制 ────────────────────────────────────────────
    def play(self, start=0):
        self.stop()
        if not self.segs:
            return
        start = max(0, min(int(start), len(self.segs) - 1))

        stop = threading.Event()
        pause = threading.Event()
        self._stop = stop
        self._pause = pause
        self.status = "playing"
        self.cur = start

        self._thread = threading.Thread(
            target=self._run, args=(start, stop, pause), daemon=True)
        self._thread.start()

    def pause(self):
        if self.status == "playing":
            self._pause.set()

    def resume(self):
        self._pause.clear()

    def stop(self):
        self._stop.set()
        self._pause.clear()
        old = self._thread
        if old is not None and old.is_alive():
            old.join(timeout=1.5)
        self._thread = None
        if self.status != "idle":
            self.status = "idle"

    # ── 播放主循环 ──────────────────────────────────────
    def _run(self, start, stop, pause):
        cache, built, failed = {}, set(), set()

        def producer():
            last = 0.0
            for i in range(start, len(self.segs)):
                if stop.is_set():
                    return
                if i in built:
                    continue
                built.add(i)
                path = os.path.join(self._tmpdir, f"s{self.rev}_{i:04d}.mp3")
                ok = False
                for attempt in range(3):
                    if stop.is_set():
                        return
                    # 两次请求之间留间隔：微软那边请求太密会直接返回空音频
                    gap = time.time() - last
                    if gap < SYNTH_GAP:
                        time.sleep(SYNTH_GAP - gap)
                    last = time.time()
                    try:
                        actual = rc.synth(self.segs[i], path, self.voice, self.rate)
                        if actual:
                            ok = True
                            break
                        log(f"段 {i} 合成失败({attempt+1}/3)")
                    except Exception as e:
                        log(f"段 {i} 合成异常({attempt+1}/3): {type(e).__name__}")
                    time.sleep((1, 4, 9)[attempt])
                last = time.time()
                if ok:
                    cache[i] = actual
                else:
                    failed.add(i)

        threading.Thread(target=producer, daemon=True).start()

        consec = 0
        try:
            for i in range(start, len(self.segs)):
                if stop.is_set():
                    break
                self.cur = i

                path = self._wait_file(i, cache, failed, stop)
                if path is None:
                    if stop.is_set():
                        break
                    consec += 1
                    if consec >= 2:
                        self.notice = "语音合成失败，检查网络后重试"
                        log("连续两段合成失败，停止播放")
                        rc.beep("warn")
                        break
                    log(f"段 {i} 没有音频，跳过")
                    continue
                consec = 0
                self._play_one(path, stop, pause)
        finally:
            for p in list(cache.values()):
                try:
                    os.remove(p)
                except OSError:
                    pass
            if not stop.is_set():
                self.status = "idle"

    def _wait_file(self, i, cache, failed, stop, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if stop.is_set():
                return None
            if i in cache:
                return cache[i]
            if i in failed:
                return None
            time.sleep(0.05)
        return None

    def _play_one(self, path, stop, pause):
        self._seq += 1
        alias = f"ui{self._seq}"
        ext = os.path.splitext(path)[1].lower()
        mci_type = "waveaudio" if ext == ".wav" else "mpegvideo"
        opened = rc._mci(f'open "{path}" type {mci_type} alias {alias}')
        if opened != 0:
            log(f"段 {self.cur} open 失败 rc={opened}")
            return
        t0 = time.time()
        try:
            try:
                rc._mci(f"setaudio {alias} volume to {int(rc.VOLUME * 1000)}")
            except Exception:
                pass
            # 查音频时长，用来兜底：MCI 有时会卡在 playing 状态但不出声
            _, lenstr = rc._mci(f"status {alias} length", want=True)
            try:
                dur_s = int(lenstr) / 1000.0
            except (ValueError, TypeError):
                dur_s = 0
            deadline = time.time() + (dur_s or 240) + 10

            played = rc._mci(f"play {alias}")
            if played != 0:
                log(f"段 {self.cur} play 失败 rc={played}")
                return
            time.sleep(0.12)
            paused_now = False
            while True:
                if time.time() > deadline:
                    log(f"段 {self.cur} 超时强制停止（时长 {dur_s:.1f}s）")
                    rc._mci(f"stop {alias}")
                    break
                if stop.is_set():
                    rc._mci(f"stop {alias}")
                    break
                if pause.is_set():
                    if not paused_now:
                        rc._mci(f"pause {alias}")
                        paused_now = True
                        self.status = "paused"
                        log(f"段 {self.cur} 暂停")
                    time.sleep(0.1)
                    continue
                if paused_now:
                    rc._mci(f"resume {alias}")
                    paused_now = False
                    self.status = "playing"
                    log(f"段 {self.cur} 继续")
                _, mode = rc._mci(f"status {alias} mode", want=True)
                if mode not in ("playing", "starting"):
                    break
                time.sleep(0.08)
        finally:
            rc._mci(f"close {alias}")
            if not stop.is_set() and time.time() - t0 > 3:
                log(f"段 {self.cur} 结束 用时 {time.time()-t0:.1f}s")

    # ── 给界面读的状态 ──────────────────────────────────
    def state(self):
        return {
            "cur": self.cur,
            "status": self.status,
            "rev": self.rev,
            "total": len(self.segs),
            "busy": self.busy,
            "notice": self.notice,
        }


class Api:
    def __init__(self, player, settings, hotkeys, window=None):
        self.player = player
        self.settings = settings
        self.hotkeys = hotkeys
        self.window = window

    def minimize(self):
        if self.window:
            self.window.minimize()

    def close(self):
        self.player.stop()
        if self.hotkeys:
            self.hotkeys.stop()
        if self.window:
            self.window.destroy()

    def get_state(self):
        return self.player.state()

    def get_segs(self):
        return {"segs": self.player.segs, "rev": self.player.rev}

    def load_clip(self):
        segs = self.player.load(rc.clip_text())
        return {"segs": segs, "rev": self.player.rev,
                "chars": sum(len(s) for s in segs)}

    def capture(self):
        """截图取字：异步跑，界面靠 state 里的 busy / notice 感知。"""
        if not self.player.busy:
            threading.Thread(target=run_capture, args=(self.player, False),
                             daemon=True).start()
        return {"ok": True}

    def play(self, start=0):
        self.player.play(start)

    def pause(self):
        self.player.pause()

    def resume(self):
        self.player.resume()

    def stop(self):
        self.player.stop()

    def set_rate(self, v):
        n = int(v)
        self.player.rate_num = n
        self.player.rate = f"{n:+d}%"
        self.settings["rate"] = n
        save_settings(self.settings)

    def set_voice(self, v):
        self.player.voice = v
        self.settings["voice"] = v
        save_settings(self.settings)

    # ── 热键 ────────────────────────────────────────────
    def get_settings(self):
        # 热键可能还没注册完（页面先于 manager.start 加载），用配置文件里的值兜底
        bind = dict(self.hotkeys.bindings) if self.hotkeys else {}
        for action, key in (("read", "hotkey_read"), ("shot", "hotkey_shot")):
            if not bind.get(action):
                bind[action] = self.settings.get(key, "")
        return {
            "settings": self.settings,
            "hotkeys": self.hotkeys.status() if self.hotkeys else {},
            "readable": {k: combo_readable(v) for k, v in bind.items() if v},
            "errors": dict(self.hotkeys.errors) if self.hotkeys else {},
        }

    def set_hotkey(self, action, combo):
        if not self.hotkeys:
            return {"ok": False, "msg": "热键模块未就绪"}
        r = self.hotkeys.set_binding(action, combo)
        if r.get("ok"):
            self.settings[f"hotkey_{action}"] = r["combo"]
            save_settings(self.settings)
            log(f"热键 {action} → {r['combo']}")
        else:
            log(f"热键 {action} 改键失败: {r.get('msg')}")
        return r


def single_instance():
    """保证只有一个耳读在跑，否则会抢热键、叠音。"""
    try:
        if os.path.exists(UI_PID):
            pid = int(open(UI_PID, encoding="utf-8").read().strip())
            if pid != os.getpid() and rc.alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
                time.sleep(1.0)          # 等它把热键和音频放掉
                log(f"结束旧实例 PID={pid}")
    except Exception as e:
        log(f"清理旧实例失败: {e}")
    try:
        with open(UI_PID, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def cleanup_and_exit(code=0):
    try:
        os.remove(UI_PID)
    except OSError:
        pass
    os._exit(code)


def take_over_hotkey():
    """把还在跑的 hotkey.py 关掉，热键交给这个窗口。"""
    try:
        if not os.path.exists(HOTKEY_PID):
            return
        pid = int(open(HOTKEY_PID, encoding="utf-8").read().strip())
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=5)
        os.remove(HOTKEY_PID)
        log(f"接管热键，结束旧常驻 PID={pid}")
    except Exception as e:
        log(f"接管热键失败: {e}")


def run_capture(player, play=True):
    """框选 → OCR → 载入。不动主窗口——之前强行 minimize 会让 WebView2 失联。"""
    if player.busy:
        return
    player.busy = True
    player.notice = "框选要朗读的区域…"
    try:
        log("capture: 弹出选区")
        box = shot.pick_region()
        if not box:
            player.notice = "已取消"
            log("capture: 用户取消")
            return
        log(f"capture: 选区 {box}")
        player.notice = "正在识别…"
        img = shot.grab_screen().crop(box)
        text = shot.ocr_image(img)
        if not text.strip():
            player.notice = "这块区域没识别出文字"
            log("capture: 没识别到文字")
            rc.beep("warn")
            return
        segs = player.load(text)
        player.notice = f"识别到 {len(segs)} 段"
        log(f"capture: 识别 {len(text)} 字 / {len(segs)} 段")
        if play:
            player.play(0)
    except Exception as e:
        player.notice = f"截图失败：{e}"
        log(f"capture 异常: {e}")
        rc.beep("warn")
    finally:
        player.busy = False


def handle_hotkey(player, action):
    try:
        if action == "read":
            if player.status == "playing":
                player.stop()
            else:
                player.load(rc.clip_text())
                player.play(0)
        elif action == "shot":
            if player.status == "playing":
                player.stop()
            run_capture(player, play=True)
    except Exception as e:
        log(f"热键动作 {action} 异常: {e}")


def dark_titlebar(title="耳读", tries=25):
    """把系统标题栏刷成深色，跟界面配套。Win10 只支持深浅开关。"""
    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    for _ in range(tries):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            try:
                flag = ctypes.c_int(1)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20,
                                          ctypes.byref(flag), ctypes.sizeof(flag))
                caption = ctypes.c_int(0x0000000C)     # Win11 才认，Win10 会忽略
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 35,
                                          ctypes.byref(caption), ctypes.sizeof(caption))
                log("标题栏已设深色")
                return True
            except Exception as e:
                log(f"深色标题栏设置失败: {e}")
                return False
        time.sleep(0.1)
    log("没找到窗口句柄，标题栏保持系统色")
    return False


def find_own_window(title="耳读"):
    """只认本进程的窗口。别的进程可能留着同名的孤儿窗口，不能认错。"""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
                return False
        return True

    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return found[0] if found else 0


def window_watchdog():
    """兜底：窗口一没，整个进程跟着走，别留下拖着内存的僵尸。"""
    user32 = ctypes.windll.user32
    hwnd = 0
    for _ in range(80):
        hwnd = find_own_window()
        if hwnd:
            break
        time.sleep(0.2)
    if not hwnd:
        log("没找到自己的窗口，看门狗退出")
        return
    miss = 0
    while True:
        time.sleep(1.2)
        if user32.IsWindow(hwnd):
            miss = 0
        else:
            miss += 1
            if miss >= 3:
                log("窗口连续三次探测不到，进程退出")
                cleanup_and_exit(0)


def main():
    single_instance()
    take_over_hotkey()

    import webview

    settings = load_settings()
    player = Player(settings)
    manager = HotkeyManager(lambda action: handle_hotkey(player, action))
    api = Api(player, settings, manager)
    with open(UI_HTML, encoding="utf-8") as f:
        html = f.read()

    window = webview.create_window(
        "耳读", html=html, js_api=api,
        width=644, height=800, min_size=(520, 620),
        background_color="#0c0f13",
        frameless=True, easy_drag=False,
    )
    api.window = window

    def boot():
        manager.start({
            "read": settings.get("hotkey_read", "ctrl+alt+r"),
            "shot": settings.get("hotkey_shot", "ctrl+alt+a"),
        })
        st = manager.status()
        log(f"热键就绪 {st['bindings']} 失败={st['errors']}")
        threading.Thread(target=window_watchdog, daemon=True).start()

        def preload():
            # 窗口起来后顺手看看剪贴板，够长就摆在界面上，不自动念
            time.sleep(1.2)
            try:
                raw = rc.clip_text()
                segs = player.load(raw)
                if len(segs) < 2:
                    player.load("")
                    log("剪贴板内容过短，未自动载入")
                else:
                    log(f"启动时载入剪贴板 {len(segs)} 段")
            except Exception as e:
                log(f"预载失败: {e}")

        threading.Thread(target=preload, daemon=True).start()

    def on_closed():
        log("收到窗口关闭事件")
        player.stop()
        try:
            os.remove(UI_PID)
        except OSError:
            pass
        # 留点时间让 WebView2 自己收尾，避免留下没有宿主的黑窗口
        threading.Timer(0.9, lambda: os._exit(0)).start()

    try:
        window.events.closed += on_closed
    except Exception as e:
        log(f"挂接关闭事件失败: {e}")

    webview.start(boot, gui="edgechromium")
    player.stop()
    cleanup_and_exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("启动异常:\n" + traceback.format_exc())
        raise
