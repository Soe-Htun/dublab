"""🎨 Cartoon mode: an original short Burmese comedy drawn as a simple 2D cartoon.

gemini.write_cartoon_script() writes the story (characters, scenes, lines with a mood and an action);
this file voices it (Burmese TTS, a voice per character), draws every frame with Pillow (cute big-head
characters on drawn backgrounds, mouths moving with the voice, jumps / shakes / ghosts popping up),
adds sound effects and a soft music bed, and encodes the mp4 with ffmpeg.
"""
import math
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import dub

SR = 24000                       # audio sample rate
FPS = 24
VOICE_M, VOICE_F = "my-MM-ThihaNeural", "my-MM-NilarNeural"
NIGHT_BGS = {"graveyard", "forest_night", "street_night", "room_night"}
FLOATERS = {"ghost", "ghost_lady"}
KID_SCALE = {"boy": 0.78, "girl": 0.78, "dog": 0.62, "cat": 0.55}
DEFAULT_COLORS = ["#3a7bd5", "#e0574f", "#2fa67a", "#d99a2b", "#8e5bd6", "#e07ab0"]
SKIN = {"man": (238, 192, 150), "woman": (245, 206, 168), "boy": (240, 196, 152), "girl": (246, 208, 172),
        "grandpa": (230, 186, 146), "grandma": (238, 198, 162), "ghost_lady": (222, 234, 228)}

# ------------------------------------------------------------------ helpers


def _hex(c, default=(58, 123, 213)):
    try:
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:  # noqa: BLE001
        return default


def _shade(rgb, f):
    return tuple(max(0, min(255, int(v * f))) for v in rgb[:3])


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _grad(d, box, top, bottom):
    x0, y0, x1, y1 = box
    for y in range(int(y0), int(y1)):
        d.line([(x0, y), (x1, y)], fill=_mix(top, bottom, (y - y0) / max(1, y1 - y0)))


def _clamp(v, a=0.0, b=1.0):
    return max(a, min(b, v))


# ------------------------------------------------------------------ backgrounds
# drawn at 2x (smooth edges) and scaled down once per scene; ground (feet) at 74% of the height


def _stars(d, w, h, rnd, n=70, top=0.55):
    for _ in range(n):
        x, y, r = rnd.uniform(0, w), rnd.uniform(0, h * top), rnd.choice((2, 2, 3, 4))
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 250, 220))


def _moon(img, x, y, r):
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2), fill=(255, 245, 200, 50))
    glow = glow.filter(ImageFilter.GaussianBlur(r * 0.6))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)
    d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 246, 206))
    d.ellipse((x - r * 0.45, y - r * 0.3, x - r * 0.15, y), fill=(238, 228, 186))
    d.ellipse((x + r * 0.2, y + r * 0.25, x + r * 0.5, y + r * 0.5), fill=(238, 228, 186))


def _tree_dead(d, x, gy, s, col):
    d.polygon([(x - 18 * s, gy), (x + 18 * s, gy), (x + 8 * s, gy - 260 * s), (x - 8 * s, gy - 260 * s)], fill=col)
    for (ax, ay, bx, by) in ((0, -200, -90, -300), (0, -160, 80, -260), (-50, -250, -100, -280),
                             (40, -215, 110, -230), (0, -255, 20, -340)):
        d.line([(x + ax * s, gy + ay * s), (x + bx * s, gy + by * s)], fill=col, width=int(12 * s))


def _tomb(d, x, gy, s, col):
    w, hh = 70 * s, 110 * s
    d.rounded_rectangle((x - w / 2, gy - hh, x + w / 2, gy + 6 * s), radius=int(w / 2), fill=col,
                        outline=_shade(col, 0.6), width=int(5 * s))
    d.line([(x - w * 0.25, gy - hh * 0.55), (x + w * 0.25, gy - hh * 0.55)], fill=_shade(col, 0.6), width=int(5 * s))
    d.line([(x - w * 0.25, gy - hh * 0.38), (x + w * 0.2, gy - hh * 0.38)], fill=_shade(col, 0.6), width=int(5 * s))


def _house_sil(d, x, gy, w, hh, col, win, rnd):
    d.rectangle((x, gy - hh, x + w, gy), fill=col)
    d.polygon([(x - w * 0.08, gy - hh), (x + w / 2, gy - hh - w * 0.45), (x + w * 1.08, gy - hh)], fill=col)
    for i in range(2):
        for j in range(2):
            if rnd.random() < 0.7:
                wx, wy = x + w * (0.18 + 0.42 * i), gy - hh * (0.8 - 0.4 * j)
                d.rectangle((wx, wy, wx + w * 0.22, wy + hh * 0.2), fill=win)


def _palm(d, x, gy, s):
    d.line([(x, gy), (x + 30 * s, gy - 330 * s)], fill=(120, 84, 50), width=int(22 * s))
    tx, ty = x + 30 * s, gy - 330 * s
    for ang in (-160, -120, -60, -20, 200, 90):
        a = math.radians(ang)
        ex, ey = tx + math.cos(a) * 150 * s, ty + math.sin(a) * 70 * s + 40 * s
        d.line([(tx, ty), ((tx + ex) / 2, min(ty, ey) - 30 * s), (ex, ey)], fill=(46, 140, 70), width=int(26 * s),
               joint="curve")


def _cloud(d, x, y, s, col=(255, 255, 255)):
    for dx, dy, r in ((0, 0, 50), (55, -20, 60), (115, 0, 48), (55, 18, 50)):
        d.ellipse((x + (dx - r) * s, y + (dy - r) * s, x + (dx + r) * s, y + (dy + r) * s), fill=col)


