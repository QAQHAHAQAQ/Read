#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全局热键管理 —— 支持运行中改键、冲突检测、失败回滚。
Windows 的热键是线程级绑定，所以注册/注销都放在同一个工作线程里做。
"""

import ctypes
import time
import threading
from ctypes import wintypes

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# 鼠标侧键：RegisterHotKey 不支持 XBUTTON，只能用低级钩子抓
WM_XBUTTONDOWN = 0x020B
WH_MOUSE_LL = 14
MOUSE_ALIASES = {
    "mouse4": 1, "xbutton1": 1, "m4": 1, "back": 1,
    "mouse5": 2, "xbutton2": 2, "m5": 2, "forward": 2,
}


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

MOD_NAMES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN,
}

VK_NAMES = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 13):
    VK_NAMES[f"f{_i}"] = 0x6F + _i          # F1=0x70


def parse_combo(combo):
    """
    'ctrl+alt+r' → (mods, vk)；'mouse4' → (0, -4) 用负数标记鼠标键。
    不合法返回 None。
    """
    if not combo:
        return None
    s = str(combo).strip().lower()
    if s in MOUSE_ALIASES:
        return 0, -MOUSE_ALIASES[s]          # 负数代表鼠标侧键

    parts = [p.strip().lower() for p in s.split("+") if p.strip()]
    mods, vk = 0, None
    for p in parts:
        if p in MOD_NAMES:
            mods |= MOD_NAMES[p]
        elif p in VK_NAMES:
            vk = VK_NAMES[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        else:
            return None
    if vk is None or mods == 0:
        return None
    return mods, vk


def combo_readable(combo):
    """给界面看的写法：ctrl+alt+r → Ctrl + Alt + R"""
    if not combo:
        return ""
    s = str(combo).strip().lower()
    if s in MOUSE_ALIASES:
        return "鼠标侧键 4" if MOUSE_ALIASES[s] == 1 else "鼠标侧键 5"
    out = []
    for p in str(combo).split("+"):
        p = p.strip()
        if p.lower() in ("ctrl", "control"):
            out.append("Ctrl")
        elif p.lower() == "alt":
            out.append("Alt")
        elif p.lower() == "shift":
            out.append("Shift")
        elif p.lower() in ("win", "meta"):
            out.append("Win")
        elif len(p) == 1:
            out.append(p.upper())
        else:
            out.append(p.upper() if p.lower().startswith("f") else p.capitalize())
    return " + ".join(out)


class HotkeyManager:
    def __init__(self, on_trigger):
        self.on_trigger = on_trigger        # callable(action)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.bindings = {}                  # action -> combo
        self.errors = {}                    # action -> 错误说明
        self._id2action = {}
        self._next_id = 1
        self._pending = []                  # 待改键请求
        self._running = False
        self._thread = None
        self._ready = threading.Event()
        self._mouse_bindings = {}           # 侧键号(1/2) -> action
        self._hook = None
        self._hook_proc = None

    # ── 生命周期 ────────────────────────────────────────
    def start(self, bindings):
        self.bindings = dict(bindings)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def stop(self):
        self._running = False

    def _loop(self):
        self._apply_all()
        self._ready.set()

        msg = wintypes.MSG()
        while self._running:
            while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    action = self._id2action.get(int(msg.wParam))
                    if action:
                        threading.Thread(
                            target=self._safe_trigger, args=(action,),
                            daemon=True).start()
            while self._pending:
                job = self._pending.pop(0)
                self._do_set(job)
            time.sleep(0.03)
        self._clear_all()
        self._uninstall_hook()

    def _safe_trigger(self, action):
        try:
            self.on_trigger(action)
        except Exception:
            pass

    # ── 注册 ────────────────────────────────────────────
    def _clear_all(self):
        for hid in list(self._id2action.keys()):
            try:
                self.user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._id2action.clear()

    def _register(self, action, combo):
        parsed = parse_combo(combo)
        if not parsed:
            return False, "组合不合法（键盘组合需带修饰键；也可直接用鼠标侧键）"
        mods, vk = parsed
        if vk < 0:                           # 鼠标侧键，不进 RegisterHotKey
            self._mouse_bindings[-vk] = action
            return True, ""
        hid = self._next_id
        self._next_id += 1
        ok = self.user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk)
        if ok:
            self._id2action[hid] = action
            return True, ""
        err = ctypes.get_last_error()
        if err == ERROR_HOTKEY_ALREADY_REGISTERED:
            return False, f"{combo_readable(combo)} 已被其他程序占用"
        return False, f"注册失败（错误码 {err}）"

    def _apply_all(self):
        self._clear_all()
        self.errors.clear()
        self._id2action.clear()
        self._mouse_bindings.clear()
        for action, combo in list(self.bindings.items()):
            ok, msg = self._register(action, combo)
            if not ok:
                self.errors[action] = msg
        self._sync_mouse_hook()

    # ── 鼠标侧键钩子 ────────────────────────────────────
    def _sync_mouse_hook(self):
        if self._mouse_bindings and not self._hook:
            self._install_hook()
        elif not self._mouse_bindings and self._hook:
            self._uninstall_hook()

    def _install_hook(self):
        u = self.user32
        hookproc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)

        def proc(code, wparam, lparam):
            if code >= 0 and wparam == WM_XBUTTONDOWN:
                try:
                    info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    btn = (info.mouseData >> 16) & 0xFFFF
                    action = self._mouse_bindings.get(btn)
                    if action:
                        threading.Thread(target=self._safe_trigger,
                                         args=(action,), daemon=True).start()
                except Exception:
                    pass
            return u.CallNextHookEx(None, code, wparam, lparam)

        try:
            u.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc,
                                            wintypes.HINSTANCE, wintypes.DWORD]
            u.SetWindowsHookExW.restype = ctypes.c_void_p
            u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         wintypes.WPARAM, wintypes.LPARAM]
            u.CallNextHookEx.restype = ctypes.c_ssize_t
            u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            self._hook_proc = hookproc(proc)          # 持有引用，别被回收
            self._hook = u.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, None, 0)
        except Exception:
            self._hook = None

    def _uninstall_hook(self):
        if self._hook:
            try:
                self.user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
        self._hook = None
        self._hook_proc = None

    # ── 改键（可被任意线程调用） ────────────────────────
    def set_binding(self, action, combo, timeout=3.0):
        ev = threading.Event()
        box = {}
        self._pending.append((action, combo, ev, box))
        if not ev.wait(timeout):
            return {"ok": False, "msg": "热键线程无响应"}
        return box.get("result", {"ok": False, "msg": "未知错误"})

    def _do_set(self, job):
        action, combo, ev, box = job
        old = self.bindings.get(action)

        # 先摘掉这个动作原有的热键
        for hid, act in list(self._id2action.items()):
            if act == action:
                try:
                    self.user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
                self._id2action.pop(hid, None)
        for btn, act in list(self._mouse_bindings.items()):
            if act == action:
                self._mouse_bindings.pop(btn, None)

        ok, msg = self._register(action, combo)
        if ok:
            self.bindings[action] = combo
            self.errors.pop(action, None)
            self._sync_mouse_hook()
            box["result"] = {"ok": True, "combo": combo,
                             "readable": combo_readable(combo)}
        else:
            if old:
                back_ok, _ = self._register(action, old)
                if back_ok:
                    self.bindings[action] = old
                    self._sync_mouse_hook()
            box["result"] = {"ok": False, "msg": msg,
                             "combo": old,
                             "readable": combo_readable(old or "")}
        ev.set()

    def status(self):
        return {
            "bindings": {k: combo_readable(v) for k, v in self.bindings.items()},
            "errors": dict(self.errors),
        }
