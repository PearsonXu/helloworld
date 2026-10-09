"""程序化绘制歌曲 MV 竖版背景（电影感 / 极简设计风格，1080x1920）。

用法:
    python3 paint.py          # 重画全部 5 张
    python3 paint.py 03       # 只重画文件名包含 "03" 的那张
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1080, 1920


# ---------------------------------------------------------------- 基础工具
def rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255


def _gauss_fft(a, sigma, axis):
    n = a.shape[axis]
    f = np.fft.rfftfreq(n)
    k = np.exp(-2 * (math.pi * f * sigma) ** 2).astype(np.float32)
    shape = [1] * a.ndim
    shape[axis] = len(k)
    return np.fft.irfft(np.fft.rfft(a, axis=axis) * k.reshape(shape), n=n, axis=axis).astype(np.float32)


def blur(a, sx, sy=None):
    """高斯模糊（float 精度，反射填充避免边缘回卷）。"""
    sy = sx if sy is None else sy
    p = int(3 * max(sx, sy)) + 2
    pad = [(p, p), (p, p)] + [(0, 0)] * (a.ndim - 2)
    b = np.pad(np.asarray(a, np.float32), pad, mode="reflect")
    if sx > 0:
        b = _gauss_fft(b, sx, 1)
    if sy > 0:
        b = _gauss_fft(b, sy, 0)
    return b[p:-p, p:-p]


def fbm(rng, scale, octaves=5, persist=0.5):
    """分形噪声，返回约 0..1。scale = 最大特征尺度（像素）。"""
    out = np.zeros((H, W), np.float32)
    amp, tot = 1.0, 0.0
    for _ in range(octaves):
        gh, gw = max(2, int(H / scale) + 2), max(2, int(W / scale) + 2)
        g = Image.fromarray(rng.normal(0, 1, (gh, gw)).astype(np.float32), "F")
        out += np.asarray(g.resize((W, H), Image.BICUBIC), np.float32) * amp
        tot += amp
        amp *= persist
        scale /= 2
    out /= tot
    return np.clip(out * 0.5 + 0.5, 0, 1)


def ridge(rng, base, amp, start=3, octaves=6):
    x = np.linspace(0, 1, W)
    y = np.zeros(W)
    a = 1.0
    for o in range(octaves):
        n = start * 2 ** o + 1
        k = rng.uniform(-1, 1, n)
        idx = np.minimum((x * (n - 1)).astype(int), n - 2)
        f = x * (n - 1) - idx
        f = (1 - np.cos(f * math.pi)) / 2
        y += (k[idx] * (1 - f) + k[idx + 1] * f) * a
        a *= 0.5
    return (base - y / 1.9 * amp).astype(np.float32)


YY = np.arange(H, dtype=np.float32)[:, None]
XX = np.arange(W, dtype=np.float32)[None, :]


def mask(draw_fn, soft=0.7):
    """用 PIL 画形状得到 0..1 遮罩（2x 超采样抗锯齿）。"""
    ss = 2
    m = Image.new("L", (W * ss, H * ss), 0)
    draw_fn(ImageDraw.Draw(m), ss)
    m = m.resize((W, H), Image.LANCZOS)
    a = np.asarray(m, np.float32) / 255
    return blur(a, soft) if soft else a


def radial(cx, cy, r, power=2.0):
    d = np.sqrt((XX - cx) ** 2 + (YY - cy) ** 2) / r
    return np.exp(-d ** power)


def S(pts, ss):
    return [(x * ss, y * ss) for x, y in pts]


def ell(cx, cy, rx, ry, rot, n=48):
    cr, sr = math.cos(rot), math.sin(rot)
    return [(cx + rx * math.cos(t) * cr - ry * math.sin(t) * sr, cy + rx * math.cos(t) * sr + ry * math.sin(t) * cr)
            for t in (2 * math.pi * i / n for i in range(n))]


class Canvas:
    def __init__(self):
        self.a = np.zeros((H, W, 3), np.float32)

    def gradient(self, stops):
        t = np.broadcast_to(YY / (H - 1), (H, W))
        ps = [p for p, _ in stops]
        for ch in range(3):
            self.a[..., ch] = np.interp(t, ps, [rgb(c)[ch] for _, c in stops])

    def over(self, alpha, color):
        al = np.clip(alpha, 0, 1)
        if al.ndim == 2:
            al = al[..., None]
        col = rgb(color) if isinstance(color, str) else color
        self.a = self.a * (1 - al) + col * al

    def add(self, light, color=None, k=1.0):
        """屏幕叠加光（screen）。"""
        l = np.clip(light, 0, None)
        if l.ndim == 2:
            l = l[..., None] * (rgb(color) if isinstance(color, str) else color)
        self.a = 1 - (1 - self.a) * (1 - np.clip(l * k, 0, 1))

    def mul(self, m, color):
        m = np.clip(m, 0, 1)[..., None]
        self.a = self.a * (1 - m + m * rgb(color))

    def bokeh(self, items, soft=1.2):
        """items: (x, y, r, color, intensity) 虚化光斑，边缘略亮更像镜头焦外。"""
        layer = np.zeros((H, W, 3), np.float32)
        for x, y, r, col, inten in items:
            x0, x1 = int(max(0, x - r - 3)), int(min(W, x + r + 4))
            y0, y1 = int(max(0, y - r - 3)), int(min(H, y + r + 4))
            if x0 >= x1 or y0 >= y1:
                continue
            gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
            d = np.sqrt((gx - x) ** 2 + (gy - y) ** 2)
            disc = np.clip(r - d + 0.8, 0, 1)
            ring = 0.72 + 0.28 * np.clip(d / max(r, 1), 0, 1) ** 3
            layer[y0:y1, x0:x1] += (disc * ring * inten)[..., None] * rgb(col)
        self.add(blur(layer, soft))

    def finish(self, seed, grain=0.022, vignette=0.22, lift=0.035, warm=0.0, sat=1.0):
        rng = np.random.default_rng(seed)
        a = self.a
        lum = (a @ np.array([0.299, 0.587, 0.114], np.float32))[..., None]
        a = lum + (a - lum) * sat
        if warm:
            a = a * (1 + np.array([0.04, 0.0, -0.05], np.float32) * warm)
        # 柔和 S 曲线 + 抬黑（胶片哑光感）
        a = np.clip(a, 0, 1)
        a = a + 0.12 * (a - 0.5) * (1 - np.abs(2 * a - 1))
        a = lift + (1 - lift * 1.4) * a
        # 暗角
        d = np.sqrt(((XX - W / 2) / (W / 2)) ** 2 + ((YY - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
        a = a * (1 - vignette * d ** 2.2)[..., None]
        # 胶片颗粒（中间调更明显）
        lum = a @ np.array([0.299, 0.587, 0.114], np.float32)
        g = blur(rng.normal(0, 1, (H, W)).astype(np.float32), 0.55) * 1.6
        a = a + (g * grain * (0.35 + 0.65 * (1 - np.abs(2 * lum - 1))))[..., None]
        self.a = a

    def save(self, path):
        d = np.random.default_rng(0).uniform(-0.5, 0.5, self.a.shape) / 255  # 抖动，避免渐变色带
        im = Image.fromarray((np.clip(self.a + d, 0, 1) * 255 + 0.5).astype(np.uint8))
        im.save(path, quality=95, subsampling=0)


def mirror_water(c, hz, squash, bx, by, tint, k_tint):
    """把地平线以上的画面翻转成水面倒影，并做横向模糊。"""
    h = int(hz)
    sky = c.a[:h].copy()
    n_w = H - h
    idx = np.clip(h - 1 - (np.arange(n_w) * squash).astype(int), 0, h - 1)
    water = np.stack([blur(sky[idx][..., k], bx, by) for k in range(3)], -1)
    t = (np.arange(n_w, dtype=np.float32) / n_w)[:, None, None]
    kt = k_tint[0] + (k_tint[1] - k_tint[0]) * t
    c.a[h:] = water * (1 - kt) + rgb(tint) * kt


# ---------------------------------------------------------------- 1 巷口暖灯 · 冷咖啡 · 窗边夜色
def scene_window(rng):
    c = Canvas()
    c.gradient([(0, "#16233b"), (0.35, "#22344f"), (0.62, "#2c3d57"), (1, "#1c2638")])
    # 虚化的巷子：两侧楼体剪影
    bld = np.zeros((H, W), np.float32)
    for x0, x1, top in ((0, 0.3, 0.2), (0.25, 0.42, 0.38), (0.58, 0.74, 0.34), (0.7, 1.0, 0.16)):
        bld[int(top * H):int(0.78 * H), int(x0 * W):int(x1 * W)] = 1
    c.over(blur(bld, 26) * 0.75, "#0a101b")
    lx, ly = 0.68 * W, 0.40 * H
    # 地面湿反光
    c.add(blur((YY > 0.66 * H) * radial(lx, 0.72 * H, 0.35 * W, 1.5), 30) * 0.35, "#e0a36a")
    # 巷口路灯：大光斑 + 柔光 + 外圈光晕（忽明忽灭）
    c.add(radial(lx, ly, 0.42 * W, 1.3) * 0.32, "#d98a4e")
    c.add(radial(lx, ly, 0.16 * W, 1.6) * 0.55, "#ffb871")
    c.bokeh([(lx, ly, 0.075 * W, "#ffd59c", 1.0)], soft=2.0)
    ringd = np.sqrt((XX - lx) ** 2 + (YY - ly) ** 2)
    c.add(np.exp(-((ringd - 0.13 * W) / (0.01 * W)) ** 2) * 0.10, "#ffcf9a")
    # 远处窗灯与车灯光斑
    items = []
    for _ in range(46):
        x = rng.uniform(0, W)
        y = rng.uniform(0.24, 0.7) * H
        if abs(x - lx) < 0.12 * W and abs(y - ly) < 0.08 * H:
            continue
        r = rng.uniform(0.012, 0.045) * W
        col = rng.choice(["#ffc684", "#ffb36b", "#ffd9a8", "#9fc3ff", "#e8d9ff"], p=[0.35, 0.25, 0.2, 0.12, 0.08])
        items.append((x, y, r, col, rng.uniform(0.12, 0.45)))
    c.bokeh(items, soft=1.5)
    # 玻璃上的雾气（被灯光照亮），以及被水滴划开的清晰轨迹
    fog = fbm(rng, 500, 5) * 0.5 + fbm(rng, 60, 3) * 0.5
    fog = np.clip((fog - 0.35) * 1.6, 0, 1) * (0.18 + 0.25 * np.clip((YY / H - 0.45) * 2, 0, 1))
    wipe = np.zeros((H, W), np.float32)
    trails = []
    for _ in range(5):
        x = rng.uniform(0.05, 0.95) * W
        y0, y1 = rng.uniform(0.05, 0.4) * H, rng.uniform(0.55, 0.8) * H
        xs = x + np.cumsum(rng.normal(0, 0.6, int(y1 - y0)))
        trails.append((xs[-1], y1))
        for i, xx in enumerate(xs):
            wipe[int(y0) + i, int(max(0, xx - 3)):int(min(W, xx + 4))] = 1
    fog *= np.clip(1 - blur(wipe, 2.5) * 1.2, 0.35, 1)
    lampfog = radial(lx, ly, 0.5 * W, 1.2)
    c.add(fog * (0.5 + 1.6 * lampfog), "#b9c6da")
    # 水珠：暗边 + 底部折射亮光 + 左上高光
    drops = np.zeros((H, W), np.float32)
    spec = np.zeros((H, W), np.float32)
    rims = np.zeros((H, W), np.float32)

    def put(x, y, r):
        x0, x1 = int(max(0, x - r - 2)), int(min(W, x + r + 3))
        y0, y1 = int(max(0, y - r * 1.2 - 2)), int(min(H, y + r * 1.2 + 3))
        if x0 >= x1 or y0 >= y1:
            return
        gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        d = np.sqrt(((gx - x) / r) ** 2 + ((gy - y) / (r * 1.12)) ** 2)
        inside = np.clip((1 - d) * r, 0, 1)
        sl = (slice(y0, y1), slice(x0, x1))
        drops[sl] = np.maximum(drops[sl], inside * np.clip((gy - y) / r * 0.5 + 0.5, 0, 1))
        rims[sl] = np.maximum(rims[sl], inside * np.clip(d - 0.65, 0, 1) * 2.5)
        hs = np.sqrt((gx - (x - r * 0.35)) ** 2 + (gy - (y - r * 0.4)) ** 2) / (r * 0.22)
        spec[sl] = np.maximum(spec[sl], np.exp(-hs ** 2))

    for _ in range(420):
        y = rng.uniform(0.0, 0.86) ** 0.8 * H
        put(rng.uniform(0, W), y, rng.uniform(1.5, 5.5) * (0.6 + 0.8 * y / H))
    for x, y in trails:
        put(x, y, rng.uniform(7, 11))
    c.over(rims * 0.55, "#070b14")
    c.add(drops * (0.25 + 0.9 * lampfog), "#ffd8a6")
    c.add(spec * (0.5 + 0.6 * lampfog), "#fff4e4")
    # 窗台
    sill = 0.86 * H
    c.over(np.broadcast_to((YY > sill).astype(np.float32), (H, W)), "#1a202c")
    c.add((YY > sill) * radial(lx, sill, 0.5 * W, 1.4) * 0.12, "#ffb978")
    c.add(np.exp(-((YY - sill) / 2.2) ** 2) * radial(lx, sill, 0.6 * W, 1.5) * 0.7, "#ffcf98")
    # 冷掉的咖啡杯（逆光剪影 + 右侧暖色轮廓光，没有热气）
    mx, mw, mh = 0.3 * W, 0.17 * W, 0.105 * H

    def mug(d, ss):
        d.rounded_rectangle(S([(mx - mw / 2, sill - mh), (mx + mw / 2, sill)], ss), radius=int(0.022 * W * ss), fill=255)
        d.ellipse(S([(mx + mw * 0.36, sill - mh * 0.82), (mx + mw * 0.86, sill - mh * 0.28)], ss), outline=255, width=int(0.016 * W * ss))

    mm = mask(mug, 0.9)
    c.over(mm, "#0d121c")
    rim = np.clip(mm - np.roll(mm, -5, axis=1), 0, 1) + np.clip(mm - np.roll(mm, 4, axis=0), 0, 1) * 0.5
    c.add(blur(rim, 1.2) * 0.8, "#ffbf7e")
    c.add(blur(np.clip(mm - np.roll(mm, -2, axis=1), 0, 1), 0.6) * 0.6, "#ffe1b5")
    # 杯口液面的一圈微光
    c.add(mask(lambda d, ss: d.ellipse(S([(mx - mw * 0.42, sill - mh - 0.004 * H), (mx + mw * 0.42, sill - mh + 0.006 * H)], ss), outline=255, width=2 * ss), 0.8) * 0.35, "#c79a74")
    # 杯子在窗台上的倒影
    refl = np.zeros((H, W), np.float32)
    top = int(sill - mh)
    n = H - int(sill)
    refl[int(sill):] = mm[int(sill) - 1 - np.arange(n).clip(0, int(sill) - top)][:n]
    c.add(blur(refl, 6) * 0.07, "#ffbf7e")
    c.finish(11, grain=0.026, vignette=0.24, lift=0.045, sat=0.95)
    return c


# ---------------------------------------------------------------- 2 春日蝴蝶 · 和风 · 暖阳 · 草地与花影
def scene_spring(rng):
    c = Canvas()
    hz = 0.6 * H
    c.gradient([(0, "#cfe3ea"), (0.25, "#e6eee6"), (0.5, "#eef0dc"), (hz / H, "#c6d9a8"), (0.8, "#98ba78"), (1, "#6f9560")])
    sx, sy = 0.06 * W, 0.08 * H
    # 远处树影，空气透视
    tree = ridge(rng, hz - 0.035 * H, 0.06 * H, start=6)
    c.over(blur((YY > tree[None, :]).astype(np.float32), 8) * 0.7 * (YY < hz + 0.02 * H), "#9db890")
    # 草地纹理
    tex = fbm(rng, 120, 5)
    c.mul(np.clip((tex - 0.5) * 0.6, 0, 1) * (YY > hz), "#7f9a64")

    # 倾斜的暖阳光束
    def ray_draw(d, ss):
        for k in range(7):
            a = 0.62 + k * 0.1 + rng.uniform(-0.03, 0.03)
            w = rng.uniform(0.015, 0.04)
            L = 2.4 * H
            d.polygon(S([(sx, sy), (sx + L * math.cos(a - w), sy + L * math.sin(a - w)), (sx + L * math.cos(a + w), sy + L * math.sin(a + w))], ss), fill=int(rng.uniform(120, 255)))

    rays = blur(mask(ray_draw, 0), 22) * np.clip(1 - np.sqrt((XX - sx) ** 2 + (YY - sy) ** 2) / (1.6 * H), 0, 1)
    c.add(rays * 0.28, "#fff1cf")
    c.add(radial(sx, sy, 0.7 * W, 1.1) * 0.55, "#fff0cc")
    c.add(radial(sx, sy, 0.12 * W, 1.6) * 0.9, "#ffffff")
    # 镜头鬼影
    for t, r, a in ((0.45, 0.03, 0.06), (0.62, 0.05, 0.05), (0.9, 0.09, 0.035)):
        c.bokeh([(sx + (W / 2 - sx) * 2 * t, sy + (H / 2 - sy) * 2 * t, r * W, "#d9f0c8", a)], soft=3)
    # 花影：斑驳的树叶阴影投在草地上
    dap = fbm(rng, 40, 4)
    dap = blur(np.clip((dap - 0.52) * 6, 0, 1), 3) * np.clip((YY - hz) / (0.15 * H), 0, 1) * np.clip((XX - 0.3 * W) / (0.6 * W), 0, 1)
    c.mul(dap * 0.3, "#7d9a72")
    # 野花：焦外光斑
    items = []
    for _ in range(170):
        y = rng.uniform(hz + 0.01 * H, H)
        f = (y - hz) / (H - hz)
        r = (0.003 + 0.03 * f ** 2) * W * rng.uniform(0.6, 1.3)
        col = rng.choice(["#ffffff", "#fde3ea", "#fff1b8", "#e9dcff"], p=[0.45, 0.3, 0.15, 0.1])
        items.append((rng.uniform(0, W), y, r, col, rng.uniform(0.35, 0.8)))
    c.bokeh(items, soft=1.0)
    # 光中漂浮的花粉
    c.bokeh([(rng.uniform(0, W * 0.8), rng.uniform(0.05, 0.6) * H, rng.uniform(1.5, 9), "#fff6e0", rng.uniform(0.2, 0.6)) for _ in range(60)], soft=1.2)
    # 草叶：远处清晰，近处大幅虚化；统一向右倾（和风）
    for layer, (n, hmin, hmax, wd, bl, col, al) in enumerate((
            (900, 0.01, 0.03, 1, 0.4, "#7d9a5e", 0.8),
            (260, 0.04, 0.09, 2, 1.2, "#6a8a55", 0.85),
            (60, 0.12, 0.3, 6, 9, "#4f6f45", 0.9))):
        def grass(d, ss):
            for _ in range(n):
                x = rng.uniform(-0.05, 1.05) * W
                y = rng.uniform(hz + 0.02 * H, H) if layer == 0 else rng.uniform(0.86 + 0.05 * layer, 1.02) * H
                h = rng.uniform(hmin, hmax) * H
                lean = rng.uniform(0.15, 0.45) * h
                pts = [(x + lean * (k / 8) ** 2, y - h * k / 8) for k in range(9)]
                d.line(S(pts, ss), fill=255, width=wd * ss)
        c.over(blur(mask(grass, 0), bl) * al, col)

    # 蝴蝶：一只清晰（逆光半透明翅膀），一只远处虚化
    def fly(bx, by, sz, rot, bl, al):
        def wings(d, ss):
            for sd in (-1, 1):
                d.polygon(S(ell(bx + sd * sz * 0.5, by - sz * 0.25, sz * 0.55, sz * 0.36, rot + sd * -0.55), ss), fill=255)
                d.polygon(S(ell(bx + sd * sz * 0.34, by + sz * 0.28, sz * 0.32, sz * 0.26, rot + sd * 0.7), ss), fill=255)
        m = mask(wings, bl)
        c.over(m * 0.85 * al, "#f3d89a")
        c.over(np.clip(m - blur(m, 3), 0, 1) * 0.9 * al, "#8a6a48")
        c.add(m * 0.25 * al, "#fff2cf")
        body = mask(lambda d, ss: d.polygon(S(ell(bx, by, sz * 0.05, sz * 0.42, rot), ss), fill=255), bl)
        c.over(body * 0.8 * al, "#5b4a3d")
        c.add(radial(bx, by, sz * 1.8) * 0.18 * al, "#fff5dc")

    fly(0.6 * W, 0.44 * H, 0.05 * W, 0.25, 0.6, 1.0)
    fly(0.34 * W, 0.53 * H, 0.03 * W, -0.3, 3.0, 0.7)
    fly(0.8 * W, 0.36 * H, 0.022 * W, 0.4, 1.5, 0.8)
    c.finish(12, grain=0.018, vignette=0.14, lift=0.03, warm=0.5, sat=1.0)
    return c


# ---------------------------------------------------------------- 3 仲夏弯月 · 澄澈夜空 · 湖面倒影
def scene_summer(rng):
    c = Canvas()
    hz = 0.64 * H
    c.gradient([(0, "#060d22"), (0.25, "#0d1f48"), (0.48, "#1d4377"), (hz / H, "#4f7fa8"), (1, "#4f7fa8")])
    c.add(radial(0.5 * W, hz, 0.8 * W, 1.6) * 0.25 * (YY < hz), "#9cc2d8")
    # 银河
    band = np.exp(-(((YY - (0.55 * H - 0.55 * XX)) / (0.09 * H)) ** 2))
    mw = fbm(rng, 260, 6) * fbm(rng, 80, 4)
    c.add(band * np.clip((mw - 0.15) * 2.2, 0, 1) * 0.35 * (YY < hz), "#c7d2f2")
    # 星星（幂律亮度）
    stars = np.zeros((H, W), np.float32)
    n = 2600
    xs = rng.uniform(0, W, n).astype(int)
    ys = (rng.uniform(0, 1, n) ** 1.2 * hz * 0.98).astype(int)
    stars[ys, xs] = rng.pareto(2.2, n).clip(0, 6) * 0.35 + 0.15
    c.add(blur(stars, 0.7) * 3.2, "#f2f5ff")
    for _ in range(16):
        c.add(radial(rng.uniform(0, W), rng.uniform(0.02, 0.5) * H, rng.uniform(3, 6), 1.2) * 0.7, rng.choice(["#ffffff", "#dbe6ff", "#fff1d6"]))
    # 细弯月（两角朝上，像倒挂天际的笑靥）
    mx, my, mr = 0.64 * W, 0.2 * H, 0.052 * W
    c.add(radial(mx, my + mr * 0.5, mr * 7, 1.3) * 0.22, "#a8bfe6")
    c.add(radial(mx, my + mr * 0.5, mr * 2.2, 1.5) * 0.25, "#f2e8cc")

    def moon(d, ss):
        d.ellipse(S([(mx - mr, my - mr), (mx + mr, my + mr)], ss), fill=255)
        d.ellipse(S([(mx - mr * 1.04, my - mr * 1.32), (mx + mr * 1.04, my + mr * 0.74)], ss), fill=0)

    c.over(mask(moon, 0.5), "#fbf3dc")
    # 远方极低的山线
    hill = ridge(rng, hz, 0.022 * H, start=4)
    c.over(np.clip(YY - hill[None, :] + 0.5, 0, 1) * (YY <= hz + 1), "#0a1630")
    # 湖面：天空镜像 + 横向模糊
    mirror_water(c, hz, 0.9, 5, 0.8, "#0b1a36", (0.2, 0.45))
    # 月光倒影：碎银
    glit = np.zeros((H, W), np.float32)
    for _ in range(900):
        y = rng.uniform(hz + 2, H)
        f = (y - hz) / (H - hz)
        x = mx + rng.normal(0, 0.012 * W + 0.06 * W * f)
        w = rng.uniform(4, 22) * (0.4 + 2.2 * f)
        glit[int(y):int(y + 1 + 2 * f), int(max(0, x - w / 2)):int(min(W, x + w / 2))] += rng.uniform(0.3, 1.0)
    c.add(blur(glit, 1.0, 0.5) * 0.8 * np.exp(-((YY - hz) / (0.32 * H)) ** 2 * 0.6), "#f7ecd0")
    c.add(np.exp(-((XX - mx) / (0.05 * W)) ** 2) * np.exp(-((YY - hz - 0.02 * H) / (0.05 * H)) ** 2) * 0.3, "#f7ecd0")
    # 极细的水面横纹
    rip = np.clip(blur(fbm(rng, 30, 3), 40, 0.5) - 0.5, 0, 1) * 2 * (YY > hz)
    c.add(rip * 0.12, "#9ab8d8")
    # 地平线薄雾
    c.add(np.exp(-((YY - hz) / (0.012 * H)) ** 2) * 0.25, "#aac6dc")
    c.finish(13, grain=0.02, vignette=0.2, lift=0.03, sat=0.95)
    return c


# ---------------------------------------------------------------- 4 秋末落叶 · 远山与海 · 风吹空旷长堤
def scene_autumn(rng):
    c = Canvas()
    hz = 0.6 * H
    c.gradient([(0, "#cfcac6"), (0.3, "#e2dbd2"), (0.52, "#efe4d5"), (hz / H, "#f3e7d6"), (1, "#c9c4be")])
    c.add(radial(0.72 * W, 0.42 * H, 0.45 * W, 1.3) * 0.35, "#ffe2b8")
    # 层云
    cl = blur(fbm(rng, 400, 6), 30, 4)
    c.over(np.clip((cl - 0.5) * 2.5, 0, 1) * 0.35 * (YY < hz), "#f7f1e8")
    c.mul(np.clip((0.45 - cl) * 2, 0, 1) * 0.25 * (YY < hz), "#b9b2b0")
    # 远山三层（空气透视）
    for base, amp, col, haze, st in ((hz - 0.11 * H, 0.14 * H, "#b9b6bd", 0.45, 3),
                                     (hz - 0.05 * H, 0.1 * H, "#9c9ca8", 0.3, 4),
                                     (hz - 0.012 * H, 0.05 * H, "#7d808c", 0.15, 5)):
        r = ridge(rng, base, amp, start=st)
        m = np.clip(YY - r[None, :] + 0.5, 0, 1) * (YY <= hz + 1)
        tx = fbm(rng, 60, 4)
        shade = rgb(col) * (0.92 + 0.16 * tx[..., None])
        fade = np.clip((YY - r[None, :]) / (0.08 * H), 0, 1)[..., None]
        shade = shade * (1 - fade * haze) + rgb("#efe4d5") * fade * haze
        c.a = c.a * (1 - m[..., None]) + shade * m[..., None]
    # 海面
    sea_t = np.clip((YY - hz) / (H - hz), 0, 1)
    mirror_water(c, hz, 1.1, 18, 2, "#a7a9ae", (0.45, 0.45))
    c.mul(sea_t * 0.35, "#7f8a96")
    c.add(np.clip(blur(fbm(rng, 40, 3), 60, 0.6) - 0.52, 0, 1) * 3 * (YY > hz) * 0.25, "#f2ece2")
    c.add(np.exp(-((YY - hz) / (0.006 * H)) ** 2) * 0.3, "#f5ead9")
    # 长堤：从右下伸向远方的消失点
    vx, vy = 0.3 * W, hz + 0.004 * H
    top = [(vx, vy), (vx + 0.012 * W, vy), (1.25 * W, 1.0 * H), (0.58 * W, 1.0 * H)]
    side = [(vx, vy), (vx, vy + 0.003 * H), (0.5 * W, 1.0 * H), (0.58 * W, 1.0 * H)]
    c.over(mask(lambda d, ss: d.polygon(S(side, ss), fill=255)), "#6c6966")
    stone = mask(lambda d, ss: d.polygon(S(top, ss), fill=255))
    c.over(stone, "#cfc5b8")
    c.mul(stone * np.clip(fbm(rng, 6, 2) - 0.45, 0, 1) * 0.6, "#a59b8f")
    c.mul(stone * np.clip((YY - vy) / (H - vy), 0, 1) * 0.25, "#8f877d")

    # 堤上被风吹弯的细树（越远越小）
    def trees(d, ss):
        for k in range(9):
            t = (1 - k / 8.6) ** 2.2
            px = vx + 0.006 * W + (0.98 * W - vx) * t
            py = vy + (H * 0.98 - vy) * t
            h = 0.02 * H + 0.42 * H * t
            lean = h * 0.18
            pts = [(px + lean * (j / 10) ** 1.8, py - h * j / 10) for j in range(11)]
            wd = max(1, int((1 + 9 * t) * ss))
            d.line(S(pts, ss), fill=255, width=wd)
            for _ in range(4):
                bx, by = pts[rng.integers(5, 10)]
                L = h * rng.uniform(0.12, 0.25)
                ang = -math.pi / 2 + rng.uniform(0.3, 0.9)
                d.line(S([(bx, by), (bx + L * math.cos(ang), by + L * math.sin(ang))], ss), fill=255, width=max(1, wd // 2))

    c.over(mask(trees, 0.6) * 0.85, "#3f3a37")
    # 被风吹起的落叶（运动模糊，暖橙色点缀）
    for n, sz, bl, al in ((30, 0.006, (3, 1), 0.85), (8, 0.014, (9, 2), 0.8), (3, 0.03, (22, 6), 0.7)):
        for col in ("#c4703a", "#d89048", "#a8502e"):
            def lv(d, ss):
                for _ in range(n // 3 + 1):
                    x, y = rng.uniform(0, W), rng.uniform(0.25, 0.95) * H
                    r = sz * W * rng.uniform(0.7, 1.3)
                    d.polygon(S(ell(x, y, r, r * 0.55, rng.uniform(0, 3.14)), ss), fill=255)
            c.over(blur(mask(lv, 0), *bl) * al, col)
    # 风：近乎看不见的横向气流
    c.add(np.clip(blur(fbm(rng, 50, 3), 120, 3) - 0.5, 0, 1) * 0.15, "#ffffff")
    c.finish(14, grain=0.024, vignette=0.18, lift=0.04, warm=0.4, sat=0.85)
    return c


# ---------------------------------------------------------------- 5 冬至白雪 · 安静街灯 · 留白的夜
def scene_winter(rng):
    c = Canvas()
    gz = 0.74 * H
    c.gradient([(0, "#1d2944"), (0.35, "#34466a"), (0.62, "#6a7d9e"), (gz / H, "#9eabc2"), (gz / H + 0.001, "#b8c4d8"), (1, "#dfe6f1")])
    # 远处被雪雾吞没的楼
    far = np.zeros((H, W), np.float32)
    for _ in range(14):
        x0 = rng.uniform(-0.05, 1.0) * W
        w = rng.uniform(0.06, 0.18) * W
        top = gz - rng.uniform(0.04, 0.16) * H
        far[int(top):int(gz), int(max(0, x0)):int(min(W, x0 + w))] = 1
    c.over(blur(far, 14) * 0.35, "#4a5a7a")
    c.bokeh([(rng.uniform(0, W), rng.uniform(gz - 0.12 * H, gz - 0.01 * H), rng.uniform(4, 12), "#ffc98c", rng.uniform(0.2, 0.5)) for _ in range(16)], soft=3)
    c.add(np.exp(-((YY - gz) / (0.03 * H)) ** 2) * 0.25, "#c6d0e2")
    # 雪地起伏
    c.mul(np.clip(fbm(rng, 200, 4) - 0.5, 0, 1) * (YY > gz) * 0.5, "#a3b1ca")
    # 街灯
    lx, ly = 0.27 * W, 0.44 * H
    post = mask(lambda d, ss: (d.rectangle(S([(lx - 4, ly), (lx + 4, gz + 0.04 * H)], ss), fill=255),
                               d.polygon(S([(lx - 22, ly + 4), (lx + 22, ly + 4), (lx + 14, ly - 24), (lx - 14, ly - 24)], ss), fill=255),
                               d.rectangle(S([(lx - 6, ly - 34), (lx + 6, ly - 24)], ss), fill=255)), 0.6)
    c.over(post, "#151b2a")
    # 灯光锥
    cone = mask(lambda d, ss: d.polygon(S([(lx - 18, ly + 4), (lx + 18, ly + 4), (lx + 0.3 * W, gz + 0.05 * H), (lx - 0.3 * W, gz + 0.05 * H)], ss), fill=255), 0)
    cone = blur(cone, 28) * np.clip(1 - (YY - ly) / (0.5 * H), 0, 1)
    c.add(cone * 0.22, "#ffd6a0")
    c.add(radial(lx, ly, 0.38 * W, 1.2) * 0.28, "#ffb978")
    c.add(radial(lx, ly + 6, 0.05 * W, 1.6) * 1.0, "#fff1d2")
    pool = np.exp(-(((XX - lx) / (0.32 * W)) ** 2 + ((YY - gz - 0.05 * H) / (0.05 * H)) ** 2))
    c.add(pool * 0.35, "#ffd7a8")
    # 灯下一条覆雪的长椅
    bx, by = 0.52 * W, gz + 0.03 * H
    bench = mask(lambda d, ss: (d.rectangle(S([(bx, by - 26), (bx + 170, by - 18)], ss), fill=255),
                                d.rectangle(S([(bx, by - 52), (bx + 170, by - 44)], ss), fill=255),
                                d.rectangle(S([(bx + 12, by - 52), (bx + 18, by)], ss), fill=255),
                                d.rectangle(S([(bx + 152, by - 52), (bx + 158, by)], ss), fill=255)), 0.6)
    c.over(bench * 0.9, "#22283a")
    snowcap = mask(lambda d, ss: (d.rounded_rectangle(S([(bx - 2, by - 31), (bx + 172, by - 25)], ss), radius=3 * ss, fill=255),
                                  d.rounded_rectangle(S([(bx - 2, by - 57), (bx + 172, by - 51)], ss), radius=3 * ss, fill=255)), 0.6)
    c.over(snowcap, "#e9eef7")
    c.over(blur(np.roll(bench, 18, axis=0) * (YY > by - 2), 8, 3) * 0.25, "#7f8db0")

    # 一串走远的脚印
    def steps(d, ss):
        for k in range(26):
            y = H - (1 - (1 - k / 26) ** 1.6) * (H - gz) - 10
            f = (y - gz) / (H - gz)
            x = 0.62 * W - (1 - f) * 0.25 * W + (k % 2) * 16 * f
            rx, ry = 2 + 6 * f, 1 + 3 * f
            d.ellipse(S([(x - rx, y - ry), (x + rx, y + ry)], ss), fill=255)

    c.mul(mask(steps, 1.0) * 0.35, "#7d8aa6")
    # 雪：三层景深，灯光锥中更亮
    lit = 0.35 + 1.4 * np.clip(cone * 4 + radial(lx, ly, 0.3 * W) * 0.8, 0, 1)
    for n, rmin, rmax, bl, al in ((2200, 0.8, 1.8, 0.4, 0.7), (500, 2, 3.5, 1.2, 0.75), (60, 6, 14, 5, 0.5)):
        layer = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(layer)
        for _ in range(n):
            x, y = rng.uniform(0, W), rng.uniform(0, gz + 0.1 * H)
            r = rng.uniform(rmin, rmax)
            d.ellipse([x - r, y - r, x + r, y + r], fill=int(rng.uniform(150, 255)))
        sn = blur(np.asarray(layer, np.float32) / 255, bl)
        c.add(sn * al * lit, "#ffffff")
        c.add(sn * al * cone * 2.5, "#ffd8a8")
    c.finish(15, grain=0.024, vignette=0.22, lift=0.035, sat=0.9)
    return c


SCENES = [
    ("01_巷口暖灯", scene_window),
    ("02_春日蝴蝶", scene_spring),
    ("03_仲夏弯月", scene_summer),
    ("04_秋末长堤", scene_autumn),
    ("05_冬至白雪", scene_winter),
]


def main():
    only = sys.argv[1:]
    folder = os.path.join(OUT, "vertical_1080x1920")
    os.makedirs(folder, exist_ok=True)
    for i, (name, fn) in enumerate(SCENES):
        if only and not any(o in name for o in only):
            continue
        path = os.path.join(folder, name + ".jpg")
        fn(np.random.default_rng(200 + i)).save(path)
        print(path)


if __name__ == "__main__":
    main()