def _window(d, box, sky_top, sky_bot, night, frame=(120, 84, 56)):
    x0, y0, x1, y1 = box
    _grad(d, box, sky_top, sky_bot)
    if night:
        r = (x1 - x0) * 0.13
        d.ellipse((x1 - r * 3, y0 + r, x1 - r, y0 + r * 3), fill=(255, 246, 206))
    else:
        _cloud(d, x0 + (x1 - x0) * 0.15, y0 + (y1 - y0) * 0.35, (x1 - x0) / 520)
    fw = (x1 - x0) * 0.04
    d.rectangle(box, outline=frame, width=int(fw * 1.6))
    d.line([((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1)], fill=frame, width=int(fw))
    d.line([(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)], fill=frame, width=int(fw))


def draw_bg(kind, W, H, seed=1):
    w, h = W * 2, H * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    gy = h * 0.74                       # where the feet stand
    s = h / 2560                        # 1.0 at 1280-high (2x)
    sx = w / 1440
    if kind == "graveyard":
        _grad(d, (0, 0, w, gy), (8, 12, 40), (44, 48, 92))
        _stars(d, w, h, rnd)
        _moon(img, w * 0.78, h * 0.16, 90 * s)
        d = ImageDraw.Draw(img)
        d.polygon([(0, gy - 260 * s), (w * 0.3, gy - 380 * s), (w * 0.6, gy - 250 * s), (w, gy - 360 * s),
                   (w, gy), (0, gy)], fill=(26, 30, 60))
        _grad(d, (0, gy - 120 * s, w, h), (30, 46, 66), (18, 26, 40))
        _tree_dead(d, w * 0.12, gy - 60 * s, 1.5 * s * 1.6, (34, 26, 30))
        for fx, fy, sc in ((0.3, -80, 0.8), (0.55, -95, 0.7), (0.82, -70, 0.9), (0.68, 40, 1.15), (0.08, 60, 1.2),
                           (0.92, 70, 1.1)):
            _tomb(d, w * fx, gy + fy * s, sc * s * 1.6, (126, 132, 152))
        fog = Image.new("RGBA", img.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(fog)
        for _ in range(14):
            fx, fy = rnd.uniform(-100, w), gy + rnd.uniform(-120, 60) * s
            fd.ellipse((fx, fy, fx + rnd.uniform(300, 600) * sx, fy + 90 * s), fill=(200, 210, 240, 40))
        img.alpha_composite(fog.filter(ImageFilter.GaussianBlur(30 * s)))
    elif kind == "forest_night":
        _grad(d, (0, 0, w, gy), (6, 18, 30), (24, 48, 60))
        _stars(d, w, h, rnd, 40, 0.35)
        _moon(img, w * 0.25, h * 0.13, 70 * s)
        d = ImageDraw.Draw(img)
        for layer, col in ((0, (16, 40, 40)), (1, (10, 28, 28))):
            for i in range(7):
                x = w * (i / 6) + rnd.uniform(-60, 60) * sx + layer * 90 * sx
                tw = (70 + layer * 30) * s
                d.rectangle((x - tw / 2, gy - 900 * s, x + tw / 2, gy + 20 * s), fill=_shade(col, 0.8))
                for k in range(4):
                    r = (170 + 40 * layer) * s
                    cx, cy = x + rnd.uniform(-90, 90) * s, gy - (850 + rnd.uniform(-120, 200)) * s
                    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
        _grad(d, (0, gy - 60 * s, w, h), (24, 56, 36), (12, 30, 20))
        for _ in range(26):
            x, y = rnd.uniform(0, w), rnd.uniform(h * 0.25, gy)
            d.ellipse((x - 5 * s, y - 5 * s, x + 5 * s, y + 5 * s), fill=(250, 240, 120))
    elif kind == "street_night":
        _grad(d, (0, 0, w, gy), (12, 14, 44), (60, 46, 96))
        _stars(d, w, h, rnd, 50, 0.3)
        _moon(img, w * 0.82, h * 0.1, 60 * s)
        d = ImageDraw.Draw(img)
        x = -40 * sx
        while x < w:
            hw, hh = rnd.uniform(240, 380) * sx, rnd.uniform(380, 640) * s
            _house_sil(d, x, gy - 120 * s, hw, hh, (28, 26, 54), (250, 210, 110), rnd)
            x += hw + rnd.uniform(30, 80) * sx
        _grad(d, (0, gy - 130 * s, w, h), (70, 70, 84), (44, 44, 56))
        d.rectangle((0, gy - 130 * s, w, gy - 110 * s), fill=(110, 110, 120))
        for i in range(6):
            d.rectangle((w * i / 5 - 70 * sx, gy + 260 * s, w * i / 5 + 70 * sx, gy + 285 * s), fill=(220, 210, 150))
        lx = w * 0.86
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((lx - 260 * s, gy - 980 * s, lx + 260 * s, gy - 460 * s), fill=(255, 220, 130, 70))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60 * s)))
        d = ImageDraw.Draw(img)
        d.rectangle((lx - 12 * s, gy - 720 * s, lx + 12 * s, gy - 110 * s), fill=(40, 40, 50))
        d.rounded_rectangle((lx - 50 * s, gy - 770 * s, lx + 50 * s, gy - 700 * s), radius=int(20 * s), fill=(255, 226, 140))
    elif kind in ("room_night", "room_day"):
        night = kind == "room_night"
        wall = (52, 48, 92) if night else (246, 230, 196)
        _grad(d, (0, 0, w, gy - 160 * s), _shade(wall, 0.85), wall)
        floor = (70, 50, 46) if night else (190, 140, 92)
        d.rectangle((0, gy - 160 * s, w, h), fill=floor)
        for i in range(10):
            yy = gy - 160 * s + i * 80 * s
            d.line([(0, yy), (w, yy)], fill=_shade(floor, 0.85), width=int(5 * s))
        d.rectangle((0, gy - 175 * s, w, gy - 155 * s), fill=_shade(wall, 0.7))
        wb = (w * 0.56, h * 0.12, w * 0.92, h * 0.38)
        _window(d, wb, (10, 14, 50) if night else (110, 180, 240), (40, 40, 100) if night else (190, 225, 250), night)
        d.rectangle((wb[0] - 40 * sx, wb[1] - 30 * s, wb[0] + 30 * sx, wb[3] + 80 * s), fill=(150, 60, 70) if night else (210, 90, 80))
        d.rectangle((wb[2] - 30 * sx, wb[1] - 30 * s, wb[2] + 40 * sx, wb[3] + 80 * s), fill=(150, 60, 70) if night else (210, 90, 80))
        fx0, fy0 = w * 0.1, h * 0.14
        d.rectangle((fx0, fy0, fx0 + 260 * sx, fy0 + 200 * s), fill=(140, 96, 60))
        d.rectangle((fx0 + 18 * sx, fy0 + 18 * s, fx0 + 242 * sx, fy0 + 182 * s), fill=(150, 200, 170) if not night else (60, 80, 90))
        d.polygon([(fx0 + 30 * sx, fy0 + 170 * s), (fx0 + 110 * sx, fy0 + 70 * s), (fx0 + 170 * sx, fy0 + 140 * s),
                   (fx0 + 200 * sx, fy0 + 110 * s), (fx0 + 235 * sx, fy0 + 170 * s)], fill=(70, 130, 90) if not night else (40, 60, 60))
        if night:                                     # a small lamp glowing
            glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow).ellipse((w * 0.05, gy - 620 * s, w * 0.35, gy - 260 * s), fill=(255, 210, 120, 60))
            img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(50 * s)))
            d = ImageDraw.Draw(img)
            d.rectangle((w * 0.17, gy - 330 * s, w * 0.23, gy - 170 * s), fill=(90, 60, 50))
            d.polygon([(w * 0.13, gy - 330 * s), (w * 0.27, gy - 330 * s), (w * 0.24, gy - 440 * s), (w * 0.16, gy - 440 * s)],
                      fill=(255, 214, 140))
        else:
            d.rounded_rectangle((w * 0.08, gy - 330 * s, w * 0.2, gy - 170 * s), radius=int(20 * s), fill=(196, 96, 60))
            for k in range(5):
                a = math.radians(-150 + k * 30)
                d.ellipse((w * 0.14 + math.cos(a) * 90 * s - 40 * s, gy - 360 * s + math.sin(a) * 90 * s - 40 * s,
                           w * 0.14 + math.cos(a) * 90 * s + 40 * s, gy - 360 * s + math.sin(a) * 90 * s + 40 * s),
                          fill=(60, 150, 80))
    elif kind == "village_day":
        _grad(d, (0, 0, w, gy), (90, 170, 240), (200, 230, 250))
        d.ellipse((w * 0.08, h * 0.06, w * 0.08 + 150 * s, h * 0.06 + 150 * s), fill=(255, 222, 90))
        _cloud(d, w * 0.45, h * 0.1, 1.3 * s * 1.6)
        _cloud(d, w * 0.75, h * 0.2, 1.0 * s * 1.6)
        d.polygon([(0, gy - 300 * s), (w * 0.25, gy - 460 * s), (w * 0.5, gy - 320 * s), (w * 0.78, gy - 480 * s),
                   (w, gy - 330 * s), (w, gy), (0, gy)], fill=(120, 180, 110))
        hx, hy = w * 0.6, gy - 140 * s                 # a bamboo house on stilts
        for k in range(5):
            d.rectangle((hx + k * 110 * sx, hy - 160 * s, hx + k * 110 * sx + 16 * sx, hy + 20 * s), fill=(110, 76, 44))
        d.rectangle((hx - 20 * sx, hy - 480 * s, hx + 470 * sx, hy - 160 * s), fill=(214, 176, 112))
        for k in range(12):
            d.line([(hx - 20 * sx + k * 41 * sx, hy - 480 * s), (hx - 20 * sx + k * 41 * sx, hy - 160 * s)],
                   fill=(184, 146, 86), width=int(4 * s))
        d.polygon([(hx - 90 * sx, hy - 470 * s), (hx + 225 * sx, hy - 700 * s), (hx + 540 * sx, hy - 470 * s)],
                  fill=(150, 104, 56))
        d.rectangle((hx + 170 * sx, hy - 400 * s, hx + 290 * sx, hy - 160 * s), fill=(110, 76, 44))
        _palm(d, w * 0.14, gy - 120 * s, 1.5 * s * 1.6)
        _grad(d, (0, gy - 140 * s, w, h), (200, 168, 110), (176, 140, 88))
        for _ in range(40):
            x, y = rnd.uniform(0, w), rnd.uniform(gy - 130 * s, h)
            d.line([(x, y), (x - 8 * s, y - 26 * s)], fill=(110, 160, 70), width=int(5 * s))
            d.line([(x, y), (x + 8 * s, y - 24 * s)], fill=(110, 160, 70), width=int(5 * s))
    else:                                              # tea_shop
        _grad(d, (0, 0, w, gy - 160 * s), (232, 236, 200), (250, 246, 222))
        d.rectangle((0, gy - 160 * s, w, h), fill=(200, 200, 196))
        for i in range(14):
            for j in range(10):
                if (i + j) % 2:
                    d.rectangle((i * w / 13, gy - 160 * s + j * 90 * s, (i + 1) * w / 13, gy - 160 * s + (j + 1) * 90 * s),
                                fill=(180, 182, 180))
        d.rounded_rectangle((w * 0.12, h * 0.07, w * 0.88, h * 0.17), radius=int(30 * s), fill=(196, 50, 46),
                            outline=(250, 210, 90), width=int(10 * s))
        try:
            f = dub._caption_font(int(78 * s * 1.6 / 1.6 * 1.0 * (w / 1440)))
            tw = dub._tlen(d, "လက်ဖက်ရည်ဆိုင်", f)
            dub._tdraw(d, ((w - tw) / 2, h * 0.085), "လက်ဖက်ရည်ဆိုင်", f, fill=(255, 240, 200))
        except Exception:  # noqa: BLE001 - the sign without its words
            pass
        sy = h * 0.24                                  # a shelf with cups and a kettle
        d.rectangle((w * 0.1, sy + 120 * s, w * 0.9, sy + 140 * s), fill=(130, 90, 60))
        for k in range(7):
            cx = w * 0.16 + k * w * 0.1
            d.rounded_rectangle((cx, sy + 50 * s, cx + 60 * sx, sy + 120 * s), radius=int(10 * s), fill=(250, 250, 250),
                                outline=(150, 150, 150), width=int(3 * s))
        for tx in (0.22, 0.72):                       # low tables + plastic stools
            x = w * tx
            d.rectangle((x - 150 * sx, gy - 250 * s, x + 150 * sx, gy - 225 * s), fill=(150, 104, 64))
            d.rectangle((x - 130 * sx, gy - 225 * s, x - 112 * sx, gy - 150 * s), fill=(120, 80, 50))
            d.rectangle((x + 112 * sx, gy - 225 * s, x + 130 * sx, gy - 150 * s), fill=(120, 80, 50))
            d.rounded_rectangle((x - 30 * sx, gy - 300 * s, x + 30 * sx, gy - 250 * s), radius=int(8 * s), fill=(250, 250, 250))
            for ox, col in ((-230, (220, 60, 60)), (200, (60, 110, 220))):
                d.rectangle((x + ox * sx, gy - 190 * s, x + (ox + 70) * sx, gy - 170 * s), fill=col)
                d.rectangle((x + (ox + 8) * sx, gy - 170 * s, x + (ox + 62) * sx, gy - 120 * s), fill=_shade(col, 0.8))
    return img.resize((W, H), Image.LANCZOS).convert("RGB")


