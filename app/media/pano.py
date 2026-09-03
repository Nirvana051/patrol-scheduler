# -*- coding: utf-8 -*-
"""全景（2:1 等距投影）的角度↔像素换算、跨缝裁切、标注，以及 mock 用的合成全景。

约定（docs/task.md §4.1）：图像宽度 ↔ 360°，左边缘 0°，中心 180°；范围 [from, to] 沿 x 增大方向，
from > to 表示跨越左右接缝。
"""
from __future__ import annotations

import io
import math
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc',
    '/usr/share/fonts/truetype/arphic/uming.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def norm_deg(a: float) -> float:
    a = float(a) % 360.0
    return a


def angle_to_x(angle: float, width: int) -> int:
    return int(round(norm_deg(angle) / 360.0 * width)) % max(1, width)


def x_to_angle(x: float, width: int) -> float:
    return (float(x) / width * 360.0) % 360.0


def span_deg(angle_from: float, angle_to: float) -> float:
    """范围宽度（0–360）。from==to 视为整圈。"""
    a, b = norm_deg(angle_from), norm_deg(angle_to)
    if abs(a - b) < 1e-9:
        return 360.0
    return (b - a) % 360.0


def crop_angle_range(img: Image.Image, angle_from: float, angle_to: float, *, pad_deg: float = 0.0,
                     vertical: tuple[float, float] | None = None) -> Image.Image:
    """按角度范围裁切；跨缝时把右段与左段拼接。vertical=(top_frac, bottom_frac) 可再裁掉天顶/地面。"""
    w, h = img.size
    a = norm_deg(angle_from - pad_deg)
    b = norm_deg(angle_to + pad_deg)
    if span_deg(angle_from, angle_to) >= 360.0 - 1e-6:
        out = img.copy()
    else:
        x1, x2 = angle_to_x(a, w), angle_to_x(b, w)
        if x2 <= x1:
            right = img.crop((x1, 0, w, h))
            left = img.crop((0, 0, max(x2, 1), h))
            out = Image.new(img.mode, (right.width + left.width, h))
            out.paste(right, (0, 0))
            out.paste(left, (right.width, 0))
        else:
            out = img.crop((x1, 0, x2, h))
    if vertical:
        t, bt = vertical
        out = out.crop((0, int(h * t), out.width, int(h * bt)))
    return out


