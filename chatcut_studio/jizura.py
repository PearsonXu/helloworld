"""JIZURA 动态排版引擎的融合层。

JIZURA（https://github.com/852wa/JIZURA ，MIT）是浏览器里的歌词 PV 生成器：600 多个版式 / 入场 / 出场 /
文字处理 / 装饰部件，按随机种子编排。源码原样放在 third_party/jizura/src，这里不改它，只在外面做四件事：

1. 渲染：在无头 Chromium 里加载 JIZURA，逐帧调用 Renderer.frame() 画「透明背景」的歌词层，
   PNG 流交给 ffmpeg，叠到我们的背景图上、合上音频，或直接输出透明 .mov 给 ChatCut。
2. 中文：JIZURA 的分镜器按日文规则写，会把中文拆在词中间（「笑|靥」）。我们先用浏览器自带的中文分词，
   按「单字修饰词向后黏、助词语气词向前黏」切好，用 JIZURA 的手动分镜语法（a/b/c）交给它。
3. 主题：从 JIZURA 部件库里挑选组合成 4 套适合中文歌的主题（叠字海报、清新抒情、动感卡点、日系文字PV），
   默认关掉恐怖部件和「No.04 / 00:12.34」这类装饰性编号。
4. 字体：简体中文用到的字体（思源黑体 / 宋体、站酷三款、马善政）放在本地，不依赖 Google Fonts。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import shutil
import urllib.request
from pathlib import Path

from .lyrics import Line, RenderError, background_input, background_schedule, fit_filter
from .profile import Jizura
from .settings import DATA_ROOT

SRC_DIR = Path(__file__).parent / "third_party" / "jizura" / "src"
FONT_DIR = Path(os.environ.get("CHATCUT_STUDIO_JZ_FONTS", Path.home() / ".cache" / "chatcut-studio" / "fonts"))
GF = "https://raw.githubusercontent.com/google/fonts/main/ofl"
# (CSS 字体族名, 本地文件名, 字重范围, 下载地址) —— JIZURA 简体中文版用到的全部字体
FONTS = [
    ("Noto Sans SC", "NotoSansSC-VF.ttf", "100 900", f"{GF}/notosanssc/NotoSansSC%5Bwght%5D.ttf"),
    ("Noto Serif SC", "NotoSerifSC-VF.ttf", "200 900", f"{GF}/notoserifsc/NotoSerifSC%5Bwght%5D.ttf"),
    ("ZCOOL QingKe HuangYou", "ZCOOLQingKeHuangYou-Regular.ttf", "400", f"{GF}/zcoolqingkehuangyou/ZCOOLQingKeHuangYou-Regular.ttf"),
    ("ZCOOL KuaiLe", "ZCOOLKuaiLe-Regular.ttf", "400", f"{GF}/zcoolkuaile/ZCOOLKuaiLe-Regular.ttf"),
    ("ZCOOL XiaoWei", "ZCOOLXiaoWei-Regular.ttf", "400", f"{GF}/zcoolxiaowei/ZCOOLXiaoWei-Regular.ttf"),
    ("Ma Shan Zheng", "MaShanZheng-Regular.ttf", "400", f"{GF}/mashanzheng/MaShanZheng-Regular.ttf"),
    ("IBM Plex Mono", "IBMPlexMono-Medium.ttf", "500", f"{GF}/ibmplexmono/IBMPlexMono-Medium.ttf"),
]

# JIZURA 的 27 套配色（原名是日文）
PALETTES = {
    "noir": "黑色色偏", "crimson": "深红信号", "caution": "警示黄", "magenta": "波普洋红", "paper": "纸与墨",
    "hud": "暗色 HUD", "mint": "薄荷终端", "specimen": "字体样本", "transit": "交通标识", "blueprint": "蓝图",
    "rouge": "胭脂渐变", "mono": "单色 RGB", "sakura": "樱花", "ocean": "深海", "sunset": "晚霞渐变",
    "forest": "森林手账", "vapor": "蒸汽波", "newsprint": "报纸", "synth80": "80 年代合成器", "kraft": "牛皮纸",
    "candy": "糖果", "acid": "酸性", "sumi": "墨与朱", "gold": "金夜",
    "hrRuin": "废墟（恐怖）", "hrNightRec": "深夜录像（恐怖）", "hrCurse": "诅咒信（恐怖）",
}

# 我们的四套主题：从 JIZURA 部件库里挑出来的白名单（键名以 * 结尾表示前缀匹配），配色候选和整体参数
THEMES: dict[str, dict] = {
    "叠字海报": {
        "desc": "同一句歌词放大成描边 / 残影 / 出框巨字做背景，前景字重叠压在上面，色彩对比强",
        "palettes": ["mono", "noir", "sunset", "ocean", "gold", "synth80"],
        "koma": 12,
        "fx": {"chroma": 0.35, "texture": 0.4},
        "allow": {
            "layout": ["huge", "stack", "depthStack", "tyCropGiant", "tyKeySplit", "zoomRepeat", "columnsBig",
                       "kanjiFocus", "poster", "halftoneBig", "sliceStack", "mixed", "center"],
            "treat": ["echoOutline", "shadowStack", "outline", "doubleOutline", "outlineOffset", "longShadow",
                      "hardShadow", "none"],
            "enter": ["zoomOut", "echoIn", "trackIn", "slideL", "slideR", "riseMask", "stretch", "assemble",
                      "knWordSlam", "blurStagger"],
            "hold": ["still", "drift", "zoomSlow", "echoTrail", "trackBreathe", "breathe"],
            "exit": ["cut", "trackOutWide", "echoOut", "blur", "zoomThrough", "zoomFar", "slideOutL", "slideOutR", "riseOut"],
            "decor": ["brackets", "cropMarks", "guides", "watermarkKanji", "lineBurst", "slash"],
            "cam": ["push", "pullOut", "dollyIn", "stepZoom", "beatPunch", "driftDiag"],
            "trans": ["pushSlide", "zoomThrough", "wipe"],
        },
    },
    "清新抒情": {
        "desc": "安静、留白、柔和的入场和出场，适合抒情慢歌",
        "palettes": ["ocean", "sakura", "sumi", "gold", "paper", "specimen"],
        "koma": 0,  # 不做一拍两的顿挫感，动作连续
        "fx": {"chroma": 0.1, "texture": 0.3},
        "allow": {
            "layout": ["center", "vcols", "credits", "quote", "lowerThird", "halfVertical", "tyRuby",
                       "kanjiFocus", "columnsBig", "tyBaseline", "depthStack"],
            "treat": ["none", "softShadow", "glow", "fadeChars", "focusPull", "emphasisDots", "underline", "echoOutline"],
            "enter": ["blur", "blurStagger", "fadeStagger", "trackIn", "riseMask", "inkBleed", "brushReveal",
                      "backlight", "glint", "strokeOrder", "lightLeak"],
            "hold": ["still", "drift", "breathe", "float", "zoomSlow", "glowFlicker", "focusRack", "shimmer"],
            "exit": ["blur", "drift", "blurOutStagger", "dissolve", "sinkMask", "riseOut", "trackOutWide", "hazeOut"],
            "decor": ["petals", "fireflies", "starField", "bokeh", "twinkle", "moonPhases", "snow"],
            "cam": ["push", "focusIn", "floatNoise", "driftDiag", "dollyIn", "pullOut"],
            "trans": ["irisOpen", "pushSlide", "uncover"],
        },
    },
    "动感卡点": {
        "desc": "逐词砸屏、跟着鼓点切换，快节奏",
        "palettes": ["caution", "crimson", "magenta", "synth80", "acid", "blueprint"],
        "koma": 12,
        "fx": {"chroma": 0.6, "texture": 0.5},
        "allow": {
            "layout": ["kn*", "ty*", "huge", "stack", "zoomRepeat", "burst", "slotMachine", "flipBoard", "crossBands"],
            "enter": ["kn*", "ty*", "zoomOut", "slideL", "slideR", "stretch", "glitchIn", "whip", "bounceBig", "stamp"],
            "exit": ["kn*", "ty*", "cut", "explode", "slice", "glitch", "zoomThrough", "whipOut", "shatterLite"],
            "cam": ["kn*", "beatPunch", "crashZoom", "snapPan", "whipIn", "stepZoom", "shakeHard"],
        },
    },
    "日系文字PV": {
        "desc": "JIZURA 原汁原味：全部部件随机组合（不含恐怖部件）",
        "palettes": ["noir", "paper", "hud", "specimen", "blueprint", "rouge"],
        "koma": 12,
        "fx": {},
        "allow": {},
    },
}

# 在 JIZURA 之后加载的我们自己的脚本：中文分镜、按主题组装项目、逐帧输出
HARNESS_JS = r"""
(() => {
const SUFFIX = new Set('的了着过地得啊呀吧呢吗嘛哦哟们么喔哇'.split(''));
const PUNCT = /^[\s，。、！？；：,.!?;:…—~～「」『』“”"'（）()]+$/;
const HAN = /[㐀-鿿]/;
let seg = null;
try { seg = new Intl.Segmenter('zh-Hans', { granularity: 'word' }); } catch (e) {}
const words = s => seg ? [...seg.segment(s)].map(x => x.segment) : [...s];
const len = s => [...s].length;

/* 中文分镜。浏览器自带的中文分词词库很小（「天际」「笑靥」都会切成单字），所以：
   连续单字两两成词（中文以双字词为主）；「与 / 和」连接左右（海与山、风和月）；
   助词语气词向前黏（仲夏的、你看啊）；落单的字向后黏（在没有、甜咖啡）；关键词整体不拆。 */
const CONJ = new Set('与和跟及同'.split(''));
function pairRun(run) {
  const out = []; let i = 0;
  while (i < run.length) {
    const c = run[i];
    if (CONJ.has(c) && out.length && i + 1 < run.length) { out[out.length - 1] += c + run[i + 1]; i += 2; continue; }
    if (i + 1 < run.length && !CONJ.has(run[i + 1])) { out.push(c + run[i + 1]); i += 2; continue; }
    out.push(c); i += 1;
  }
  return out;
}
function chunkZh(parts) {                   // parts: [[text, isKey], ...]
  const units = [];
  let run = [], carry = '';
  const flush = (beforeWord) => {
    const toks = pairRun(run); run = [];
    for (const tk of toks) units.push({ s: tk, key: false });
    // 落单的一个字：后面紧跟一个词就黏过去，否则黏回前面
    const last = units[units.length - 1];
    if (last && !last.key && len(last.s) === 1 && toks.length) {
      if (beforeWord) { carry = units.pop().s; }
      else if (units.length > 1 && !units[units.length - 2].key) { units.pop(); units[units.length - 1].s += last.s; }
    }
  };
  for (const [text, key] of parts) {
    if (key) { flush(false); units.push({ s: carry + text, key: true }); carry = ''; continue; }
    for (const w of words(text)) {
      if (PUNCT.test(w)) { flush(false); if (units.length) units[units.length - 1].s += w.trim(); continue; }
      if (len(w) === 1 && SUFFIX.has(w) && (units.length || run.length)) {
        flush(false);
        if (units.length) units[units.length - 1].s += w; else run.push(w);
        continue;
      }
      if (len(w) === 1) { run.push(w); continue; }
      flush(true);
      units.push({ s: carry + w, key: false }); carry = '';
    }
  }
  flush(false);
  if (carry) units.push({ s: carry, key: false });
  // 「……的」和后面的词合起来不超过 4 字时并成一段（你的世界）；相邻两个短段合起来不超过 3 字也并起来
  const out = [];
  for (const u of units) {
    const last = out[out.length - 1];
    const mergeDe = last && !last.key && !u.key && /的$/.test(last.s) && len(last.s) + len(u.s) <= 4;
    const mergeShort = last && !last.key && !u.key && len(last.s) + len(u.s) <= 3;
    if (mergeDe || mergeShort) last.s += u.s; else out.push({ ...u });
  }
  return out;
}

function lrcTime(t) { const m = Math.floor(t / 60), s = t - m * 60; return '[' + String(m).padStart(2, '0') + ':' + s.toFixed(2).padStart(5, '0') + ']'; }

/* 我们的歌词行 → JIZURA 歌词语法：[mm:ss.xx]块1/块2/*关键词*!   */
function buildLyrics(lines, chorusHit) {
  return lines.map(ln => {
    const clean = s => s.replace(/[*|!！]/g, ' ').replace(/\s+/g, ' ').trim();
    const parts = []; const re = /\{([^}]+)\}|([^{]+)/g; let m;
    while ((m = re.exec(ln.raw))) parts.push([clean(m[1] || m[2]), !!m[1]]);
    const plain = parts.map(p => p[0]).join('');
    let body;
    if (plain.includes('/')) {                // 用户手动断点优先
      body = parts.map(([s, k]) => k ? '*' + s.replace(/\//g, '') + '*' : s).join('').replace(/\/+/g, '/').replace(/^\/|\/$/g, '');
    } else if (HAN.test(plain)) {
      const ch = chunkZh(parts.filter(p => p[0]));
      body = ch.map(u => u.key ? '*' + u.s + '*' : u.s).join(ch.length > 1 ? '/' : '');
    } else {
      body = parts.map(([s, k]) => k ? '*' + s + '*' : s).join('');
    }
    return lrcTime(ln.start) + body + (ln.chorus && chorusHit ? '!' : '');
  }).join('\n');
}

function enabledFrom(allow) {
  const en = {};
  for (const g of J.GROUP_KEYS) {
    const all = J.order(g);
    const pats = allow[g];
    let keep = null;
    if (pats && pats.length) {
      const hit = (k, p) => p.endsWith('*') ? k.startsWith(p.slice(0, -1)) && /[A-Z]/.test(k[p.length - 1] || '') : k === p;
      keep = new Set(all.filter(k => pats.some(p => hit(k, p))));
      if (!keep.size) keep = null;            // 白名单里的部件都不存在（JIZURA 升级改名）时退回全部
    }
    en[g] = Object.fromEntries(all.map(k => [k, !k.startsWith('hr') && (!keep || keep.has(k))]));
  }
  return en;
}

const origChunk = J.chunkText;
J.chunkText = (text) => {                   // 版式内部按词拆分（对话气泡、逐词入场等）也用中文规则
  if (!HAN.test(text || '')) return origChunk(text);
  const units = chunkZh([[text, false]]).map(u => u.s);
  return units.length ? units : origChunk(text);
};
const origSplit = J.splitLines;
J.splitLines = (text, maxPer) => {
  const arr = [...text];
  if (arr.length <= maxPer || !HAN.test(text) || text.includes('\n')) return origSplit(text, maxPer);
  const units = chunkZh([[text, false]]).map(u => u.s);
  if (units.join('') !== text) return origSplit(text, maxPer);
  const bounds = new Set(); let pos = 0;
  for (const u of units) { pos += len(u); bounds.add(pos); }
  const nLines = Math.ceil(arr.length / maxPer), per = arr.length / nLines, out = []; let start = 0;
  for (let l = 1; l < nLines; l++) {
    const target = Math.round(per * l); let best = target, bestScore = -1e9;
    for (let k = Math.max(start + 1, target - 3); k <= Math.min(arr.length - 1, target + 3); k++) {
      const s = 3 - Math.abs(k - target) + (bounds.has(k) ? 6 : -6) + (J.isPunct(arr[k - 1]) ? 5 : 0);
      if (s > bestScore) { bestScore = s; best = k; }
    }
    out.push(arr.slice(start, best).join('').trim()); start = best;
  }
  out.push(arr.slice(start).join('').trim());
  return out.join('\n');
};

/* 对比度：我们的歌词层是叠在自己的背景图上的，JIZURA 不知道底下有多亮。
   按每段背景的亮度挑配色，并把对比度不够的镜头换成同一配色里对比度最高的方案。 */
const relLum = hex => {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim()); if (!m) return 0.5;
  const v = [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16) / 255).map(c => c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
};
const contrast = (fg, bgLum) => { const a = relLum(fg), b = bgLum; return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
const lumAt = (segs, t) => { let L = segs.length ? segs[0][1] : 0.05; for (const [t0, l] of segs) if (t >= t0) L = l; return L; };
function pickPalette(cands, segs, seed) {
  const score = k => { const st = J.STYLES[k]; if (!st) return -1;
    return segs.reduce((a, [, L]) => a + Math.max(...st.schemes.map(s => contrast(s.fg, L))), 0) / Math.max(1, segs.length); };
  const scored = cands.map(k => [k, score(k)]).filter(x => x[1] > 0).sort((a, b) => b[1] - a[1]);
  if (!scored.length) return cands[0];
  const good = scored.filter(x => x[1] >= Math.min(4.5, scored[0][1] * 0.85));
  return good[seed % good.length][0];
}
function fixSchemes(plan, segs) {
  const sch = plan.style.schemes; let fixed = 0;
  for (const c of plan.cuts) {
    const L = lumAt(segs, c.start), cur = sch[c.scheme % sch.length];
    if (contrast(cur.fg, L) >= 3) continue;
    let best = c.scheme, bc = -1;
    sch.forEach((s, i) => { const v = contrast(s.fg, L); if (v > bc) { bc = v; best = i; } });
    if (best !== c.scheme) { c.scheme = best; fixed++; }
  }
  return fixed;
}

window.CS = {
  chunkZh: (text) => chunkZh([[text, false]]).map(u => u.s),
  buildLyrics,
  async setup(a) {
    const p = Object.assign(J.defaultProject(), {
      lyrics: buildLyrics(a.lines, a.chorusHit), aspect: a.aspect, res: a.res, fps: a.fps, lang: 'zh-Hans',
      style: a.palette === 'auto' ? pickPalette(a.palettes, a.bgLums, a.seed) : a.palette, seed: a.seed, extra: true, wa: true, typo: true, kinetic: true, horror: false,
      unify: a.unify, typeset: false,
    });
    p.fx = Object.assign(J.defaultProject().fx, a.fx);
    p.enabled = enabledFrom(a.allow);
    p.timing = Object.assign(p.timing, { offset: 0, snap: !!a.audio, tail: 0.8 });
    let audio = null;
    if (a.audio) {
      const bin = Uint8Array.from(atob(a.audio), c => c.charCodeAt(0));
      const r = await J.analyzeAudio(new Blob([bin]));
      audio = { beats: r.beats, duration: r.duration, energy: r.energy, energyRate: r.energyRate, bpm: r.bpm };
    }
    window.PROJ = p;
    window.PLAN = J.plan(p, audio);
    const fixedSchemes = fixSchemes(PLAN, a.bgLums);
    await J.ensureFonts(p.lyrics.replace(/\[[^\]]*\]/g, '') + '0123456789', J.fontsOfPlan(PLAN));
    window.R = new J.Renderer();
    const c = document.getElementById('c');
    c.width = Math.round(PLAN.W * a.scale); c.height = Math.round(PLAN.H * a.scale);
    window.CTX = c.getContext('2d');
    return { W: PLAN.W, H: PLAN.H, duration: PLAN.duration, lyrics: p.lyrics, bpm: audio ? audio.bpm : null,
             palette: p.style, fixedSchemes,
             cuts: PLAN.cuts.map(c => ({ text: c.text, start: c.start, end: c.end, layout: c.layout, enter: c.enter,
                                         exit: c.exit, treat: c.treat })) };
  },
  frames(ts, scale) {
    return ts.map(t => { R.frame(CTX, PLAN, t, { scale, transparent: true }); return CTX.canvas.toDataURL('image/png'); });
  },
};
})();
"""


def ensure_fonts(download: bool = True) -> list[str]:
    """检查（必要时下载）中文字体，返回仍然缺失的字体族名。"""
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    missing = []
    for family, fname, _, url in FONTS:
        f = FONT_DIR / fname
        if f.exists() and f.stat().st_size > 1000:
            continue
        if download:
            try:
                tmp = f.with_suffix(".part")
                with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as out:
                    shutil.copyfileobj(r, out)
                tmp.replace(f)
                continue
            except Exception:
                pass
        missing.append(family)
    return missing


def harness() -> Path:
    """把 JIZURA 源码按文件名顺序拼成一个脚本（和上游 build.py 一致），连同字体和我们的脚本写成渲染页。"""
    sources = sorted(SRC_DIR.glob("*.js"))
    if not sources:
        raise RenderError(f"找不到 JIZURA 源码：{SRC_DIR}")
    bundle = "\n".join(p.read_text("utf-8") for p in sources)
    css = "\n".join(f"@font-face{{font-family:'{fam}';src:url('{(FONT_DIR / f).as_uri()}');font-weight:{w};}}"
                    for fam, f, w, _ in FONTS if (FONT_DIR / f).exists())
    key = hashlib.sha1((bundle + HARNESS_JS + css).encode()).hexdigest()[:12]
    d = DATA_ROOT / "jizura" / key
    page = d / "index.html"
    if not page.exists():
        d.mkdir(parents=True, exist_ok=True)
        (d / "jizura.js").write_text(bundle, "utf-8")
        (d / "cs.js").write_text(HARNESS_JS, "utf-8")
        page.write_text("<!doctype html><meta charset=utf-8>"
                        f"<style>{css}</style><canvas id=c></canvas>"
                        "<script src='jizura.js'></script><script src='cs.js'></script>", "utf-8")
    return page


def luminance(img: Path) -> float:
    """背景图中间区域（歌词通常所在的位置）的平均相对亮度 0–1。"""
    import numpy as np
    from PIL import Image

    im = Image.open(img).convert("RGB")
    w, h = im.size
    a = np.asarray(im.crop((int(w * 0.1), int(h * 0.2), int(w * 0.9), int(h * 0.8))).resize((64, 96)), float) / 255
    lin = np.where(a <= 0.03928, a / 12.92, ((a + 0.055) / 1.055) ** 2.4).mean(axis=(0, 1))
    return float(0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2])


def project_args(lines: list[Line], cfg: Jizura, scale: float, fps: int, audio: Path | None,
                 backgrounds: list[Path] | None = None) -> dict:
    theme = THEMES[cfg.theme]
    palette = cfg.palette if cfg.palette in PALETTES else "auto"
    bgs = backgrounds or []
    # 没有背景图（例如只出透明字幕层）时按深色背景处理
    bg_lums = [[t0, luminance(b)] for t0, b in zip(background_schedule(lines, bgs), bgs)] or [[0.0, 0.05]]
    fx = {"motion": cfg.motion, "glitch": cfg.glitch, "decor": cfg.decor, "density": cfg.density,
          "flash": cfg.flash, "koma": theme["koma"], "onTwos": theme["koma"] > 0, "hud": "off",
          "hideNo": True, "hideTime": True} | theme["fx"]
    return {
        "lines": [{"start": ln.start, "raw": ln.raw, "chorus": ln.chorus} for ln in lines],
        "chorusHit": cfg.chorus_hit, "aspect": "9:16", "res": 1080, "fps": fps, "palette": palette,
        "palettes": theme["palettes"], "bgLums": bg_lums,
        "seed": cfg.seed, "fx": fx, "allow": theme["allow"], "scale": scale, "unify": theme.get("unify", True),
        "audio": base64.b64encode(audio.read_bytes()).decode() if audio else None,
    }


class _Page:
    """一个加载好 JIZURA 的无头浏览器页面。"""

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RenderError("需要安装 Playwright：pip install playwright && playwright install chromium") from e
        self._pw = await async_playwright().start()
        try:
            self.browser = await self._pw.chromium.launch()
        except Exception as e:
            await self._pw.stop()
            raise RenderError(f"无法启动 Chromium（运行 playwright install chromium）：{e}") from e
        self.page = await self.browser.new_page()
        errors: list[str] = []
        self.page.cs_errors = errors
        self.page.on("pageerror", lambda e: errors.append(str(e)))
        await self.page.route("**/fonts.googleapis.com/**", lambda r: r.abort())  # 字体已在本地
        await self.page.goto(harness().as_uri())
        return self.page

    async def __aexit__(self, *exc):
        await self.browser.close()
        await self._pw.stop()


async def plan(lines: list[Line], cfg: Jizura) -> dict:
    """只编排不渲染：返回分镜列表（用于界面展示和测试）。"""
    async with _Page() as page:
        return await page.evaluate("a => CS.setup(a)", project_args(lines, cfg, 0.1, 30, None))


async def render(lines: list[Line], cfg: Jizura, out: Path, backgrounds: list[Path] | None = None,
                 audio: Path | None = None, scale: float = 1.0, fps: int = 30, overlay: bool = False,
                 batch: int = 6) -> tuple[Path, dict]:
    """逐帧渲染透明歌词层 → ffmpeg。返回（输出文件，编排信息）。"""
    if not lines:
        raise RenderError("没有可渲染的歌词行")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RenderError("找不到 ffmpeg")
    ensure_fonts(download=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    async with _Page() as page:
        info = await page.evaluate("a => CS.setup(a)", project_args(lines, cfg, scale, fps, audio, backgrounds))
        w, h = round(info["W"] * scale) // 2 * 2, round(info["H"] * scale) // 2 * 2
        duration = info["duration"]
        koma = THEMES[cfg.theme]["koma"]
        step = 1 / koma if koma > 0 else 1 / fps  # 一拍两（每秒 12 张）时只渲染不重复的画面，由 ffmpeg 补帧
        times = [i * step for i in range(math.ceil(duration / step))]
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "image2pipe", "-framerate", f"{1 / step:.6f}", "-c:v", "png", "-i", "-"]
        fg = f"[0:v]scale={w}:{h},fps={fps},format=rgba[fg]"
        if overlay:
            out = out.with_suffix(".mov")
            cmd += ["-filter_complex", fg, "-map", "[fg]", "-c:v", "prores_ks", "-profile:v", "4444",
                    "-pix_fmt", "yuva444p10le"]
            ai = 1
        else:
            out = out.with_suffix(".mp4")
            cmd += background_input(lines, backgrounds or [], duration, w, h, fps, out)
            cmd += ["-filter_complex", f"[1:v]{fit_filter(w, h, fps)}[bg];{fg};[bg][fg]overlay=format=auto,format=yuv420p[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
            ai = 2
        if audio:
            cmd[cmd.index("-filter_complex"):cmd.index("-filter_complex")] = ["-i", str(audio)]
            cmd += ["-map", f"{ai}:a", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-t", f"{duration:.2f}", str(out)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            for i in range(0, len(times), batch):
                urls = await page.evaluate("([ts, s]) => CS.frames(ts, s)", [times[i:i + batch], scale])
                for u in urls:
                    proc.stdin.write(base64.b64decode(u.split(",", 1)[1]))
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        err = (await proc.stderr.read()).decode(errors="replace")
        if await proc.wait() != 0:
            raise RenderError("ffmpeg 失败：" + err[-800:])
        if page.cs_errors:
            info["warnings"] = page.cs_errors[:5]
    info["frames"] = len(times)
    (out.with_suffix(".plan.json")).write_text(json.dumps(info, ensure_ascii=False, indent=1), "utf-8")
    return out, info


def preview_times(info: dict, n: int = 3) -> list[float]:
    """样张取帧：均匀挑几个镜头，各取其显示过半的时刻。"""
    cuts = [c for c in info["cuts"] if c["end"] - c["start"] > 0.3]
    if not cuts:
        return [info["duration"] / 2]
    idx = sorted({round(k * (len(cuts) - 1) / max(1, n - 1)) for k in range(n)})
    return [cuts[i]["start"] + (cuts[i]["end"] - cuts[i]["start"]) * 0.55 for i in idx]