# ------------------------------------------------------------------ characters
# drawn facing RIGHT in a 320x560 box (feet at y≈545), at 2x


class _Pen:
    def __init__(self, img, k=2):
        self.d = ImageDraw.Draw(img)
        self.k = k

    def _p(self, pts):
        return [(x * self.k, y * self.k) for x, y in pts]

    def ell(self, box, **kw):
        if "width" in kw:
            kw["width"] = int(kw["width"] * self.k)
        self.d.ellipse([v * self.k for v in box], **kw)

    def rect(self, box, r=0, **kw):
        if "width" in kw:
            kw["width"] = int(kw["width"] * self.k)
        self.d.rounded_rectangle([v * self.k for v in box], radius=int(r * self.k), **kw)

    def poly(self, pts, **kw):
        if "width" in kw:
            kw["width"] = int(kw["width"] * self.k)
        self.d.polygon(self._p(pts), **kw)

    def line(self, pts, width=4, **kw):
        self.d.line(self._p(pts), width=int(width * self.k), joint="curve", **kw)
        r = width * self.k / 2
        for x, y in self._p([pts[0], pts[-1]]):
            self.d.ellipse((x - r, y - r, x + r, y + r), fill=kw.get("fill"))

    def arc(self, box, a0, a1, width=4, **kw):
        self.d.arc([v * self.k for v in box], a0, a1, width=int(width * self.k), **kw)

    def chord(self, box, a0, a1, **kw):
        self.d.chord([v * self.k for v in box], a0, a1, **kw)

    def pie(self, box, a0, a1, **kw):
        self.d.pieslice([v * self.k for v in box], a0, a1, **kw)


INK = (40, 30, 34)
MOUTH = (110, 30, 40)
TONGUE = (236, 120, 130)


