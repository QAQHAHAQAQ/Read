#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
截图取字 —— 全屏选区 + Windows 本地 OCR（离线，不联网）。
"""

import os
import re
import time
import asyncio
import tempfile


def grab_screen():
    """抓主屏。返回 PIL.Image。"""
    from PIL import ImageGrab
    return ImageGrab.grab()


def pick_region():
    """弹一个全屏半透明遮罩，拖拽选框。返回 (left, top, right, bottom) 或 None。"""
    import tkinter as tk

    res = {"box": None}

    root = tk.Tk()
    root.overrideredirect(True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    root.attributes("-alpha", 0.38)
    root.attributes("-topmost", True)
    root.configure(bg="black", cursor="crosshair")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)

    hint = canvas.create_text(
        root.winfo_screenwidth() // 2, 46,
        text="拖拽框选要朗读的区域   ·   Esc 取消",
        fill="#ffd166", font=("Microsoft YaHei UI", 15),
    )

    st = {"x": 0, "y": 0, "rect": None, "label": None}

    def on_press(e):
        st["x"], st["y"] = e.x, e.y
        canvas.delete(hint)
        if st["rect"]:
            canvas.delete(st["rect"])
        if st["label"]:
            canvas.delete(st["label"])
        st["rect"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#ffcb52", width=2, dash=(5, 3))
        st["label"] = canvas.create_text(
            e.x + 6, e.y - 10, text="", fill="#ffd166", anchor="sw",
            font=("Consolas", 11))

    def on_move(e):
        if not st["rect"]:
            return
        canvas.coords(st["rect"], st["x"], st["y"], e.x, e.y)
        w, h = abs(e.x - st["x"]), abs(e.y - st["y"])
        canvas.coords(st["label"], min(st["x"], e.x) + 6, min(st["y"], e.y) - 10)
        canvas.itemconfigure(st["label"], text=f"{w} × {h}")

    def on_release(e):
        x1, y1 = min(st["x"], e.x), min(st["y"], e.y)
        x2, y2 = max(st["x"], e.x), max(st["y"], e.y)
        if x2 - x1 >= 8 and y2 - y1 >= 8:
            res["box"] = (x1, y1, x2, y2)
        root.destroy()

    def on_esc(_=None):
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Button-3>", on_esc)          # 右键也能取消
    root.bind("<Escape>", on_esc)

    # 抢前台：不抢的话 Esc 和拖拽都收不到
    root.update_idletasks()
    root.update()
    root.focus_force()
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
    except Exception:
        pass

    try:
        root.mainloop()
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass
    return res["box"]


def tidy_ocr(t):
    """Windows OCR 会把中文按字拆开加空格，这里收拢回正常句子。"""
    if not t:
        return ""
    # 汉字之间的空格
    t = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", t)
    # 全角标点前的空格
    t = re.sub(r"[ \t]+([，。！？；：、）》」』%])", r"\1", t)
    # 全角标点后紧跟汉字的空格
    t = re.sub(r"(?<=[，。！？；：、）】」』])[ \t]+(?=[\u4e00-\u9fff])", "", t)
    # 左括号后的空格
    t = re.sub(r"([（《「『])[ \t]+", r"\1", t)
    # 中文语境里的西文句点还原成句号
    t = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]*．[ \t]*", "。", t)
    # 英文数字后面的．→ 半角句点
    t = re.sub(r"(?<=[A-Za-z0-9])[ \t]*．[ \t]*", ". ", t)
    # 汉字与英文数字之间保留一个空格，其余多空格压缩
    t = re.sub(r"[ \t]{2,}", " ", t)
    # 高频形近字纠正（只在明显是误识时替换）
    for bad, good in (("囗", "口"), ("⼝", "口"), ("⼀", "一")):
        t = t.replace(bad, good)
    lines = [ln.strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def ocr_image(img):
    """对 PIL 图片做 OCR，返回整理后的文本。"""
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile, FileAccessMode
    from PIL import Image

    # 小图先放大，Windows OCR 对小字号不敏感
    if img.width < 1100:
        scale = 1100 / max(1, img.width)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    tmp = os.path.join(tempfile.gettempdir(), f"ocr_{int(time.time()*1000)}.png")
    img.save(tmp)

    async def run():
        eng = OcrEngine.try_create_from_language(Language("zh-Hans"))
        if eng is None:
            eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            return ""
        f = await StorageFile.get_file_from_path_async(tmp)
        stream = await f.open_async(FileAccessMode.READ)
        dec = await BitmapDecoder.create_async(stream)
        bmp = await dec.get_software_bitmap_async()
        res = await eng.recognize_async(bmp)
        return "\n".join(line.text for line in res.lines)

    try:
        return tidy_ocr(asyncio.run(run()))
    except Exception:
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def capture_text():
    """
    完整流程：遮罩选区域 → 裁剪截图 → OCR。
    返回 (文本, 说明)。文本为空表示没取到。
    """
    box = pick_region()
    if not box:
        return "", "已取消"
    try:
        shot = grab_screen()
        region = shot.crop(box)
        text = ocr_image(region)
        if not text.strip():
            return "", "这块区域没识别出文字"
        return text, f"识别 {len(region.size)}".replace("(", "").replace(")", "")
    except Exception as e:
        return "", f"出错: {e}"