def annotate(img: Image.Image, angle_from: float, angle_to: float, *, forward_deg: float = 180.0,
             label: str = '') -> Image.Image:
    """在整张全景上画出范围竖线、刻度与说明（用于检查记录的留档）。"""
    out = img.convert('RGB').copy()
    w, h = out.size
    d = ImageDraw.Draw(out, 'RGBA')
    f = font(max(14, h // 32))
    for deg in range(0, 360, 30):
        x = angle_to_x(deg, w)
        d.line([(x, h - 26), (x, h)], fill=(255, 255, 255, 200), width=1)
        d.text((x + 3, h - 24), f'{deg}°', font=f, fill=(255, 255, 255, 230))
    fx = angle_to_x(forward_deg, w)
    d.line([(fx, 0), (fx, h)], fill=(80, 200, 255, 160), width=2)
    d.text((fx + 4, 4), '机头', font=f, fill=(80, 200, 255, 255))
    x1, x2 = angle_to_x(angle_from, w), angle_to_x(angle_to, w)
    if x2 <= x1:
        d.rectangle([x1, 0, w, h], fill=(255, 200, 0, 40))
        d.rectangle([0, 0, x2, h], fill=(255, 200, 0, 40))
    else:
        d.rectangle([x1, 0, x2, h], fill=(255, 200, 0, 40))
    for x in (x1, x2):
        d.line([(x, 0), (x, h)], fill=(255, 170, 0, 255), width=3)
    txt = f'{norm_deg(angle_from):.0f}° → {norm_deg(angle_to):.0f}°  ({span_deg(angle_from, angle_to):.0f}°)'
    if label:
        txt = label + '  ' + txt
    d.rectangle([8, h // 2 - 18, 8 + f.getlength(txt) + 12, h // 2 + 18], fill=(0, 0, 0, 150))
    d.text((14, h // 2 - 14), txt, font=f, fill=(255, 220, 120, 255))
    return out


# ── 合成全景（mock / 无相机时）──────────────────────────────────────────────
DEFAULT_OBJECTS = [
    {'angle': 200, 'width': 22, 'label': '消防栓柜', 'color': (200, 30, 30), 'kind': 'cabinet'},
    {'angle': 165, 'width': 26, 'label': '地面方格', 'color': (240, 200, 40), 'kind': 'grid'},
    {'angle': 90, 'width': 14, 'label': '安全出口', 'color': (30, 160, 60), 'kind': 'sign'},
    {'angle': 300, 'width': 10, 'label': '立柱', 'color': (120, 120, 130), 'kind': 'pillar'},
    {'angle': 20, 'width': 18, 'label': '配电箱', 'color': (70, 90, 160), 'kind': 'cabinet'},
]


def synth_pano(width: int = 1280, height: int = 640, *, pose: dict | None = None, label: str = 'MOCK 全景',
               objects: list[dict] | None = None, door_open: bool = False, t: float | None = None) -> Image.Image:
    img = Image.new('RGB', (width, height))
    d = ImageDraw.Draw(img)
    horizon = int(height * 0.55)
    for y in range(height):                      # 天空 → 地面的渐变
        if y < horizon:
            k = y / horizon
            c = (int(120 + 100 * k), int(170 + 60 * k), int(230 + 20 * k))
        else:
            k = (y - horizon) / (height - horizon)
            g = int(150 - 70 * k)
            c = (g, g, g + 5)
        d.line([(0, y), (width, y)], fill=c)
    # 墙面基线
    d.rectangle([0, int(height * 0.32), width, horizon], fill=(225, 222, 210))
    d.line([(0, horizon), (width, horizon)], fill=(90, 90, 90), width=2)
    f = font(max(14, height // 30))
    fs = font(max(12, height // 42))
    for o in (objects if objects is not None else DEFAULT_OBJECTS):
        cx = angle_to_x(o['angle'], width)
        half = int(o['width'] / 360 * width / 2)
        top, bottom = int(height * 0.36), int(height * 0.62)
        color = tuple(o['color'])
        if o['kind'] == 'cabinet':
            d.rectangle([cx - half, top, cx + half, bottom], fill=color, outline=(30, 30, 30), width=3)
            door_w = half if not door_open else int(half * 0.35)
            d.rectangle([cx - half + 6, top + 6, cx - half + 6 + door_w * 2 - 12, bottom - 6],
                        outline=(255, 255, 255), width=2)
            if door_open:
                d.text((cx - half, bottom + 4), '门未关', font=fs, fill=(255, 80, 80))
        elif o['kind'] == 'grid':
            gy0, gy1 = horizon + 10, int(height * 0.85)
            d.rectangle([cx - half, gy0, cx + half, gy1], outline=color, width=4)
            for k in range(1, 4):
                yy = gy0 + (gy1 - gy0) * k // 4
                d.line([(cx - half, yy), (cx + half, yy)], fill=color, width=2)
            d.line([(cx, gy0), (cx, gy1)], fill=color, width=2)
        elif o['kind'] == 'sign':
            d.rectangle([cx - half, top - 20, cx + half, top + 20], fill=color)
            d.text((cx - half + 4, top - 16), 'EXIT', font=fs, fill=(255, 255, 255))
        else:
            d.rectangle([cx - half, int(height * 0.2), cx + half, int(height * 0.8)], fill=color)
        d.text((cx - half, top - 44), f"{o['label']} {o['angle']}°", font=fs, fill=(20, 20, 20))
    # 刻度
    for deg in range(0, 360, 30):
        x = angle_to_x(deg, width)
        d.line([(x, height - 30), (x, height)], fill=(255, 255, 255), width=1)
        d.text((x + 3, height - 28), f'{deg}°', font=fs, fill=(255, 255, 255))
    ts = time.strftime('%H:%M:%S', time.localtime(t or time.time()))
    head = f'{label}  {ts}'
    if pose:
        head += f"   x={pose.get('x', 0):.2f} y={pose.get('y', 0):.2f} yaw={math.degrees(pose.get('yaw', 0)):.0f}°"
    d.rectangle([0, 0, width, 34], fill=(0, 0, 0))
    d.text((10, 5), head, font=f, fill=(255, 255, 255))
    return img


def to_jpeg(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='JPEG', quality=quality)
    return buf.getvalue()