def _face(p, cx, cy, mood, talk, blink, ghosty=False, lips=None):
    """Eyes, brows, mouth and cheeks around the face centre (cx, cy); facing right (pupils +x)."""
    ex = (cx - 32, cx + 32)
    ey = cy - 2
    if ghosty:
        if mood in ("happy", "laugh") or blink:
            for x in ex:
                p.arc((x - 15, ey - 10, x + 15, ey + 12), 200, 340, width=6, fill=INK)
        else:
            big = mood in ("scared", "surprised")
            for x in ex:
                p.ell((x - 15, ey - 24 - (6 if big else 0), x + 15, ey + 20 + (4 if big else 0)), fill=INK)
                p.ell((x - 2, ey - 16, x + 8, ey - 6), fill=(255, 255, 255))
    elif blink and mood not in ("happy", "laugh"):
        for x in ex:
            p.line([(x - 13, ey + 2), (x + 13, ey + 2)], width=4, fill=INK)
    elif mood in ("happy", "laugh"):
        for x in ex:
            p.arc((x - 14, ey - 10, x + 14, ey + 14), 200, 340, width=5, fill=INK)
    else:
        rx, ry, pr = ((18, 22, 4) if mood == "scared" else (16, 19, 7) if mood == "surprised" else (13, 16, 7))
        for x in ex:
            p.ell((x - rx, ey - ry, x + rx, ey + ry), fill=(255, 255, 255), outline=INK, width=2.5)
            off = 3 if mood != "sad" else 0
            dy = 5 if mood == "sad" else 0
            p.ell((x + off - pr, ey + dy - pr, x + off + pr, ey + dy + pr), fill=INK)
            if mood == "angry":                       # lids: half shut
                p.pie((x - rx - 2, ey - ry - 2, x + rx + 2, ey + ry + 2), 180, 360,
                      fill=getattr(p, "skin", (238, 192, 150)))
    # brows
    if mood == "angry":
        p.line([(ex[0] - 16, ey - 26), (ex[0] + 12, ey - 16)], width=6, fill=INK)
        p.line([(ex[1] - 12, ey - 16), (ex[1] + 16, ey - 26)], width=6, fill=INK)
    elif mood in ("scared", "sad"):
        p.line([(ex[0] - 14, ey - 24), (ex[0] + 12, ey - 32)], width=5, fill=INK)
        p.line([(ex[1] - 12, ey - 32), (ex[1] + 14, ey - 24)], width=5, fill=INK)
    elif mood == "surprised":
        for x in ex:
            p.arc((x - 15, ey - 44, x + 15, ey - 26), 200, 340, width=5, fill=INK)
    elif not ghosty:
        for x in ex:
            p.line([(x - 11, ey - 28), (x + 11, ey - 29)], width=4, fill=INK)
    # cheeks
    if mood in ("normal", "happy", "laugh") or ghosty:
        for x in ex:
            p.ell((x - 14, ey + 18, x + 12, ey + 30), fill=(244, 150, 150) if not ghosty else (250, 190, 200))
    if mood == "angry":
        for x in ex:
            p.ell((x - 14, ey + 18, x + 12, ey + 30), fill=(230, 90, 80))
    # mouth
    my = cy + 40
    mx = cx + 4
    lip = lips or INK
    open_m = talk or mood in ("laugh", "surprised")
    if mood == "surprised":
        h = 20 if talk else 14
        p.ell((mx - 11, my - h, mx + 11, my + h), fill=MOUTH, outline=lip, width=2)
    elif mood == "laugh":
        p.chord((mx - 30, my - 22, mx + 30, my + 26 + (6 if talk else 0)), 0, 180, fill=MOUTH, outline=lip, width=2)
        p.ell((mx - 12, my + 8, mx + 12, my + 22), fill=TONGUE)
    elif open_m:
        wide = 20 if mood in ("happy", "angry") else 15
        p.ell((mx - wide, my - 10, mx + wide, my + 14), fill=MOUTH, outline=lip, width=2)
        p.ell((mx - wide * 0.55, my + 3, mx + wide * 0.55, my + 13), fill=TONGUE)
    elif mood == "happy":
        p.chord((mx - 24, my - 16, mx + 24, my + 18), 0, 180, fill=MOUTH, outline=lip, width=2)
    elif mood in ("sad", "angry"):
        p.arc((mx - 18, my - 2, mx + 18, my + 22), 200, 340, width=5, fill=lip)
    elif mood == "scared":
        pts = [(mx - 20 + i * 5, my + (4 if i % 2 else -4)) for i in range(9)]
        p.d.line(p._p(pts), fill=lip, width=int(4 * p.k))
    else:
        p.arc((mx - 18, my - 16, mx + 18, my + 8), 20, 160, width=5, fill=lip)
    # extras
    if mood == "scared" and not ghosty:
        p.ell((cx + 70, cy - 70, cx + 86, cy - 44), fill=(130, 190, 250))
        for k in range(3):
            p.line([(cx - 30 + k * 18, cy - 74), (cx - 30 + k * 18, cy - 52)], width=3, fill=(90, 110, 190))
    if mood == "sad" or mood == "laugh":
        p.ell((ex[0] - 18, ey + 14, ex[0] - 8, ey + 30), fill=(130, 190, 250))


ARMS = {  # (left hand, right hand) or elbow chains
    "normal": [((112, 250), (94, 350)), ((208, 250), (226, 350))],
    "happy": [((112, 250), (94, 350)), ((208, 250), (240, 200), (258, 150))],
    "laugh": [((112, 250), (104, 300), (140, 318)), ((208, 250), (216, 300), (180, 318))],
    "scared": [((112, 250), (90, 220), (118, 172)), ((208, 250), (230, 220), (204, 172))],
    "angry": [((112, 250), (76, 296), (110, 330)), ((208, 250), (244, 296), (210, 330))],
    "surprised": [((112, 250), (80, 200), (66, 150)), ((208, 250), (240, 200), (254, 150))],
    "sad": [((112, 250), (112, 300), (124, 350)), ((208, 250), (208, 300), (196, 350))],
}


