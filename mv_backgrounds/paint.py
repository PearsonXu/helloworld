"""程序化绘制歌曲 MV 背景图（清新风景风格）。

用法: python3 paint.py            # 输出 5 张竖屏 1080x1920
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.dirname(os.path.abspath(__file__))


def rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255


class Canvas:
    def __init__(self, W, H):
        self.W, self.H = W, H
        self.s = min(W, H)
        self.a = np.zeros((H, W, 3), np.float32)
        self.Y = np.arange(H, dtype=np.float32)[:, None]
        self.X = np.arange(W, dtype=np.float32)[None, :]

    # 竖直渐变，stops = [(pos 0..1, "#hex"), ...]
    def gradient(self, stops):
        t = (self.Y / (self.H - 1)).repeat(self.W, 1)
        ps = [p for p, _ in stops]
        for c in range(3):
            self.a[..., c] = np.interp(t, ps, [rgb(h)[c] for _, h in stops])

    def blend(self, alpha, color):
        alpha = np.clip(alpha, 0, 1)[..., None]
        color = np.asarray(color, np.float32)
        self.a = self.a * (1 - alpha) + color * alpha

    def shape(self, draw_fn, color, alpha=1.0, blur=0):
        m = Image.new("L", (self.W, self.H), 0)
        draw_fn(ImageDraw.Draw(m))
        if blur:
            m = m.filter(ImageFilter.GaussianBlur(blur))
        self.blend(np.asarray(m, np.float32) / 255 * alpha, rgb(color) if isinstance(color, str) else color)

    def glow(self, cx, cy, r, color, strength=1.0):
        d2 = ((self.X - cx) ** 2 + (self.Y - cy) ** 2) / (r * r)
        g = np.exp(-d2)[..., None] * strength
        c = rgb(color)
        self.a = 1 - (1 - self.a) * (1 - np.clip(c * g, 0, 1))  # screen

    def mountain(self, ridge, top, bottom, depth, alpha=1.0):
        t = np.clip((self.Y - ridge[None, :]) / depth, 0, 1)[..., None]
        col = rgb(top) * (1 - t) + rgb(bottom) * t
        a = np.clip(self.Y - ridge[None, :] + 0.5, 0, 1)[..., None] * alpha
        self.a = self.a * (1 - a) + col * a

    def reflect(self, horizon, tint, strength=0.55, ripple=0.004, fade=0.5):
        H, W = self.H, self.W
        hy = int(horizon)
        src = self.a.copy()
        for y in range(hy, H):
            dy = y - hy
            sy = hy - 1 - int(dy * 1.0)
            if sy < 0:
                sy = 0
            shift = int(math.sin(dy * 0.35) * ripple * self.s * (0.3 + dy / (H - hy)))
            row = np.roll(src[sy], shift, axis=0)
            k = strength * (1 - fade * dy / max(1, H - hy))
            self.a[y] = row * k + rgb(tint) * (1 - k)

    def soften(self, r):
        im = self.to_image().filter(ImageFilter.GaussianBlur(r))
        self.a = np.asarray(im, np.float32) / 255

    def finish(self, seed, warm=0.0, vignette=0.18, grain=0.012):
        rng = np.random.default_rng(seed)
        cx, cy = self.W / 2, self.H / 2
        d = np.sqrt(((self.X - cx) / cx) ** 2 + ((self.Y - cy) / cy) ** 2) / math.sqrt(2)
        self.a *= (1 - vignette * d ** 2)[..., None]
        if warm:
            self.a = self.a * (1 - warm) + self.a * np.array([1.06, 1.0, 0.92]) * warm
        # 水彩纸纹理
        n = rng.normal(0, 1, (self.H // 3 + 1, self.W // 3 + 1)).astype(np.float32)
        n = np.asarray(Image.fromarray(((n * 40) + 128).clip(0, 255).astype(np.uint8)).resize(
            (self.W, self.H), Image.BICUBIC), np.float32) / 255 - 0.5
        fine = rng.normal(0, 1, (self.H, self.W)).astype(np.float32)
        self.a += (n * 2 * grain + fine * grain * 0.5)[..., None]

    def to_image(self):
        return Image.fromarray((np.clip(self.a, 0, 1) * 255 + 0.5).astype(np.uint8))


def ridge(W, rng, base, amp, octaves=6, start=3):
    x = np.linspace(0, 1, W)
    y = np.zeros(W)
    a = 1.0
    for o in range(octaves):
        n = start * 2 ** o + 1
        knots = rng.uniform(-1, 1, n)
        xs = np.linspace(0, 1, n)
        # 余弦插值让山形更圆润
        idx = np.minimum((x * (n - 1)).astype(int), n - 2)
        f = x * (n - 1) - idx
        f = (1 - np.cos(f * math.pi)) / 2
        y += (knots[idx] * (1 - f) + knots[idx + 1] * f) * a
        a *= 0.5
    return base - y / 1.9 * amp


def ellipse_pts(cx, cy, rx, ry, rot, n=40):
    out = []
    cr, sr = math.cos(rot), math.sin(rot)
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        out.append((cx + x * cr - y * sr, cy + x * sr + y * cr))
    return out


def leaf_pts(cx, cy, size, rot):
    # 枫叶状五裂叶
    pts = []
    for i in range(60):
        t = 2 * math.pi * i / 60
        r = size * (0.3 + 0.7 * abs(math.cos(2.5 * t)) ** 2.2)
        x, y = r * math.cos(t), r * math.sin(t)
        pts.append((cx + x * math.cos(rot) - y * math.sin(rot), cy + x * math.sin(rot) + y * math.cos(rot)))
    return pts


def butterfly(c, cx, cy, size, rot, color, accent):
    def wings(d):
        for side in (-1, 1):
            r = rot + side * 0.0
            # 上翅
            ox = side * size * 0.55
            d.polygon(ellipse_pts(cx + ox * math.cos(r), cy - size * 0.35 + ox * math.sin(r),
                                  size * 0.6, size * 0.45, r + side * -0.5), fill=255)
            ox2 = side * size * 0.4
            d.polygon(ellipse_pts(cx + ox2 * math.cos(r), cy + size * 0.35 + ox2 * math.sin(r),
                                  size * 0.38, size * 0.32, r + side * 0.6), fill=255)

    c.glow(cx, cy, size * 2.2, color, 0.35)
    c.shape(wings, color, 0.95, blur=size * 0.06)
    c.shape(lambda d: [d.polygon(ellipse_pts(cx + side * size * 0.6, cy - size * 0.38, size * 0.22, size * 0.16, 0),
                                 fill=255) for side in (-1, 1)], accent, 0.55, blur=size * 0.08)
    c.shape(lambda d: d.polygon(ellipse_pts(cx, cy, size * 0.08, size * 0.55, rot), fill=255), "#4a3b3a", 0.85)
    c.shape(lambda d: [d.line([(cx, cy - size * 0.5), (cx + s * size * 0.3, cy - size * 0.95)],
                              fill=255, width=max(1, int(size * 0.04))) for s in (-1, 1)], "#4a3b3a", 0.7)


# ---------------------------------------------------------------- 场景
def scene_window(W, H, rng):
    """巷口灯光忽明忽灭 / 手中甜咖啡已冷却 / 在发呆的窗前凝结"""
    c = Canvas(W, H)
    s = c.s
    portrait = H > W
    wx0, wx1 = (0.12 * W, 0.88 * W) if portrait else (0.22 * W, 0.78 * W)
    wy0, wy1 = (0.14 * H, 0.70 * H) if portrait else (0.08 * H, 0.76 * H)
    ww, wh = wx1 - wx0, wy1 - wy0

    # 窗外：黄昏小巷
    c.gradient([(0, "#3b4c82"), (wy0 / H + 0.25 * wh / H, "#8a8fc4"), (wy0 / H + 0.55 * wh / H, "#f3c2ab"), (1, "#f7dcc4")])
    c.glow(wx0 + ww * 0.5, wy0 + wh * 0.62, s * 0.5, "#ffd7b0", 0.35)
    vx, vy = wx0 + ww * 0.52, wy0 + wh * 0.66  # 巷子消失点
    ground = wy1 + 0.02 * H
    # 远处屋檐剪影
    rr = ridge(W, rng, vy - wh * 0.06, wh * 0.06, octaves=3, start=8)
    c.mountain(np.round(rr / (wh * 0.03)) * (wh * 0.03), "#9a8fb4", "#c7aebb", wh * 0.2)
    # 两侧墙
    for side in (-1, 1):
        xe = wx0 - ww * 0.05 if side < 0 else wx1 + ww * 0.05
        poly = [(xe, wy0 + wh * 0.18), (vx + side * ww * 0.07, vy - wh * 0.16), (vx + side * ww * 0.07, vy + wh * 0.02), (xe, ground)]
        c.shape(lambda d, p=poly: d.polygon(p, fill=255), "#6e6a96" if side < 0 else "#7d6f97", 1.0)
        # 屋檐
        eave = [(xe, wy0 + wh * 0.18), (vx + side * ww * 0.07, vy - wh * 0.16), (vx + side * ww * 0.09, vy - wh * 0.18), (xe - side * ww * 0.0, wy0 + wh * 0.13)]
        c.shape(lambda d, p=eave: d.polygon(p, fill=255), "#45436e", 1.0)
        # 暖黄小窗
        for k in range(4):
            f = 0.15 + k * 0.2
            x = xe + (vx - xe) * f
            top = (wy0 + wh * 0.18) + ((vy - wh * 0.16) - (wy0 + wh * 0.18)) * f
            bot = ground + ((vy + wh * 0.02) - ground) * f
            hgt = (bot - top)
            sz = ww * 0.05 * (1 - f * 0.8)
            y = top + hgt * 0.32
            if rng.random() < 0.8:
                c.shape(lambda d, x=x, y=y, sz=sz: d.rectangle([x - sz * 0.5, y, x + sz * 0.5, y + sz * 1.2], fill=255), "#ffd48a", 0.9)
                c.glow(x, y + sz * 0.6, sz * 1.6, "#ffb86b", 0.35)
    # 石板路
    road = [(wx0 - ww * 0.05, ground), (vx - ww * 0.07, vy + wh * 0.02), (vx + ww * 0.07, vy + wh * 0.02), (wx1 + ww * 0.05, ground)]
    c.shape(lambda d: d.polygon(road, fill=255), "#b79fa8", 1.0)
    c.shape(lambda d: d.polygon([(vx - ww * 0.02, vy + wh * 0.02), (vx + ww * 0.02, vy + wh * 0.02), (wx0 + ww * 0.62, ground), (wx0 + ww * 0.42, ground)], fill=255), "#f6cdb2", 0.35, blur=s * 0.01)
    # 巷口路灯（忽明忽灭：主光 + 外圈闪烁光晕）
    lx, ly = wx0 + ww * 0.72, wy0 + wh * 0.40
    c.shape(lambda d: d.rectangle([lx - s * 0.004, ly, lx + s * 0.004, ground - wh * 0.05], fill=255), "#3a3658", 1.0)
    c.shape(lambda d: d.line([(lx, ly), (lx - ww * 0.05, ly - wh * 0.01)], fill=255, width=max(2, int(s * 0.006))), "#3a3658", 1.0)
    hx = lx - ww * 0.05
    c.shape(lambda d: d.polygon([(hx - s * 0.018, ly + s * 0.01), (hx + s * 0.018, ly + s * 0.01), (hx + s * 0.01, ly - s * 0.012), (hx - s * 0.01, ly - s * 0.012)], fill=255), "#3a3658", 1.0)
    c.glow(hx, ly + s * 0.015, s * 0.035, "#fffbe6", 1.0)
    c.glow(hx, ly + s * 0.02, s * 0.12, "#ffd08a", 0.9)
    c.glow(hx, ly + s * 0.03, s * 0.3, "#ffb07a", 0.35)
    c.glow(hx, ground - wh * 0.06, s * 0.14, "#ffd9a0", 0.35)
    c.shape(lambda d: d.polygon([(hx - s * 0.012, ly + s * 0.012), (hx + s * 0.012, ly + s * 0.012), (hx + s * 0.11, ground), (hx - s * 0.11, ground)], fill=255), "#ffe2b0", 0.3, blur=s * 0.02)
    c.soften(s * 0.0025)

    # 玻璃起雾 & 水珠
    fog = np.clip((c.Y - (wy0 + wh * 0.55)) / (wh * 0.45), 0, 1) * 0.28
    c.blend(np.broadcast_to(fog, (H, W)), rgb("#eef0f6"))
    def drops(d):
        for _ in range(int(260 * (W * H) / (1080 * 1920))):
            x = rng.uniform(wx0, wx1)
            y = rng.uniform(wy0 + wh * 0.35, wy1)
            r = rng.uniform(0.6, 2.4) * s / 1080
            d.ellipse([x - r, y - r, x + r, y + r * 1.3], fill=255)
    c.shape(drops, "#ffffff", 0.35)

    # 室内墙面（窗外以外区域）
    inner = np.ones((H, W), np.float32)
    inner[int(wy0):int(wy1), int(wx0):int(wx1)] = 0
    wall = np.zeros((H, W, 3), np.float32)
    t = (c.Y / H)[..., None]
    wall[:] = rgb("#e9dfd3") * (1 - t) + rgb("#c9b9a8") * t
    c.a = c.a * (1 - inner[..., None]) + wall * inner[..., None]
    c.glow(wx0 + ww * 0.5, wy0 + wh * 0.5, s * 0.7, "#ffe2c4", 0.2)
    # 窗框
    fw = s * 0.022
    frame = "#f7f2ea"
    c.shape(lambda d: d.rectangle([wx0 - fw * 3, wy0 - fw * 3, wx1 + fw * 3, wy1 + fw * 3], fill=255), "#000000", 0.18, blur=s * 0.02)
    def frame_draw(d):
        d.rectangle([wx0 - fw, wy0 - fw, wx1 + fw, wy0], fill=255)
        d.rectangle([wx0 - fw, wy1, wx1 + fw, wy1 + fw], fill=255)
        d.rectangle([wx0 - fw, wy0, wx0, wy1], fill=255)
        d.rectangle([wx1, wy0, wx1 + fw, wy1], fill=255)
        mx = (wx0 + wx1) / 2
        d.rectangle([mx - fw * 0.45, wy0, mx + fw * 0.45, wy1], fill=255)
        my = wy0 + wh * 0.38
        d.rectangle([wx0, my - fw * 0.35, wx1, my + fw * 0.35], fill=255)
    # 将先把整个窗洞也挡一下：先画内墙后再画窗框
    c.shape(frame_draw, frame, 1.0)
    # 窗台
    sy0, sy1 = wy1 + fw, wy1 + fw + s * 0.05
    c.shape(lambda d: d.rectangle([wx0 - fw * 3, sy0, wx1 + fw * 3, sy1], fill=255), "#d8c3a8", 1.0)
    c.shape(lambda d: d.rectangle([wx0 - fw * 3, sy0, wx1 + fw * 3, sy0 + s * 0.008], fill=255), "#efe3d1", 1.0)
    c.shape(lambda d: d.rectangle([wx0 - fw * 3, sy1, wx1 + fw * 3, sy1 + s * 0.03], fill=255), "#000000", 0.12, blur=s * 0.015)
    # 冷掉的咖啡杯（无热气）
    mx, mw, mh = wx0 + ww * 0.3, s * 0.11, s * 0.12
    base = sy0 + s * 0.012
    c.shape(lambda d: d.ellipse([mx - mw * 0.7, base - s * 0.012, mx + mw * 0.7, base + s * 0.014], fill=255), "#000000", 0.18, blur=s * 0.008)
    c.shape(lambda d: d.ellipse([mx + mw * 0.32, base - mh * 0.78, mx + mw * 0.78, base - mh * 0.3], outline=255, width=int(s * 0.014)), "#f4ede4", 1.0)
    c.shape(lambda d: d.rounded_rectangle([mx - mw / 2, base - mh, mx + mw / 2, base], radius=int(s * 0.02), fill=255), "#f4ede4", 1.0)
    c.shape(lambda d: d.rounded_rectangle([mx - mw / 2, base - mh, mx - mw / 2 + mw * 0.25, base], radius=int(s * 0.02), fill=255), "#d9cfc4", 0.6, blur=s * 0.006)
    c.shape(lambda d: d.ellipse([mx - mw / 2, base - mh - s * 0.012, mx + mw / 2, base - mh + s * 0.012], fill=255), "#e9e0d6", 1.0)
    c.shape(lambda d: d.ellipse([mx - mw / 2 + s * 0.006, base - mh - s * 0.008, mx + mw / 2 - s * 0.006, base - mh + s * 0.010], fill=255), "#7a5440", 1.0)
    # 小绿植
    px = wx0 + ww * 0.78
    c.shape(lambda d: d.polygon([(px - s * 0.035, base - s * 0.07), (px + s * 0.035, base - s * 0.07), (px + s * 0.026, base), (px - s * 0.026, base)], fill=255), "#d99a7c", 1.0)
    def leaves(d):
        for k in range(9):
            a = -math.pi / 2 + (k - 4) * 0.28
            L = s * rng.uniform(0.06, 0.1)
            d.polygon(ellipse_pts(px + math.cos(a) * L * 0.5, base - s * 0.07 + math.sin(a) * L * 0.5, L * 0.5, L * 0.16, a), fill=255)
    c.shape(leaves, "#7fae7c", 1.0)
    c.finish(1, warm=0.3, vignette=0.22)
    return c


def scene_spring(W, H, rng):
    """你看啊春日的蝴蝶 / 和风与暖阳倾斜"""
    c = Canvas(W, H)
    s = c.s
    hz = 0.58 * H
    c.gradient([(0, "#9fd3ef"), (0.35, "#cfe9f3"), (hz / H, "#fff2dc"), (1, "#fff2dc")])
    sx, sy = 0.12 * W, 0.1 * H
    c.glow(sx, sy, s * 0.25, "#fff6d8", 0.9)
    c.glow(sx, sy, s * 0.7, "#ffe6b8", 0.4)
    # 倾斜的暖阳光束
    def rays(d):
        for k in range(6):
            a = 0.35 + k * 0.13 + rng.uniform(-0.03, 0.03)
            w = rng.uniform(0.02, 0.05)
            L = 2.5 * max(W, H)
            d.polygon([(sx, sy), (sx + L * math.cos(a - w), sy + L * math.sin(a - w)), (sx + L * math.cos(a + w), sy + L * math.sin(a + w))], fill=255)
    c.shape(rays, "#fff4d6", 0.22, blur=s * 0.03)
    # 云
    def clouds(d):
        for _ in range(5):
            cx, cy = rng.uniform(0.3, 1.0) * W, rng.uniform(0.08, 0.3) * H
            for _ in range(7):
                r = s * rng.uniform(0.03, 0.07)
                ox, oy = rng.uniform(-3, 3) * r, rng.uniform(-0.4, 0.3) * r
                d.ellipse([cx + ox - r * 1.5, cy + oy - r, cx + ox + r * 1.5, cy + oy + r], fill=255)
    c.shape(clouds, "#ffffff", 0.75, blur=s * 0.012)
    c.mountain(ridge(W, rng, hz - 0.05 * H, 0.12 * H), "#a9c6d8", "#d8e6e3", 0.15 * H)
    c.mountain(ridge(W, rng, hz + 0.02 * H, 0.09 * H, start=2), "#9cc9a4", "#cfe5b8", 0.2 * H)
    c.mountain(ridge(W, rng, hz + 0.12 * H, 0.08 * H, start=2), "#a8d48a", "#d6eb9c", 0.4 * H)
    # 樱花树（右侧）
    tx, tb = 0.82 * W, hz + 0.2 * H
    c.shape(lambda d: d.polygon([(tx - s * 0.02, tb), (tx + s * 0.02, tb), (tx + s * 0.008, tb - 0.2 * H), (tx - s * 0.012, tb - 0.2 * H)], fill=255), "#7b5e57", 1.0)
    c.shape(lambda d: [d.line([(tx, tb - 0.15 * H), (tx + a * s * 0.15, tb - 0.25 * H)], fill=255, width=int(s * 0.012)) for a in (-1, 0.6)], "#7b5e57", 1.0)
    for col, al in (("#f6b7c8", 1.0), ("#fbd3df", 0.9), ("#fff0f4", 0.7)):
        def canopy(d):
            for _ in range(28):
                r = s * rng.uniform(0.03, 0.07)
                cx = tx + rng.normal(0, s * 0.12)
                cy = tb - 0.25 * H + rng.normal(0, s * 0.07)
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
        c.shape(canopy, col, al, blur=s * 0.004)
    # 花海
    for col in ("#ffffff", "#ffd6e3", "#fff3a6", "#c9b6ef"):
        def flowers(d):
            for _ in range(int(500 * W / 1080)):
                y = rng.uniform(hz + 0.12 * H, H)
                f = (y - hz) / (H - hz)
                r = s * 0.004 * (0.5 + 3 * f ** 2) * rng.uniform(0.7, 1.3)
                x = rng.uniform(0, W)
                d.ellipse([x - r, y - r * 0.8, x + r, y + r * 0.8], fill=255)
        c.shape(flowers, col, 0.9)
    # 飘落花瓣
    def petals(d):
        for _ in range(int(60 * W / 1080)):
            x, y = rng.uniform(0, W), rng.uniform(0, H)
            r = s * rng.uniform(0.004, 0.009)
            d.polygon(ellipse_pts(x, y, r * 1.4, r * 0.8, rng.uniform(0, math.pi)), fill=255)
    c.shape(petals, "#f8c4d4", 0.85)
    # 颤抖着飞越的蝴蝶 + 飞行轨迹
    path = [(0.2, 0.62), (0.38, 0.5), (0.55, 0.44), (0.66, 0.36)] if H > W else [(0.25, 0.6), (0.42, 0.46), (0.58, 0.4), (0.68, 0.3)]
    def trail(d):
        for i in range(len(path) - 1):
            (x0, y0), (x1, y1) = path[i], path[i + 1]
            for k in range(14):
                f = k / 14
                x = (x0 + (x1 - x0) * f) * W
                y = (y0 + (y1 - y0) * f) * H + math.sin(f * 9 + i) * s * 0.012
                r = s * 0.0022
                d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    c.shape(trail, "#ffffff", 0.7, blur=1)
    for (fx, fy), sz, col, acc in zip(path, (0.025, 0.03, 0.037, 0.045), ("#fffdf3", "#fff1a8", "#d8ecff", "#ffffff"), ("#f4c26b", "#f2a3b8", "#7fb3e6", "#a6c9f0")):
        butterfly(c, fx * W, fy * H, s * sz, rng.uniform(-0.3, 0.3), col, acc)
    c.finish(2, warm=0.15, vignette=0.12)
    return c


def scene_summer(W, H, rng):
    """你看啊仲夏的弯月 / 倒挂天际的笑靥"""
    c = Canvas(W, H)
    s = c.s
    hz = 0.6 * H
    c.gradient([(0, "#2c3f7c"), (0.3, "#4f68a8"), (0.5, "#8d9fd0"), (hz / H, "#c9c3e0"), (1, "#c9c3e0")])
    def stars(d):
        for _ in range(int(700 * W * H / (1080 * 1920))):
            x, y = rng.uniform(0, W), rng.uniform(0, hz) ** 1.0
            y = rng.uniform(0, 1) ** 1.6 * hz
            r = rng.uniform(0.4, 1.8) * s / 1080
            d.ellipse([x - r, y - r, x + r, y + r], fill=int(rng.uniform(120, 255)))
    c.shape(stars, "#ffffff", 1.0)
    for _ in range(14):
        c.glow(rng.uniform(0, W), rng.uniform(0, 0.45) * H, s * 0.006, "#dfe8ff", 0.8)
    # 银河薄雾
    c.shape(lambda d: d.polygon([(0, 0.3 * H), (W, 0.02 * H), (W, 0.14 * H), (0, 0.44 * H)], fill=255), "#9fb3e6", 0.12, blur=s * 0.06)
    # 笑脸弯月（两角朝上）
    mx, my, mr = 0.5 * W, 0.22 * H, s * 0.11
    c.glow(mx, my + mr * 0.4, mr * 2.5, "#9fb6e8", 0.4)
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=255)
    d.ellipse([mx - mr * 1.02, my - mr * 1.4, mx + mr * 1.02, my + mr * 0.62], fill=0)
    m = m.filter(ImageFilter.GaussianBlur(s * 0.0015))
    c.blend(np.asarray(m, np.float32) / 255, rgb("#fff6d6"))
    c.glow(mx, my + mr * 0.75, mr * 0.9, "#fff1c0", 0.35)
    # 远山与湖
    c.mountain(ridge(W, rng, hz - 0.06 * H, 0.1 * H), "#7183b8", "#a3abd2", 0.1 * H)
    c.mountain(ridge(W, rng, hz - 0.01 * H, 0.05 * H, start=2), "#55669e", "#7385b8", 0.06 * H)
    c.reflect(hz, "#5a6fa6", strength=0.7, ripple=0.003)
    # 月光倒影
    def moonpath(d):
        for _ in range(int(220 * H / 1920)):
            y = rng.uniform(hz + 0.005 * H, H)
            f = (y - hz) / (H - hz)
            w = s * (0.02 + 0.1 * f) * rng.uniform(0.3, 1)
            x = mx + rng.normal(0, s * (0.03 + 0.09 * f))
            hgt = max(1, s * 0.002 * (1 + 2 * f))
            d.rectangle([x - w / 2, y, x + w / 2, y + hgt], fill=255)
    c.shape(moonpath, "#fff1c8", 0.55, blur=0.6)
    # 前景芦苇
    def reeds(d):
        for _ in range(int(90 * W / 1080)):
            x0 = rng.uniform(-0.05, 1.05) * W
            if abs(x0 - mx) < 0.18 * W and rng.random() < 0.8:
                continue
            h = rng.uniform(0.08, 0.3) * H
            bend = rng.uniform(-0.04, 0.04) * W
            pts = [(x0 + bend * (k / 10) ** 2, H - h * k / 10) for k in range(11)]
            d.line(pts, fill=255, width=max(2, int(s * 0.004)))
            tx, ty = pts[-1]
            if rng.random() < 0.6:
                d.polygon(ellipse_pts(tx, ty - s * 0.02, s * 0.006, s * 0.028, bend / W * 3), fill=255)
    c.shape(reeds, "#2b3b6b", 0.95)
    # 萤火虫
    for _ in range(int(40 * W / 1080)):
        x, y = rng.uniform(0, W), rng.uniform(hz - 0.05 * H, H)
        c.glow(x, y, s * rng.uniform(0.004, 0.009), "#e6ff9a", 1.0)
        c.glow(x, y, s * 0.03, "#c8f07a", 0.15)
    c.finish(3, vignette=0.15)
    return c


def scene_autumn(W, H, rng):
    """你听啊秋末的落叶 / 你听它叹息着离别"""
    c = Canvas(W, H)
    s = c.s
    hz = 0.56 * H
    c.gradient([(0, "#f3d9c4"), (0.3, "#fbe6cf"), (hz / H, "#fff3dc"), (1, "#fff3dc")])
    c.glow(0.7 * W, hz - 0.08 * H, s * 0.3, "#ffe1a8", 0.6)
    c.mountain(ridge(W, rng, hz - 0.12 * H, 0.16 * H), "#b7a7c4", "#e3d3d6", 0.2 * H)
    c.mountain(ridge(W, rng, hz - 0.05 * H, 0.1 * H), "#a39ab8", "#d9c3bf", 0.1 * H)
    r = ridge(W, rng, hz - 0.01 * H, 0.06 * H, start=2)
    c.mountain(r, "#c98a5e", "#d9a36b", 0.05 * H)
    # 秋林
    for cols in (("#c4583c", "#e08a3c", "#f0b24a"), ("#d86b3e", "#eaa04a", "#f5c768")):
        for col in cols:
            def trees(d, col=col):
                for _ in range(int(160 * W / 1080)):
                    x = rng.uniform(0, W)
                    y = r[int(min(W - 1, max(0, x)))] + rng.uniform(-0.01, 0.03) * H
                    rr = s * rng.uniform(0.01, 0.025)
                    d.ellipse([x - rr, y - rr * 1.3, x + rr, y + rr], fill=255)
            c.shape(trees, col, 0.9, blur=s * 0.002)
    c.mountain(np.full(W, hz, np.float32), "#d9a36b", "#d9a36b", 1, alpha=1.0)
    c.reflect(hz, "#e9d7c6", strength=0.6, ripple=0.004)
    # 湖面波纹
    def ripples(d):
        for _ in range(int(80 * H / 1920)):
            y = rng.uniform(hz + 0.01 * H, H)
            w = s * rng.uniform(0.05, 0.25)
            x = rng.uniform(0, W)
            d.line([(x - w / 2, y), (x + w / 2, y)], fill=255, width=max(1, int(s * 0.0015)))
    c.shape(ripples, "#fff6ea", 0.4)
    # 右上探出的枝条
    bx, by = W, 0.03 * H
    pts = [(bx - W * 0.55 * k / 10, by + (0.12 * H) * (k / 10) ** 1.6) for k in range(11)]
    c.shape(lambda d: d.line(pts, fill=255, width=int(s * 0.012), joint="curve"), "#5c4136", 1.0)
    for col in ("#c4483a", "#e07a3a", "#f2b04c"):
        def bleaves(d):
            for k in range(14):
                px, py = pts[rng.integers(2, 11)]
                sz = s * rng.uniform(0.025, 0.04)
                d.polygon(leaf_pts(px + rng.normal(0, s * 0.03), py + rng.uniform(0, s * 0.06), sz, rng.uniform(0, 6.28)), fill=255)
        c.shape(bleaves, col, 1.0, blur=0.8)
    # 飘落的叶子（远小近大，近处虚化）
    for k in range(3):
        for col in ("#c4483a", "#e07a3a", "#f2b04c", "#b8642f"):
            def fall(d, k=k):
                n = (5, 2, 1)[k]
                for _ in range(int(n * W / 1080) + 1):
                    sz = s * (0.012, 0.022, 0.05)[k] * rng.uniform(0.7, 1.3)
                    d.polygon(leaf_pts(rng.uniform(0, W), rng.uniform(0.1, 0.95) * H, sz, rng.uniform(0, 6.28)), fill=255)
            c.shape(fall, col, 0.95, blur=(0.5, 1, s * 0.006)[k])
    c.finish(4, warm=0.2, vignette=0.16)
    return c


def scene_sea(W, H, rng):
    """只剩我独自领略 / 海与山 风和月"""
    c = Canvas(W, H)
    s = c.s
    hz = 0.55 * H
    c.gradient([(0, "#38497c"), (0.25, "#7d82b6"), (0.45, "#d7aac4"), (hz / H, "#f8d2b6"), (1, "#f8d2b6")])
    mx, my, mr = 0.66 * W, hz - 0.17 * H, s * 0.075
    c.glow(mx, my, mr * 4, "#ffe9d6", 0.35)
    c.shape(lambda d: d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=255), "#fff8ec", 1.0, blur=s * 0.002)
    c.shape(lambda d: [d.ellipse([mx + ox * mr - rr * mr, my + oy * mr - rr * mr, mx + ox * mr + rr * mr, my + oy * mr + rr * mr], fill=255)
                       for ox, oy, rr in ((-0.3, -0.2, 0.25), (0.25, 0.2, 0.18), (0.1, -0.4, 0.12))], "#f1dccf", 0.4, blur=s * 0.006)
    # 风：几道柔和的弧线
    def wind(d):
        for k in range(6):
            y0 = rng.uniform(0.08, 0.38) * H
            x0 = rng.uniform(-0.2, 0.5) * W
            L = rng.uniform(0.3, 0.6) * W
            pts = [(x0 + L * t / 30, y0 + math.sin(t / 30 * math.pi * 1.4 + k) * s * 0.03) for t in range(31)]
            d.line(pts, fill=255, width=max(2, int(s * 0.003)))
    c.shape(wind, "#ffffff", 0.35, blur=s * 0.002)
    def wisps(d):
        for _ in range(8):
            cx, cy = rng.uniform(0, W), rng.uniform(0.1, 0.4) * H
            w = s * rng.uniform(0.15, 0.35)
            d.ellipse([cx - w, cy - s * 0.01, cx + w, cy + s * 0.012], fill=255)
    c.shape(wisps, "#f7d6dc", 0.35, blur=s * 0.012)
    # 远山与海
    rr = ridge(W, rng, hz - 0.04 * H, 0.08 * H)
    rr = np.minimum(rr + np.clip((c.X[0] - 0.55 * W) / W, 0, 1) * 0.15 * H, hz)
    c.mountain(rr, "#8d86b0", "#c7a9c0", 0.06 * H)
    c.reflect(hz, "#9fa7cc", strength=0.55, ripple=0.004)
    sea_t = np.clip((c.Y - hz) / (H - hz), 0, 1)
    c.blend(np.broadcast_to((c.Y > hz) * (0.25 + 0.4 * sea_t), (H, W)), rgb("#6d86b8"))
    def glitter(d):
        for _ in range(int(320 * H / 1920)):
            y = rng.uniform(hz, H)
            f = (y - hz) / (H - hz)
            w = s * (0.01 + 0.08 * f) * rng.uniform(0.3, 1)
            x = mx + rng.normal(0, s * (0.02 + 0.08 * f))
            d.rectangle([x - w / 2, y, x + w / 2, y + max(1, s * 0.002 * (1 + 2 * f))], fill=255)
    c.shape(glitter, "#fff2df", 0.6, blur=0.6)
    def waves(d):
        for _ in range(int(120 * H / 1920)):
            y = rng.uniform(hz, H)
            f = (y - hz) / (H - hz)
            x = rng.uniform(0, W)
            w = s * (0.03 + 0.15 * f)
            d.arc([x - w, y - s * 0.004 * (1 + 3 * f), x + w, y + s * 0.004 * (1 + 3 * f)], 200, 340, fill=255, width=max(1, int(s * 0.002 * (1 + f))))
    c.shape(waves, "#e6e4f4", 0.35)
    # 左侧海崖 + 独自眺望的人
    cliff_top = 0.66 * H if H > W else 0.6 * H
    cw = 0.38 * W
    cr = ridge(W, rng, cliff_top, 0.03 * H, octaves=4)
    edge = cw + (c.Y[:, 0] - cliff_top) * 0.25
    mask = (c.Y >= cr[None, :]) & (c.X <= edge[:, None])
    c.blend(mask.astype(np.float32), rgb("#3c3a62"))
    def grass(d):
        for _ in range(int(120 * W / 1080)):
            x = rng.uniform(0, cw)
            y = cr[int(x)]
            h = s * rng.uniform(0.01, 0.03)
            d.line([(x, y + 2), (x + rng.uniform(-1, 1) * h * 0.5 + h * 0.4, y - h)], fill=255, width=max(1, int(s * 0.002)))
    c.shape(grass, "#3c3a62", 1.0)
    px = cw * 0.8
    py = cr[int(px)]
    hs = s * 0.05
    c.shape(lambda d: d.polygon([(px - hs * 0.14, py + 2), (px + hs * 0.14, py + 2), (px + hs * 0.1, py - hs * 0.7), (px - hs * 0.1, py - hs * 0.7)], fill=255), "#2a2848", 1.0)
    c.shape(lambda d: d.ellipse([px - hs * 0.11, py - hs * 0.95, px + hs * 0.11, py - hs * 0.72], fill=255), "#2a2848", 1.0)
    c.shape(lambda d: d.polygon([(px + hs * 0.08, py - hs * 0.66), (px + hs * 0.45, py - hs * 0.55), (px + hs * 0.1, py - hs * 0.45)], fill=255), "#2a2848", 0.9)  # 风吹起的围巾
    c.finish(5, vignette=0.2)
    return c


def scene_winter(W, H, rng):
    """你听啊冬至的白雪 / 再没有你的冬眠"""
    c = Canvas(W, H)
    s = c.s
    hz = 0.55 * H
    c.gradient([(0, "#aebfd6"), (0.3, "#d3dde9"), (hz / H, "#f3e9ee"), (1, "#f3e9ee")])
    c.glow(0.3 * W, 0.2 * H, s * 0.35, "#fff0e6", 0.4)
    c.mountain(ridge(W, rng, hz - 0.13 * H, 0.18 * H), "#f4f6fa", "#c4cfde", 0.15 * H)
    r2 = ridge(W, rng, hz - 0.04 * H, 0.08 * H)
    c.mountain(r2, "#ffffff", "#d2dceb", 0.08 * H)
    r3 = ridge(W, rng, hz + 0.07 * H, 0.06 * H, start=2)
    def pines(d, ys, n, sz):
        out = []
        for _ in range(n):
            x = rng.uniform(0, W)
            y = ys[int(x)] + rng.uniform(0, 0.02) * H
            hh = sz * rng.uniform(0.7, 1.3)
            out.append((x, y, hh))
        return out
    def draw_pines(tr, col, snow):
        c.shape(lambda d: [d.polygon([(x, y - h), (x - h * 0.28, y), (x + h * 0.28, y)], fill=255) for x, y, h in tr], col, 1.0)
        c.shape(lambda d: [[d.polygon([(x, y - h + h * k * 0.3), (x - h * (0.1 + 0.07 * k), y - h * (0.78 - 0.3 * k)), (x + h * (0.1 + 0.07 * k), y - h * (0.78 - 0.3 * k))], fill=255) for k in range(3)] for x, y, h in tr], snow, 0.9)
    draw_pines(pines(None, r2, int(60 * W / 1080), s * 0.05), "#8aa0b6", "#f2f5fa")
    c.mountain(r3, "#ffffff", "#e3e9f3", 0.3 * H)
    # 小木屋（暖光窗 = 冬眠）
    hx = 0.36 * W
    hy = r3[int(hx)] + 0.01 * H
    hw, hh = s * 0.16, s * 0.1
    c.shape(lambda d: d.rectangle([hx - hw / 2, hy - hh, hx + hw / 2, hy], fill=255), "#8b6a5b", 1.0)
    c.shape(lambda d: d.polygon([(hx - hw * 0.65, hy - hh), (hx, hy - hh - hw * 0.45), (hx + hw * 0.65, hy - hh)], fill=255), "#ffffff", 1.0)
    c.shape(lambda d: d.polygon([(hx - hw * 0.65, hy - hh), (hx, hy - hh - hw * 0.45), (hx, hy - hh - hw * 0.38), (hx - hw * 0.58, hy - hh + s * 0.008)], fill=255), "#d7e0ee", 0.8)
    c.shape(lambda d: d.rectangle([hx + hw * 0.22, hy - hh - hw * 0.4, hx + hw * 0.32, hy - hh - hw * 0.15], fill=255), "#6e5248", 1.0)
    for k in range(5):
        c.glow(hx + hw * 0.27 + k * s * 0.015, hy - hh - hw * 0.45 - k * s * 0.03, s * (0.012 + 0.006 * k), "#ffffff", 0.35)
    wxl = hx - hw * 0.25
    c.shape(lambda d: d.rectangle([wxl - s * 0.02, hy - hh * 0.7, wxl + s * 0.02, hy - hh * 0.3], fill=255), "#ffd27d", 1.0)
    c.shape(lambda d: d.line([(wxl, hy - hh * 0.7), (wxl, hy - hh * 0.3)], fill=255, width=max(2, int(s * 0.003))), "#8b6a5b", 1.0)
    c.glow(wxl, hy - hh * 0.5, s * 0.06, "#ffb35c", 0.5)
    c.shape(lambda d: d.rectangle([hx + hw * 0.08, hy - hh * 0.65, hx + hw * 0.3, hy], fill=255), "#5d443b", 1.0)
    c.shape(lambda d: d.ellipse([hx - hw * 0.8, hy - s * 0.015, hx + hw * 0.8, hy + s * 0.02], fill=255), "#ffffff", 1.0, blur=s * 0.004)
    # 前景松树
    fg = []
    for x in (0.06, 0.16, 0.86, 0.95):
        fg.append((x * W, r3[int(x * W)] + 0.12 * H + rng.uniform(0, 0.05) * H, s * rng.uniform(0.32, 0.45)))
    draw_pines(fg, "#4f6e6a", "#f6f8fc")
    # 雪地脚印/阴影
    def shadow(d):
        for k in range(10):
            y = hy + (H - hy) * (k + 1) / 11
            d.ellipse([hx - s * 0.006 + k * s * 0.02 * ((-1) ** k * 0.3 + 1), y, hx + s * 0.006 + k * s * 0.02 * ((-1) ** k * 0.3 + 1), y + s * 0.01], fill=255)
    c.shape(shadow, "#b7c3d8", 0.6)
    # 飘雪（三层景深）
    for n, rmin, rmax, bl, al in ((900, 0.0012, 0.0025, 0, 0.85), (220, 0.003, 0.006, 1.2, 0.9), (40, 0.008, 0.016, s * 0.004, 0.75)):
        def snow(d):
            for _ in range(int(n * W * H / (1080 * 1920))):
                x, y = rng.uniform(0, W), rng.uniform(0, H)
                rr = s * rng.uniform(rmin, rmax)
                d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=255)
        c.shape(snow, "#ffffff", al, blur=bl)
    c.finish(6, vignette=0.12)
    return c


SCENES = [
    ("01_巷口窗前", scene_window),
    ("02_春日蝴蝶", scene_spring),
    ("03_仲夏弯月", scene_summer),
    ("04_秋末落叶", scene_autumn),
    ("05_冬至白雪", scene_winter),
]


def main():
    sizes = {"vertical_1080x1920": (1080, 1920)}
    only = sys.argv[1:]
    for folder, (W, H) in sizes.items():
        os.makedirs(os.path.join(OUT, folder), exist_ok=True)
        for i, (name, fn) in enumerate(SCENES):
            if only and not any(o in name for o in only):
                continue
            img = fn(W, H, np.random.default_rng(100 + i)).to_image()
            path = os.path.join(OUT, folder, name + ".jpg")
            img.save(path, quality=93)
            print(path)


if __name__ == "__main__":
    main()
