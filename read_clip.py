#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
朗读 —— 把文字念出来。
语音：微软 Edge 神经网络语音（与 Edge 浏览器「大声朗读」同源）。

单独用：双击桌面「朗读剪贴板.vbs」，念剪贴板内容。
常驻用：hotkey.py 注册 Ctrl+Alt+S，随时念。
"""

import os
import re
import sys
import time
import ctypes
import asyncio
import queue
import tempfile
import threading
import subprocess

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# pythonw 下没有控制台，stdout/stderr 为 None，任何输出都会炸 → 先兜住
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# ── 可调参数 ────────────────────────────────────────────────
VOICE = "zh-CN-XiaoxiaoNeural"   # 晓晓(女/自然) 云希(男/年轻) 云健(男/浑厚) 晓伊(女/温柔)
RATE = "+8%"                     # 语速，可写 "+20%" / "-10%"
VOLUME = 0.95
CHUNK = 160                      # 每段字数，越小开口越快、衔接越碎
PIDFILE = os.path.join(tempfile.gettempdir(), "read_aloud.pid")
LASTLOG = os.path.join(tempfile.gettempdir(), "read_aloud_last.log")
# ────────────────────────────────────────────────────────────


def alive(pid):
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, timeout=5,
        )
        return str(pid).encode() in (r.stdout or b"")   # 不解码，绕开 GBK/UTF-8 差异
    except Exception:
        return False


def claim_slot():
    """已有实例在念就先停掉它，返回 True 表示「本次只做停止」。"""
    if os.path.exists(PIDFILE):
        try:
            pid = int(open(PIDFILE, encoding="utf-8").read().strip())
            if pid != os.getpid() and alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
                try:
                    os.remove(PIDFILE)
                except OSError:
                    pass
                return True
        except (ValueError, OSError):
            pass
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return False


def release_slot():
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def beep(kind="info"):
    try:
        import winsound
        winsound.MessageBeep(0x40 if kind == "info" else 0x30)
    except Exception:
        pass


def clean(t):
    """去掉 markdown 噪音，只留下值得念的文字。"""
    t = re.sub(r"```.*?```", "\n", t, flags=re.S)          # 代码块整段跳过
    t = re.sub(r"`([^`\n]*)`", r"\1", t)                    # 行内代码
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)              # 图片
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)          # 链接留文字
    t = re.sub(r"https?://\S+", "", t)                      # 裸链接
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)     # 标题井号
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)          # 列表符号
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)              # 引用符
    t = re.sub(r"^\s*[|\-:\s]{3,}$", "", t, flags=re.M)     # 表格分隔行 / 横线
    t = re.sub(r"\|", " ", t)                               # 表格竖线
    t = re.sub(r"(\*\*|__|\*|_|~~)", "", t)                 # 强调符
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s+([，。！？、；：」』）])", r"\1", t)      # 标点前多余空格
    t = re.sub(r"([（「『])\s+", r"\1", t)
    t = re.sub(r"\n{2,}", "\n", t)
    t = re.sub(r"\n\s+", "\n", t)
    return t.strip()


def split_text(t, limit=CHUNK):
    """
    每行独立切分、合并，不跨行——原文的段落边界就是朗读的停顿点，
    跨行合并会把"第二段是成本。"这种句子粘到上一段末尾。
    """
    chunks = []
    for ln in t.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        buf = ""
        for s in re.split(r"(?<=[。！？!?；;…])", ln):
            s = s.strip()
            if not s:
                continue
            while len(s) > limit:
                if buf:
                    chunks.append(buf)
                    buf = ""
                # 优先切在逗号/顿号处，别把"了。"这种残句单独留一行
                cut = limit
                for i in range(min(limit, len(s) - 1), max(limit - 25, 1), -1):
                    if s[i - 1] in "，、；：,;":
                        cut = i
                        break
                chunks.append(s[:cut])
                s = s[cut:].lstrip()
                # 剩下的零头很短就并进上一段，避免出现孤零零的一小句
                if 0 < len(s) <= 8 and chunks:
                    chunks[-1] += s
                    s = ""
            if not buf:
                buf = s
            elif len(buf) + len(s) <= limit:
                buf += s
            else:
                chunks.append(buf)
                buf = s
        if buf:
            chunks.append(buf)
    return [c for c in chunks if re.search(r"[\w\u4e00-\u9fff]", c)]


def _synth_edge(text, path, voice=None, rate=None):
    """微软在线语音，音色好。成功返回 True。"""
    import edge_tts
    # 走 WebSocket，经代理容易被掐断，临时绕开
    saved = {}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    try:
        asyncio.run(
            edge_tts.Communicate(text, voice or VOICE, rate=rate or RATE,
                                 volume="+0%").save(path)
        )
    finally:
        os.environ.update(saved)
    return os.path.exists(path) and os.path.getsize(path) > 512


def _synth_sapi(text, wav_path):
    """系统自带语音，音色一般但离线、不限流。成功返回 True。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Format.Type = 22                    # 22kHz 16bit 单声道
        stream.Open(wav_path, 3, False)            # 3 = 创建并写入
        speaker.AudioOutputStream = stream
        try:
            speaker.Speak(text)
        finally:
            stream.Close()
    finally:
        pythoncom.CoUninitialize()
    return os.path.exists(wav_path) and os.path.getsize(wav_path) > 512