def _human(p, kind, color, mood, talk, blink):
    skin = SKIN.get(kind, SKIN["man"])
    p.skin = skin
    hair = {"grandpa": (225, 225, 225), "grandma": (215, 215, 222)}.get(kind, (36, 26, 20))
    female = kind in ("woman", "girl", "grandma")
    shirt = (246, 246, 246) if kind in ("man", "grandpa") else _mix(color, (255, 255, 255), 0.55) if female else color
    lower = color if kind != "boy" else (60, 70, 110)
    # long hair behind the head
    if kind == "woman":
        p.ell((62, 34, 258, 214), fill=hair)
    if kind == "girl":
        p.ell((40, 100, 92, 190), fill=hair)
        p.ell((228, 100, 280, 190), fill=hair)
    # feet + lower body
    p.ell((124, 530, 156, 548), fill=(70, 46, 34))
    p.ell((164, 530, 196, 548), fill=(70, 46, 34))
    if kind == "boy":
        p.rect((118, 330, 202, 420), r=10, fill=lower)
        p.rect((126, 420, 152, 536), r=10, fill=skin)
        p.rect((168, 420, 194, 536), r=10, fill=skin)
    else:
        bottom = [(112, 330), (208, 330), (214, 536), (106, 536)] if not female else \
            [(114, 330), (206, 330), (200, 536), (120, 536)]
        p.poly(bottom, fill=lower)
        dark = _shade(lower, 0.75)
        for k in range(1, 5):                        # longyi / htamein check lines
            x = 112 + k * 19
            p.line([(x, 336), (x + (k - 2.5) * 2, 530)], width=3, fill=dark)
        for yy in (400, 470):
            p.line([(112, yy), (210, yy)], width=3, fill=dark)
        if not female:                                # the longyi knot
            p.ell((150, 322, 172, 346), fill=dark)
    # arms (behind the body for a down pose, so drawn first)
    for chain in ARMS.get(mood, ARMS["normal"]):
        p.line(list(chain), width=24, fill=shirt)
        x, y = chain[-1]
        p.ell((x - 14, y - 14, x + 14, y + 14), fill=skin)
    # torso
    p.rect((104, 224, 216, 342), r=34, fill=shirt, outline=_shade(shirt, 0.82), width=3)
    if kind in ("man", "grandpa"):
        for yy in (258, 290, 322):
            p.ell((156, yy, 166, yy + 10), fill=(180, 160, 120))
    else:
        p.chord((132, 210, 188, 252), 0, 180, fill=_shade(shirt, 0.88))
    # head
    p.rect((146, 200, 174, 232), r=6, fill=skin)
    p.ell((60, 112, 84, 150), fill=skin)
    p.ell((236, 112, 260, 150), fill=skin)
    p.ell((70, 40, 250, 220), fill=skin)
    if kind == "man":
        p.chord((64, 30, 256, 160), 180, 360, fill=hair)
        p.poly([(70, 96), (120, 70), (110, 104)], fill=hair)
    elif kind == "boy":
        for k in range(7):
            x = 78 + k * 26
            p.poly([(x, 92), (x + 13, 26 + (k % 2) * 10), (x + 26, 92)], fill=hair)
        p.chord((70, 40, 250, 150), 180, 360, fill=hair)
    elif kind == "woman":
        p.chord((66, 32, 254, 140), 180, 360, fill=hair)
        p.ell((126, 4, 194, 66), fill=hair)
        p.ell((182, 18, 206, 42), fill=(230, 60, 90))
        p.ell((188, 24, 200, 36), fill=(250, 220, 90))
    elif kind == "girl":
        p.chord((66, 32, 254, 150), 180, 360, fill=hair)
        p.ell((34, 92, 60, 118), fill=(230, 60, 90))
        p.ell((260, 92, 286, 118), fill=(230, 60, 90))
    elif kind == "grandpa":
        p.ell((62, 90, 100, 140), fill=hair)
        p.ell((220, 90, 258, 140), fill=hair)
    elif kind == "grandma":
        p.chord((66, 32, 254, 140), 180, 360, fill=hair)
        p.ell((130, 6, 190, 62), fill=hair)
    if kind in ("woman", "girl", "grandma"):           # thanaka on the cheeks
        p.ell((92, 128, 132, 168), fill=(240, 226, 190))
        p.ell((188, 128, 228, 168), fill=(240, 226, 190))
    _face(p, 164, 128, mood, talk, blink, lips=(200, 70, 80) if kind == "woman" else None)
    if kind == "grandpa":                              # a white moustache
        p.ell((136, 150, 168, 168), fill=hair)
        p.ell((162, 150, 194, 168), fill=hair)


def _ghost(p, mood, talk, blink):
    pts = []
    for i in range(0, 181, 10):
        a = math.radians(180 + i)
        pts.append((160 + math.cos(a) * 85, 150 + math.sin(a) * 100))
    pts += [(245, 300), (245, 470)]
    for i in range(0, 17):
        x = 245 - i * 10.6
        pts.append((x, 470 + 20 * math.sin(i * math.pi / 4)))
    pts += [(75, 470), (75, 300)]
    arms = {"scared": [(70, 200), (250, 200)], "surprised": [(64, 180), (256, 180)],
            "angry": [(64, 260), (256, 230)]}.get(mood, [(66, 280), (254, 280)])
    for x, y in arms:
        p.ell((x - 24, y - 20, x + 24, y + 20), fill=(240, 244, 252), outline=(170, 180, 210), width=4)
    p.poly(pts, fill=(244, 247, 255), outline=(170, 180, 210), width=4)
    _face(p, 164, 150, mood, talk, blink, ghosty=True)


def _ghost_lady(p, mood, talk, blink):
    skin = SKIN["ghost_lady"]
    p.skin = skin
    hair = (14, 12, 18)
    p.poly([(62, 90), (258, 90), (270, 380), (50, 380)], fill=hair)          # long black hair behind
    dress = [(112, 236), (208, 236), (236, 520)]
    for i in range(0, 13):
        dress.append((236 - i * 12.5, 520 + 16 * math.sin(i * math.pi / 3)))
    dress.append((86, 520))
    arm_y = 196 if mood in ("scared", "surprised") else 300
    for sx in (-1, 1):                                   # long white sleeves
        x0 = 160 + sx * 48
        p.line([(x0, 252), (x0 + sx * 36, arm_y)], width=28, fill=(236, 238, 242))
        p.ell((x0 + sx * 36 - 12, arm_y - 12, x0 + sx * 36 + 12, arm_y + 12), fill=skin)
    p.poly(dress, fill=(236, 238, 242), outline=(190, 196, 210), width=3)
    p.rect((146, 200, 174, 240), r=6, fill=skin)
    p.ell((74, 44, 246, 216), fill=skin)
    for x in (132, 196):                                  # dark rings round the eyes
        p.ell((x - 24, 100, x + 24, 152), fill=(178, 196, 192))
    p.chord((68, 34, 252, 150), 180, 360, fill=hair)
    p.poly([(68, 92), (252, 92), (240, 112), (200, 104), (160, 116), (120, 104), (80, 112)], fill=hair)
    p.poly([(62, 90), (96, 96), (90, 330), (56, 330)], fill=hair)
    p.poly([(224, 96), (258, 90), (266, 330), (232, 330)], fill=hair)
    _face(p, 164, 128, mood, talk, blink, lips=(180, 30, 50))


def _animal(p, kind, color, mood, talk, blink):
    body = color if kind == "dog" else (150, 150, 160)
    if kind == "dog" and color == (58, 123, 213):
        body = (196, 140, 80)
    dark = _shade(body, 0.7)
    if kind == "dog":
        p.line([(80, 430), (40, 380)], width=16, fill=dark)
    else:
        p.line([(80, 440), (50, 400), (60, 330)], width=12, fill=dark)
    for x in (100, 130, 190, 220):
        p.rect((x - 12, 470, x + 12, 545), r=10, fill=body)
    p.ell((70, 400, 250, 500), fill=body)
    p.ell((150, 310, 290, 440), fill=body)
    if kind == "dog":
        p.ell((140, 310, 180, 400), fill=dark)
        p.ell((260, 310, 300, 400), fill=dark)
        p.ell((228, 384, 300, 434), fill=_mix(body, (255, 255, 255), 0.45))
        p.ell((276, 380, 298, 398), fill=INK)
    else:
        p.poly([(160, 340), (174, 270), (204, 326)], fill=body)
        p.poly([(236, 326), (266, 270), (280, 340)], fill=body)
        p.ell((262, 392, 274, 402), fill=(240, 130, 150))
        for dy in (-6, 6):
            p.line([(282, 400 + dy), (318, 394 + dy * 2)], width=2, fill=INK)
    p.skin = body
    _face(p, 226, 362, mood, talk, blink)


_SPRITES = {}


