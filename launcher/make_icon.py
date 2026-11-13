# -*- coding: utf-8 -*-
"""生成 AI早知道 的图标：launcher/app.ico（给 exe 用）和 launcher/icon.png（给窗口/任务栏用）。需要 Pillow。

造型：深蓝到靛紫的圆角方块里，一轮暖色朝阳从地平线升起（早），右上角一颗四角星光（AI / 知道）。
不放文字，16px 小图标也能认出来。
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
S = 1024  # 先画大图再缩小，边缘更平滑


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def gradient(size, c1, c2, diagonal=True):
    g = Image.new("RGBA", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if diagonal else y / (size - 1)
            px[x, y] = lerp(c1, c2, t) + (255,)
    return g


def star(d, cx, cy, r, color, thin=0.26):
    """四角星：上下左右四个尖，腰部收细。"""
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rr = r if i % 2 == 0 else r * thin
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=color)


def draw():
    pad, rad = 40, 230
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=rad, fill=255)

    # 底：夜空到黎明的斜向渐变
    bg = Image.new("RGBA", (S, S))
    gpx = bg.load()
    top, mid, bot = (24, 44, 120), (66, 56, 190), (122, 78, 220)
    for y in range(S):
        t = y / (S - 1)
        c = lerp(top, mid, t / 0.6) if t < 0.6 else lerp(mid, bot, (t - 0.6) / 0.4)
        for x in range(S):
            gpx[x, y] = c + (255,)

    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    horizon = 690
    cx, r = S // 2, 250

    # 朝阳光晕
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - r - 120, horizon - r - 120, cx + r + 120, horizon + r + 120],
                                 fill=(255, 170, 70, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    layer.alpha_composite(glow)

    # 光芒（地平线以上的放射短线）
    for i in range(7):
        ang = math.pi + math.pi * (i + 1) / 8
        x1, y1 = cx + (r + 40) * math.cos(ang), horizon + (r + 40) * math.sin(ang)
        x2, y2 = cx + (r + 120) * math.cos(ang), horizon + (r + 120) * math.sin(ang)
        d.line([(x1, y1), (x2, y2)], fill=(255, 214, 120, 255), width=34)
        d.ellipse([x1 - 17, y1 - 17, x1 + 17, y1 + 17], fill=(255, 214, 120, 255))
        d.ellipse([x2 - 17, y2 - 17, x2 + 17, y2 + 17], fill=(255, 214, 120, 255))

    # 太阳本体：上黄下橙的渐变半圆
    sun = gradient(S, (255, 214, 92), (255, 128, 64), diagonal=False)
    sun_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sun_mask).ellipse([cx - r, horizon - r, cx + r, horizon + r], fill=255)
    ImageDraw.Draw(sun_mask).rectangle([0, horizon, S, S], fill=0)
    layer.paste(sun, (0, 0), sun_mask)

    # 地平线与下方两道「信息流」短线
    d.rounded_rectangle([170, horizon - 14, S - 170, horizon + 14], radius=14, fill=(255, 255, 255, 255))
    d.rounded_rectangle([250, horizon + 70, S - 250, horizon + 96], radius=13, fill=(255, 255, 255, 150))
    d.rounded_rectangle([330, horizon + 140, S - 330, horizon + 164], radius=12, fill=(255, 255, 255, 90))

    # AI 星光
    star(d, 760, 250, 120, (255, 255, 255, 255))
    star(d, 640, 170, 48, (255, 255, 255, 215))

    bg.alpha_composite(layer)
    img.paste(bg, (0, 0), mask)
    return img


if __name__ == "__main__":
    im = draw()
    im.resize((256, 256), Image.LANCZOS).save(HERE / "icon.png")
    im.resize((256, 256), Image.LANCZOS).save(
        HERE / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    # 预览：各尺寸并排，检查小尺寸是否还认得出
    prev = Image.new("RGBA", (256 + 128 + 64 + 32 + 16 + 100, 270), (32, 32, 32, 255))
    x = 10
    for s in (256, 128, 64, 32, 16):
        prev.alpha_composite(im.resize((s, s), Image.LANCZOS), (x, 10))
        x += s + 20
    prev.save(HERE / "_preview.png")
    print("ok")