def synth(text, path, voice=None, rate=None):
    """
    合成一段语音到 path（.mp3）。
    优先用微软在线语音；失败时自动退回系统自带引擎。
    返回实际音频文件路径（可能是 .wav），全都失败返回 None。
    """
    try:
        if _synth_edge(text, path, voice, rate):
            return path
    except Exception:
        pass

    wav = os.path.splitext(path)[0] + ".wav"
    try:
        if _synth_sapi(text, wav):
            return wav
    except Exception:
        pass
    return None


_winmm = None
_alias_seq = 0


def _mci(cmd, want=False):
    """直接调 Windows MCI。不碰 SDL，不建窗口，不干扰消息循环。"""
    global _winmm
    if _winmm is None:
        _winmm = ctypes.WinDLL("winmm")
        _winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                          ctypes.c_uint, ctypes.c_void_p]
    buf = ctypes.create_unicode_buffer(256)
    r = _winmm.mciSendStringW(cmd, buf, 255, None)
    return (r, buf.value) if want else r


def play_file(path, stop=None):
    """播一个音频文件，stop 置位即打断。返回是否播放成功。"""
    global _alias_seq
    _alias_seq += 1
    alias = f"ra{_alias_seq}"
    if _mci(f'open "{path}" type mpegvideo alias {alias}') != 0:
        return False
    try:
        try:
            _mci(f"setaudio {alias} volume to {int(VOLUME * 1000)}")
        except Exception:
            pass
        if _mci(f"play {alias}") != 0:
            return False
        time.sleep(0.12)                      # 给解码器起播的时间
        while True:
            if stop is not None and stop.is_set():
                _mci(f"stop {alias}")
                break
            _, mode = _mci(f"status {alias} mode", want=True)
            if mode not in ("playing", "starting"):
                break
            time.sleep(0.08)
        return True
    finally:
        _mci(f"close {alias}")


def clip_text():
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


def speak_text(raw, stop=None):
    """念一段文字。stop 是 threading.Event，置位即打断。返回统计 dict，念不出来返回 None。"""
    t0 = time.time()
    text = clean(raw)
    if not text or not re.search(r"[\w\u4e00-\u9fff]", text):
        return None
    chunks = split_text(text)
    if not chunks:
        return None

    tmpdir = tempfile.mkdtemp(prefix="read_aloud_")
    q = queue.Queue(maxsize=3)
    fail = []
    halt = threading.Event()

    def producer():
        for i, c in enumerate(chunks):
            if halt.is_set():
                break
            path = os.path.join(tmpdir, f"{i:04d}.mp3")
            actual = None
            for attempt in range(2):
                if halt.is_set():
                    break
                try:
                    actual = synth(c, path)
                    if actual:
                        break
                except Exception as e:
                    if attempt == 1:
                        fail.append(str(e))
            if actual:
                try:
                    q.put(actual, timeout=0.5)
                except queue.Full:
                    pass
        try:
            q.put(None, timeout=1)
        except queue.Full:
            pass

    worker = threading.Thread(target=producer, daemon=True)
    worker.start()

    try:
        while True:
            if stop is not None and stop.is_set():
                break
            try:
                path = q.get(timeout=0.25)
            except queue.Empty:
                if not worker.is_alive():
                    break                    # 合成收工、队列也空了 → 不再干等
                continue
            if path is None:
                break
            try:
                play_file(path, stop)
            except Exception as e:
                fail.append(f"播放:{e}")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
    finally:
        halt.set()
        try:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", tmpdir],
                           capture_output=True, timeout=10)
        except Exception:
            pass

    info = {
        "chunks": len(chunks),
        "chars": len(text),
        "secs": round(time.time() - t0, 1),
        "fail": len(fail),
    }
    try:
        with open(LASTLOG, "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 段数={info['chunks']} "
                    f"字数={info['chars']} 用时={info['secs']}s "
                    f"合成失败={info['fail']}\n")
            f.write(text[:400] + "\n")
    except OSError:
        pass
    return info


def main():
    if claim_slot():
        return 0                                    # 上一段还在念 → 这次只负责打断

    text = ""
    if len(sys.argv) > 1:
        for p in sys.argv[1:]:
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    text += f.read() + "\n"
            except OSError:
                pass
    if not text.strip():
        text = clip_text()

    r = speak_text(text)
    release_slot()
    if r is None:
        beep("warn")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        try:
            import traceback
            with open(os.path.join(tempfile.gettempdir(), "read_aloud_error.log"),
                      "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ")
                        + traceback.format_exc() + "\n")
        except Exception:
            pass
        beep("warn")
        sys.exit(1)