def sprite(ch, mood, talk, blink, height):
    """A character as an RGBA image (feet at the bottom centre), cached."""
    key = (ch["id"], ch["kind"], ch.get("color"), mood, talk, blink, height)
    im = _SPRITES.get(key)
    if im is not None:
        return im
    big = Image.new("RGBA", (640, 1120), (0, 0, 0, 0))
    p = _Pen(big)
    color = _hex(ch.get("color") or "", None) or _hex(DEFAULT_COLORS[0])
    kind = ch["kind"]
    if kind == "ghost":
        _ghost(p, mood, talk, blink)
    elif kind == "ghost_lady":
        _ghost_lady(p, mood, talk, blink)
    elif kind in ("dog", "cat"):
        _animal(p, kind, color, mood, talk, blink)
    else:
        _human(p, kind, color, mood, talk, blink)
    box = big.getbbox() or (0, 0, 640, 1120)
    big = big.crop((0, 0, 640, 1100))                 # keep the feet line fixed at the bottom
    scale = height * KID_SCALE.get(kind, 1.0) / 1100
    im = big.resize((max(1, int(640 * scale)), max(1, int(1100 * scale))), Image.LANCZOS)
    if kind == "ghost":
        a = im.getchannel("A").point(lambda v: int(v * 0.9))
        im.putalpha(a)
    del box
    _SPRITES[key] = im
    return im


# ------------------------------------------------------------------ audio


def _voice_for(ch, seen):
    kind = ch["kind"] if ch else "narrator"
    base = {"man": (VOICE_M, 1.0, ""), "woman": (VOICE_F, 1.0, ""), "boy": (VOICE_F, 1.12, ""),
            "girl": (VOICE_F, 1.2, ""), "grandpa": (VOICE_M, 0.88, ""), "grandma": (VOICE_F, 0.9, ""),
            "ghost": (VOICE_M, 0.84, "aecho=0.8:0.7:60:0.35"), "ghost_lady": (VOICE_F, 0.92, "aecho=0.8:0.75:80:0.4"),
            "dog": (VOICE_M, 1.12, ""), "cat": (VOICE_F, 1.25, ""), "narrator": (VOICE_M, 1.0, "")}[kind]
    n = seen.get((base[0], base[1]), 0)            # a second man / woman: a little lower / higher
    if ch:
        seen[(base[0], base[1])] = n + 1
    pitch = base[1] * (1.0, 0.92, 1.08, 0.86)[n % 4]
    return base[0], pitch, base[2]


def _decode(path, pitch=1.0, fx=""):
    af = []
    if abs(pitch - 1.0) > 0.01:
        af.append(f"asetrate={int(SR * pitch)},aresample={SR},atempo={1 / pitch:.4f}")
    if fx:
        af.append(fx)
    cmd = ["ffmpeg", "-v", "error", "-i", str(path)] + (["-af", ",".join(af)] if af else []) + \
          ["-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def voice_lines(script, work, progress=None, workers=6):
    """TTS every line (kept as line_N.mp3 so a resumed job does not voice again) → arrays."""
    work = Path(work)
    chars = {c["id"]: c for c in script["characters"]}
    seen, voice_of = {}, {}
    for c in script["characters"]:
        voice_of[c["id"]] = _voice_for(c, seen)
    voice_of["narrator"] = _voice_for(None, seen)
    lines = [ln for s in script["scenes"] for ln in s["lines"]]
    done = [0]

    def one(i):
        ln = lines[i]
        v, pitch, fx = voice_of.get(ln["who"], voice_of["narrator"])
        mp3 = work / f"line_{i:03d}.mp3"
        if not mp3.exists() or mp3.stat().st_size == 0:
            dub.synthesize(ln["text"], v, mp3)
        a = _decode(mp3, pitch, fx)
        done[0] += 1
        if progress:
            progress(done[0] / len(lines))
        return a

    with ThreadPoolExecutor(workers) as ex:
        audio = list(ex.map(one, range(len(lines))))
    del chars
    return audio


def _sfx(kind, rnd):
    t = np.arange(int(SR * {"boing": 0.45, "whoosh": 0.5, "sting": 1.2, "slide": 0.55, "thud": 0.25}[kind])) / SR
    if kind == "boing":
        f = 260 + 380 * (1 - np.exp(-t * 9)) + 30 * np.sin(2 * np.pi * 14 * t)
        return 0.5 * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 6)
    if kind == "whoosh":
        n = np.array([rnd.uniform(-1, 1) for _ in range(len(t))], dtype=np.float32)
        k = 40
        n = np.convolve(n, np.ones(k) / k, mode="same")
        return 2.2 * n * np.sin(np.pi * t / t[-1]) ** 2
    if kind == "sting":
        env = np.minimum(1, t / 0.15) * np.exp(-t * 1.6)
        trem = 0.7 + 0.3 * np.sin(2 * np.pi * 7 * t)
        return 0.32 * env * trem * (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 233 * t) +
                                    0.6 * np.sin(2 * np.pi * 330 * t))
    if kind == "slide":
        f = 1300 * np.exp(-t * 3.2) + 200
        return 0.32 * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.minimum(1, (t[-1] - t) * 10)
    f = 90 * np.exp(-t * 8) + 45                     # thud
    return 0.8 * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 18)


