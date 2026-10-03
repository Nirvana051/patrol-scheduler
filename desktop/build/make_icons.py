#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标与托盘图标（与网页 favicon 同一个图形：蓝底圆角方块 + 白色圆环与圆点）。

    .venv/bin/python desktop/build/make_icons.py
产出：build/icon.png（512，electron-builder 由它生成 ico/icns）、assets/icon.png、assets/tray.png（32）、
assets/tray@2x.png（64）、assets/trayTemplate.png / @2x（macOS 模板图：黑色透明底）。
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / 'assets'
BLUE = (31, 94, 255, 255)


def app_icon(size: int) -> Image.Image:
    s = size * 4                                   # 4 倍超采样再缩小，边缘平滑
    im = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 7 / 32), fill=BLUE)
    c, r, w = s / 2, s * 7 / 32, s * 3 / 32
    d.ellipse((c - r, c - r, c + r, c + r), outline=(255, 255, 255, 255), width=int(w))
    r2 = s * 2.5 / 32
    d.ellipse((c - r2, c - r2, c + r2, c + r2), fill=(255, 255, 255, 255))
    return im.resize((size, size), Image.LANCZOS)


def tray_icon(size: int, template: bool) -> Image.Image:
    s = size * 4
    im = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    color = (0, 0, 0, 255) if template else BLUE
    c, r, w = s / 2, s * 0.36, s * 0.14
    d.ellipse((c - r, c - r, c + r, c + r), outline=color, width=int(w))
    r2 = s * 0.12
    d.ellipse((c - r2, c - r2, c + r2, c + r2), fill=color)
    return im.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    app_icon(512).save(HERE / 'icon.png')
    app_icon(256).save(ASSETS / 'icon.png')
    tray_icon(32, False).save(ASSETS / 'tray.png')
    tray_icon(64, False).save(ASSETS / 'tray@2x.png')
    tray_icon(22, True).save(ASSETS / 'trayTemplate.png')
    tray_icon(44, True).save(ASSETS / 'trayTemplate@2x.png')
    print('icons written to', HERE, 'and', ASSETS)


if __name__ == '__main__':
    main()
