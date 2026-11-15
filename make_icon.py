#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「耳读」的应用图标：闭眼 + 声波。"""

from PIL import Image, ImageDraw

S = 256
AMBER = (240, 180, 41, 255)
AMBER_DIM = (240, 180, 41, 120)


def make(size=S, bg=True):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    if bg:
        # 竖向渐变条再拉伸，比逐像素快
        bar = Image.new("RGB", (1, size))
        for y in range(size):
            t = y / max(1, size - 1)
            bar.putpixel((0, y), (
                int(0x1e + (0x0b - 0x1e) * t),
                int(0x25 + (0x0e - 0x25) * t),
                int(0x33 + (0x12 - 0x33) * t),
            ))
        grad = bar.resize((size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
        img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    k = size / 256.0

    # 主体：一个「耳」字，直接点题，比抽象图形好认
    from PIL import ImageFont
    fsize = int(118 * k)
    font = None
    for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\simhei.ttf"):
        try:
            font = ImageFont.truetype(path, fsize)
            break
        except Exception:
            continue

    if font is not None:
        d.text((size * 0.42, size * 0.45), "耳", font=font,
               fill=AMBER, anchor="mm")
        # 右下角声波，摆在字的空白处，不压笔画
        sx, sy = size * 0.80, size * 0.76
        for i, r in enumerate((17, 31)):
            rr = r * k
            d.arc([sx - rr, sy - rr, sx + rr, sy + rr], start=-42, end=42,
                  fill=AMBER if i == 0 else AMBER_DIM,
                  width=max(2, int(11 * k)))

    return img


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))

    big = make(256)
    big.save(os.path.join(here, "app.png"))

    # 小尺寸单独重画，比缩放清晰
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    frames = [make(s[0]) for s in sizes]
    frames[0].save(os.path.join(here, "app.ico"), format="ICO",
                   sizes=sizes, append_images=frames[1:])
    print("已生成 app.png / app.ico")