def _bed(night, length, rnd):
    t = np.arange(int(SR * length)) / SR
    if night:                                         # wind + a low drone
        n = np.array([rnd.uniform(-1, 1) for _ in range(len(t) // 8 + 2)], dtype=np.float32)
        n = np.repeat(n, 8)[:len(t)]
        n = np.convolve(n, np.ones(60) / 60, mode="same")
        wind = n * (0.6 + 0.4 * np.sin(2 * np.pi * 0.17 * t))
        drone = 0.25 * (np.sin(2 * np.pi * 55 * t) + 0.5 * np.sin(2 * np.pi * 82.5 * t)) * \
            (0.7 + 0.3 * np.sin(2 * np.pi * 0.11 * t))
        return 0.11 * (3.5 * wind + drone)
    # a light plucky tune (pentatonic), 112 bpm
    notes = [0, 4, 7, 9, 7, 4, 2, 4, 0, 2, 4, 7, 9, 12, 9, 7]
    beat = 60 / 112 / 2
    out = np.zeros(len(t), dtype=np.float32)
    k = 0
    pos = 0.0
    while pos < length:
        n0 = int(pos * SR)
        dur = min(int(SR * 0.4), len(t) - n0)
        if dur <= 0:
            break
        tt = np.arange(dur) / SR
        f = 392 * 2 ** (notes[k % len(notes)] / 12)
        out[n0:n0 + dur] += (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt)) * np.exp(-tt * 9)
        if k % 4 == 0:
            fb = 98 * 2 ** (notes[(k // 4) % 4] / 12)
            out[n0:n0 + dur] += 0.8 * np.sin(2 * np.pi * fb * tt) * np.exp(-tt * 5)
        k += 1
        pos += beat
    return 0.05 * out


# ------------------------------------------------------------------ timeline


def build_timeline(script, audio, lead=0.5, gap=0.28, scene_tail=0.6):
    """Every line's [t0, t1] (its voice + a short pause) and every scene's [start, end]."""
    t = lead
    k = 0
    scenes = []
    for s in script["scenes"]:
        s0 = t
        lines = []
        for ln in s["lines"]:
            a = audio[k]
            dur = max(1.1, len(a) / SR + gap)
            if ln["action"] in ("run_away", "fall", "vanish", "appear"):
                dur = max(dur, 1.6)
            lines.append({**ln, "t0": t, "t1": t + dur, "k": k})
            t += dur
            k += 1
        t += scene_tail
        scenes.append({**s, "t0": s0, "t1": t, "lines": lines})
    want = float(script.get("min_total") or 0)
    if want and t + 0.4 < want - 0.05 and scenes:     # ⏱️ at least a minute: each scene held a bit longer
        return build_timeline({**script, "min_total": 0}, audio, lead, gap,
                              scene_tail + (want - t - 0.4) / len(scenes))
    return scenes, t + 0.4


def _envelopes(audio):
    env = []
    hop = SR // FPS
    for a in audio:
        n = max(1, len(a) // hop)
        r = np.sqrt(np.mean(a[:n * hop].reshape(n, hop) ** 2, axis=1)) if len(a) >= hop else np.zeros(1)
        thr = max(0.02, float(np.percentile(r, 60)) * 0.5) if len(r) else 0.02
        env.append(r > thr)
    return env


def mix_audio(scenes, audio, total, seed=7):
    rnd = random.Random(seed)
    out = np.zeros(int(SR * total) + SR, dtype=np.float32)

    def put(a, t, gain=1.0):
        n0 = int(t * SR)
        n1 = min(len(out), n0 + len(a))
        if n1 > n0:
            out[n0:n1] += gain * a[:n1 - n0]

    for s in scenes:
        bed = _bed(s["bg"] in NIGHT_BGS, s["t1"] - s["t0"], rnd).astype(np.float32)
        fade = min(len(bed) // 2, int(SR * 0.4))
        if fade:
            bed[:fade] *= np.linspace(0, 1, fade)
            bed[-fade:] *= np.linspace(1, 0, fade)
        put(bed, s["t0"])
        for ln in s["lines"]:
            put(audio[ln["k"]], ln["t0"] + 0.12)
            act, t0, d = ln["action"], ln["t0"], ln["t1"] - ln["t0"]
            if act == "jump":
                put(_sfx("boing", rnd), t0)
            elif act == "appear":
                put(_sfx("whoosh", rnd), t0, 0.8)
                put(_sfx("sting", rnd), t0 + 0.05, 0.9)
            elif act == "vanish":
                put(_sfx("whoosh", rnd), t0 + d - 0.7, 0.8)
            elif act == "run_away":
                put(_sfx("whoosh", rnd), t0 + d * 0.45, 1.0)
            elif act == "fall":
                put(_sfx("slide", rnd), t0)
                put(_sfx("thud", rnd), t0 + 0.32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out * min(1.0, 0.92 / peak)).astype(np.float32)


# ------------------------------------------------------------------ captions + overlays


def _caption_img(ln, chars, W, H):
    font = dub._caption_font(int(min(W, H) * 0.052))
    small = dub._caption_font(int(min(W, H) * 0.036))
    tmp = Image.new("RGBA", (W, 10))
    d = ImageDraw.Draw(tmp)
    rows = dub._wrap(d, ln["text"], font, int(W * 0.84))[:3]
    lh = int(font.my.size * 1.55)
    ch = chars.get(ln["who"])
    name_h = int(small.my.size * 1.6) if ch else 0
    h = name_h + lh * len(rows) + int(lh * 0.5)
    img = Image.new("RGBA", (W, h + 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bw = max(dub._tlen(d, r, font) for r in rows) + W * 0.08
    if ch:
        bw = max(bw, dub._tlen(d, ch["name"], small) + W * 0.1)
    x0 = (W - bw) / 2
    d.rounded_rectangle((x0, 0, x0 + bw, h), radius=int(lh * 0.4), fill=(10, 10, 20, 170))
    y = int(lh * 0.22)
    if ch:
        col = _mix(_hex(ch.get("color") or "", None) or _hex(DEFAULT_COLORS[0]), (255, 255, 255), 0.35)
        if ch["kind"] in FLOATERS:
            col = (190, 230, 255)
        nw = dub._tlen(d, ch["name"], small)
        dub._tdraw(d, ((W - nw) / 2, y), ch["name"], small, fill=col)
        y += name_h
    fill = (255, 236, 150) if not ch else (255, 255, 255)
    for r in rows:
        tw = dub._tlen(d, r, font)
        dub._tdraw(d, ((W - tw) / 2, y), r, font, fill=fill, stroke_width=2, stroke_fill=(0, 0, 0))
        y += lh
    return img


def _title_img(title, W, H):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    title = title[:40]
    size = int(min(W, H) * 0.042)
    while True:                                   # never under the logo (top right)
        font = dub._caption_font(size)
        tw = dub._tlen(tmp, title, font)
        if tw + font.my.size * 2.3 <= W * 0.78 or size <= 14:
            break
        size -= 2
    ph = int(font.my.size * 1.9)
    img = Image.new("RGBA", (int(tw + ph * 1.2), ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, img.width - 1, ph - 1), radius=ph // 2, fill=(255, 196, 60, 235))
    dub._tdraw(d, ((img.width - tw) / 2, int(ph * 0.2)), title, font, fill=(50, 24, 10))
    return img


def _logo_img(logo, H):
    try:
        im = Image.open(logo).convert("RGBA")
        s = int(H * 0.07)
        return im.resize((s, int(s * im.height / im.width)), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ frames


def _slots(n, W):
    return {1: [0.5], 2: [0.3, 0.7], 3: [0.2, 0.5, 0.8]}.get(n, [0.5])[:n]


def _plan(scene):
    """Who is on stage / gone / their mood, per line of one scene."""
    cast = scene["cast"]
    vis = {c: True for c in cast}
    for c in cast:
        first = next((ln for ln in scene["lines"] if ln["who"] == c), None)
        if first and first["action"] == "appear":
            vis[c] = False
    mood = {c: "normal" for c in cast}
    plan = []
    for ln in scene["lines"]:
        st = {}
        for c in cast:
            if ln["who"] == c:
                m = ln["mood"]
            elif c in ln["react"]:
                m = ln["react"][c]
            else:
                m = mood[c] if mood[c] in ("scared", "angry", "sad") else "normal"
            act = ln["action"] if ln["who"] == c else None
            if ln["who"] == c and not vis[c] and act != "appear":
                act = "enter"                        # gone (ran off) but talks again: walks back in
                vis[c] = True
            st[c] = {"vis": vis[c], "act": act, "mood": m}
            mood[c] = m
        plan.append(st)
        if ln["who"] in vis:
            if ln["action"] in ("run_away", "vanish"):
                vis[ln["who"]] = False
            elif ln["action"] == "appear":
                vis[ln["who"]] = True
    return plan


class Renderer:
    def __init__(self, script, scenes, audio, W, H, logo=None):
        self.W, self.H = W, H
        self.chars = {c["id"]: c for c in script["characters"]}
        for i, c in enumerate(script["characters"]):
            if not c.get("color"):
                c["color"] = DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
        self.scenes = scenes
        self.env = _envelopes(audio)
        self.ch_h = int(H * 0.41)
        self.gy = int(H * 0.74)
        self.title = _title_img(script["title"], W, H)
        self.logo = _logo_img(logo, H) if logo else None
        self.bgs = {}
        self.caps = {}
        self.plans = [_plan(s) for s in scenes]
        self.phase = {c: random.Random(c).uniform(0, 3) for c in self.chars}
        self._rot = {}

    def bg(self, si):
        if si not in self.bgs:
            s = self.scenes[si]
            img = draw_bg(s["bg"], self.W, self.H, seed=si + 3)
            img.paste(self.title, (int(self.W * 0.04), int(self.H * 0.035)), self.title)
            if self.logo:
                img.paste(self.logo, (self.W - self.logo.width - int(self.W * 0.04), int(self.H * 0.03)), self.logo)
            self.bgs = {k: v for k, v in self.bgs.items() if k >= si - 1}
            self.bgs[si] = img
        return self.bgs[si]

    def caption(self, k, ln):
        if k not in self.caps:
            self.caps = {kk: v for kk, v in self.caps.items() if kk >= k - 2}
            self.caps[k] = _caption_img(ln, self.chars, self.W, self.H)
        return self.caps[k]

    def frame(self, t, captions=True):
        W, H = self.W, self.H
        si = next((i for i, s in enumerate(self.scenes) if t < s["t1"]), len(self.scenes) - 1)
        s = self.scenes[si]
        img = self.bg(si).copy()
        li = next((i for i, ln in enumerate(s["lines"]) if t < ln["t1"]), None)
        ln = s["lines"][li] if li is not None else None
        plan = self.plans[si][li] if li is not None else (
            {c: {**v, "act": None} for c, v in self.plans[si][-1].items()} if self.plans[si] else {})
        if li is None and s["lines"]:                    # after the last line: what was left at its end
            last = s["lines"][-1]
            for c, v in plan.items():
                if last["who"] == c and last["action"] in ("run_away", "vanish"):
                    v["vis"] = False
                elif last["who"] == c and last["action"] == "appear":
                    v["vis"] = True
        cast = s["cast"]
        xs = _slots(len(cast), W)
        shadow = ImageDraw.Draw(img, "RGBA")
        for ci, c in enumerate(cast):
            ch = self.chars[c]
            st = plan.get(c, {"vis": True, "act": None, "mood": "normal"})
            act = st["act"]
            u = t - ln["t0"] if ln else 0
            D = (ln["t1"] - ln["t0"]) if ln else 1
            if not st["vis"] and act != "appear":
                continue
            talking = bool(ln and ln["who"] == c and 0.12 <= u)
            mouth = False
            if talking:
                e = self.env[ln["k"]]
                fi = int((u - 0.12) * FPS)
                mouth = bool(fi < len(e) and e[fi])
            blink = ((t + self.phase[c]) % 3.7) < 0.13
            spr = sprite(ch, st["mood"], mouth, blink, self.ch_h)
            x = xs[ci] * W
            face_right = xs[ci] < 0.5 or (xs[ci] == 0.5 and ln is not None and ln["who"] in cast
                                          and cast.index(ln["who"]) > ci)
            if xs[ci] == 0.5 and ln is not None and ln["who"] in cast and cast.index(ln["who"]) < ci:
                face_right = False
            dy, dx, alpha, angle = 0.0, 0.0, 1.0, 0.0
            if ch["kind"] in FLOATERS:
                dy -= H * 0.035 + math.sin(t * 2.2 + self.phase[c]) * H * 0.012
            if talking:
                dy -= abs(math.sin(u * 8)) * H * 0.006
            if st["mood"] == "scared":
                dx += math.sin(t * 55) * W * 0.004
            if act == "jump":
                dy -= math.sin(math.pi * _clamp(u / 0.55)) * H * 0.08
            elif act == "shake":
                dx += math.sin(u * 60) * W * 0.009
            elif act == "appear":
                alpha = _clamp(u / 0.45)
            elif act == "enter":                          # slides back in from its side
                k = 1 - _clamp(u / 0.45)
                dx += (-1 if xs[ci] < 0.5 else 1) * k * k * W * 0.5
            elif act == "vanish":
                alpha = 1 - _clamp((u - (D - 0.75)) / 0.7)
            elif act == "run_away":
                k = max(0.0, u - D * 0.45)
                dx += (-1 if xs[ci] < 0.5 else 1) * (k * k * W * 2.6 + k * W * 0.6)
                face_right = xs[ci] >= 0.5
                if k > 0:
                    dy -= abs(math.sin(k * 18)) * H * 0.012
            elif act == "fall":
                angle = -85 * _clamp(u / 0.3) if face_right else 85 * _clamp(u / 0.3)
            im = spr if face_right else spr.transpose(Image.FLIP_LEFT_RIGHT)
            if angle:
                key = (id(im), int(angle / 5))
                r = self._rot.get(key)
                if r is None:
                    r = im.rotate(int(angle / 5) * 5, resample=Image.BICUBIC, expand=True)
                    r = r.crop(r.getbbox() or (0, 0, r.width, r.height))   # lying ON the ground
                    if len(self._rot) > 200:
                        self._rot.clear()
                    self._rot[key] = r
                im = r
            if alpha < 0.999:
                im = im.copy()
                im.putalpha(im.getchannel("A").point(lambda v, a=alpha: int(v * a)))
            px = int(x + dx - im.width / 2)
            if angle:                                     # lying down: kept on screen
                px = max(int(W * 0.02), min(px, int(W * 0.98) - im.width))
            py = int(self.gy + dy - im.height + (H * 0.004))
            if alpha > 0.05 and ch["kind"] not in FLOATERS:
                sw = im.width * 0.38
                shadow.ellipse((x + dx - sw, self.gy - H * 0.008, x + dx + sw, self.gy + H * 0.012),
                               fill=(0, 0, 0, int(60 * alpha)))
            if -im.width < px < W:
                img.paste(im, (px, py), im)
        if captions and ln is not None and t >= ln["t0"]:
            cap = self.caption(ln["k"], ln)
            img.paste(cap, (0, int(H * 0.80)), cap)
        # fade in / out between scenes
        f = min(_clamp((t - s["t0"]) / 0.35), _clamp((s["t1"] - t) / 0.3))
        if f < 1:
            img = Image.blend(Image.new("RGB", img.size, (0, 0, 0)), img, f)
        return img


def render_cartoon(script, work, out, size=(720, 1280), logo=None, captions=True, audio=None,
                   progress=None, log=None):
    """The whole cartoon → out (mp4). Returns (duration, cover_png)."""
    work = Path(work)
    W, H = size
    if audio is None:
        audio = voice_lines(script, work)
    scenes, total = build_timeline(script, audio)
    mix = mix_audio(scenes, audio, total)
    raw = work / "cartoon_audio.f32"
    mix.tofile(raw)
    r = Renderer(script, scenes, audio, W, H, logo)
    n = int(total * FPS)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(raw),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n):
            p.stdin.write(r.frame(i / FPS, captions).tobytes())
            if progress and i % FPS == 0:
                progress(i / n)
        p.stdin.close()
    except BrokenPipeError:
        pass
    err = p.stderr.read().decode(errors="ignore")
    if p.wait() != 0:
        raise RuntimeError("cartoon encode failed: " + err[-300:])
    raw.unlink(missing_ok=True)
    # 🖼️ the cover moment: a ghost popping up / a jump, else the middle; no captions on it
    best = None
    for s in scenes:
        for ln in s["lines"]:
            if ln["action"] in ("appear", "jump", "shake") and best is None:
                best = ln["t0"] + (0.6 if ln["action"] == "appear" else 0.25)
    cover = work / "cartoon_cover.png"
    cr = Renderer(script, scenes, audio, 1080, 1920, None)
    cr.title = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    cr.frame(best if best is not None else total * 0.4, captions=False).save(cover)
    if log:
        log(f"🎨 cartoon drawn: {len(scenes)} scenes, {sum(len(s['lines']) for s in scenes)} lines, {total:.0f}s")
    return total, cover
