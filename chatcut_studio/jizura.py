"""JIZURA 动态排版引擎的融合层。

JIZURA（https://github.com/852wa/JIZURA ，MIT）是浏览器里的歌词 PV 生成器：600 多个版式 / 入场 / 出场 /
文字处理 / 装饰部件，按随机种子编排。源码原样放在 third_party/jizura/src，这里不改它，只在外面做四件事：

1. 渲染：在无头 Chromium 里加载 JIZURA，逐帧调用 Renderer.frame() 画「透明背景」的歌词层，
   PNG 流交给 ffmpeg，叠到我们的背景图上、合上音频，或直接输出透明 .mov 给 ChatCut。
2. 中文：JIZURA 的分镜器按日文规则写，会把中文拆在词中间（「笑|靥」）。我们先用浏览器自带的中文分词，
   按「单字修饰词向后黏、助词语气词向前黏」切好，用 JIZURA 的手动分镜语法（a/b/c）交给它。
3. 主题：从 JIZURA 部件库里挑选组合成 4 套中文主题（宋韵留白、水墨书法、叠字国潮、清新诗意），
   默认关掉恐怖部件和「No.04 / 00:12.34」这类装饰性编号。
5. 中文审美：注册 5 套中国传统色配色，把卡通字体换成宋体 / 文楷 / 毛笔，部件只留符合中文排版的。
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
    ("LXGW WenKai", "LXGWWenKai-Regular.ttf", "400",
     "https://github.com/lxgw/LxgwWenKai/releases/latest/download/LXGWWenKai-Regular.ttf"),
    ("IBM Plex Mono", "IBMPlexMono-Medium.ttf", "500", f"{GF}/ibmplexmono/IBMPlexMono-Medium.ttf"),
]

# 中国传统色配色（我们注册进 JIZURA 的，见 HARNESS_JS 里的 CN_STYLES）+ JIZURA 自带的 27 套（原名是日文）
CN_PALETTES = {"cn_ink": "墨与朱砂", "cn_gold": "夜色鎏金", "cn_qinglv": "青绿山水", "cn_moon": "月白黛蓝", "cn_rouge": "胭脂"}
PALETTES = {**CN_PALETTES,
    "noir": "黑色色偏", "crimson": "深红信号", "caution": "警示黄", "magenta": "波普洋红", "paper": "纸与墨",
    "hud": "暗色 HUD", "mint": "薄荷终端", "specimen": "字体样本", "transit": "交通标识", "blueprint": "蓝图",
    "rouge": "胭脂渐变", "mono": "单色 RGB", "sakura": "樱花", "ocean": "深海", "sunset": "晚霞渐变",
    "forest": "森林手账", "vapor": "蒸汽波", "newsprint": "报纸", "synth80": "80 年代合成器", "kraft": "牛皮纸",
    "candy": "糖果", "acid": "酸性", "sumi": "墨与朱", "gold": "金夜",
    "hrRuin": "废墟（恐怖）", "hrNightRec": "深夜录像（恐怖）", "hrCurse": "诅咒信（恐怖）",
}

# 四套中文主题：只用中国传统色配色和雅致的中文字体，部件只留符合中文排版的（竖排、大小字错落、一字强调、
# 留白、印章、水印大字、墨迹晕开……），去掉票根、邮票、HUD、英文标签、卡通字体这类日式 / 西式元素
THEMES: dict[str, dict] = {
    "宋韵留白": {
        "desc": "宋体大小字错落、大面积留白、竖排小字与朱砂印章点缀，安静克制",
        "palettes": ["cn_ink", "cn_moon", "cn_gold"],
        "koma": 0,
        "fx": {"chroma": 0.05, "texture": 0.3},
        "allow": {
            "layout": ["cnPoem", "cnStagger", "cnFrame", "center", "columnsBig", "halfVertical", "tyBaseline", "quote"],
            "treat": ["none", "softShadow", "emphasisDots", "fadeChars"],
            "enter": ["fadeStagger", "blurStagger", "trackIn", "riseMask", "blur", "glint"],
            "hold": ["still", "drift", "breathe", "zoomSlow", "focusRack"],
            "exit": ["blur", "drift", "blurOutStagger", "dissolve", "trackOutWide", "riseOut"],
            "decor": ["seal", "verticalStrip"],
            "cam": ["push", "dollyIn", "focusIn", "pullOut"],
            "trans": ["pushSlide", "uncover"],
        },
    },
    "水墨书法": {
        "desc": "毛笔字、墨迹晕开与笔画书写入场、印章落款，水墨意境",
        "palettes": ["cn_ink", "cn_qinglv"],
        "koma": 0,
        "fx": {"chroma": 0.0, "texture": 0.4},
        "allow": {
            "layout": ["cnPoem", "cnMoon", "cnFrame", "columnsBig", "center", "halfVertical"],
            "treat": ["none", "softShadow", "fadeChars"],
            "enter": ["inkBleed", "brushReveal", "strokeOrder", "inkDrop", "strokeDraw", "blur", "fadeStagger"],
            "hold": ["still", "drift", "breathe", "zoomSlow"],
            "exit": ["dissolve", "drift", "blurOutStagger", "undraw", "blur"],
            "decor": ["seal"],
            "cam": ["push", "dollyIn", "pullOut", "driftDiag"],
            "trans": ["inkBlob", "pushSlide"],
        },
    },
    "叠字国潮": {
        "desc": "一个关键字放大做淡淡的背景，整句压在上面；鎏金 / 胭脂 / 朱砂配色，新中式海报感",
        "palettes": ["cn_gold", "cn_rouge", "cn_ink"],
        "koma": 12,
        "fx": {"chroma": 0.2, "texture": 0.3},
        "allow": {
            "layout": ["cnEcho", "cnStagger", "cnFrame", "center"],
            "treat": ["none", "softShadow"],
            "enter": ["zoomOut", "trackIn", "riseMask", "slideL", "slideR"],
            "hold": ["still", "drift", "zoomSlow", "breathe"],
            "exit": ["cut", "trackOutWide", "zoomFar", "blur"],
            "decor": ["seal"],
            "cam": ["push", "pullOut", "dollyIn"],
            "trans": ["pushSlide"],
        },
    },
    "清新诗意": {
        "desc": "文楷手写感、柔光与花瓣萤火点缀，像一首小诗，适合清新抒情歌",
        "palettes": ["cn_moon", "cn_qinglv", "cn_rouge"],
        "koma": 0,
        "fx": {"chroma": 0.05, "texture": 0.2},
        "allow": {
            "layout": ["cnPoem", "cnMoon", "cnStagger", "center", "halfVertical", "quote", "columnsBig"],
            "treat": ["none", "softShadow", "glow", "fadeChars"],
            "enter": ["blur", "fadeStagger", "blurStagger", "backlight", "glint", "riseMask", "trackIn"],
            "hold": ["still", "drift", "float", "breathe", "glowFlicker"],
            "exit": ["blur", "drift", "blurOutStagger", "dissolve", "hazeOut", "riseOut"],
            "decor": ["petals", "fireflies", "moonPhases", "twinkle"],
            "cam": ["push", "floatNoise", "focusIn", "driftDiag"],
            "trans": ["irisOpen", "pushSlide", "uncover"],
        },
    },
}
# 旧版本存下来的主题名 → 新主题
THEME_ALIASES = {"叠字海报": "叠字国潮", "清新抒情": "清新诗意", "动感卡点": "叠字国潮", "日系文字PV": "宋韵留白"}

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

/* ---------- 中文审美层 ---------- */
// 字体：中文版原本把日文展示字体映射成站酷快乐体 / 庆科黄油体这类卡通字体，统一换成雅致的中文字体
(() => {
  const Z = J.LANG_FACES['zh-Hans'].map;
  const serif900 = Object.assign({}, Z.mincho_black), wenkai = { family: '"LXGW WenKai"', weight: 400, gf: null };
  Object.assign(Z, { dela: serif900, reggae: serif900, rampart: serif900, potta: Object.assign({}, Z.brush),
                     round: wenkai, pop: wenkai, kiwi: wenkai, klee: wenkai });
})();
// 新中式版式：用 JIZURA 已有的部件（竖排、印章、细线、残影、圆、版框）组合出来的 5 个版式，
// 原则是一屏一个视觉焦点、留白多、装饰只用细线和小印章
(() => {
  const E = J.E, PK = 'cn';
  const fo = (st, roles) => J.fontsOf(st, roles);
  const strip = t => String(t || '').replace(/\s+/g, '');
  const tin = (env, d = 0, len = 0.6) => E.outCubic(J.clamp((env.lt - d) / len));
  const tout = env => 1 - E.inCubic(env.pOut);
  const hair = env => Math.max(1, Math.min(env.W, env.H) * 0.0018);
  const meas = (text, font, size, o) => J.measure(Object.assign({ text, font, size }, o || {}));
  const SEAL_INK = '#F8F1E4';
  const union = (a, b) => !a ? b : !b ? a : { x0: Math.min(a.x0, b.x0), y0: Math.min(a.y0, b.y0), x1: Math.max(a.x1, b.x1), y1: Math.max(a.y1, b.y1),
    cx: (Math.min(a.x0, b.x0) + Math.max(a.x1, b.x1)) / 2, cy: (Math.min(a.y0, b.y0) + Math.max(a.y1, b.y1)) / 2, boxes: [] };
  const firstHan = t => [...t].find(c => J.isKanji(c) || /[㐀-鿿]/.test(c)) || [...t][0] || '';
  // 朱砂小印：方形印底 + 反白字，盖下去时略微放大再落定
  function seal(env, ch, x, y, s, a, font) {
    if (a <= 0.01 || !ch) return;
    const k = 1 + 0.25 * (1 - E.outBack(J.clamp(a), 2));
    env.rect(x - s * k / 2, y - s * k / 2, s * k, s * k, env.sc.accent, a * 0.92, false);
    env.draw({ text: ch, font, size: s * k * 0.66, x, y, color: SEAL_INK, alpha: a, ghost: false });
  }
  // 按词把一句分成 n 段（竖排分列 / 错落分组用）
  const splitN = (text, n) => n <= 1 ? [text] : J.splitLines(text, Math.ceil([...text].length / n)).split('\n');
  const reg = (key, def) => J.register('layout', key, Object.assign({ w: 1.6, portrait: 1.6, tags: ['calm', 'editorial'] }, def), PK);

  /* 诗笺：竖排正文（多列从右往左）+ 一侧发丝线 + 列尾朱印 */
  reg('cnPoem', {
    name: '诗笺', fits: n => n >= 2 && n <= 16,
    plan: (rng, cut, st) => ({ font: rng.pick(fo(st, ['display', 'serif'])), side: rng.pick([1, -1]), y: rng.range(0.4, 0.46), seal: rng.chance(0.75) }),
    render(env) {
      const { W, H, sc } = env, P = env.cut.params, text = strip(env.cut.text), n = [...text].length;
      const cols = splitN(text, n > 14 ? 3 : n > 7 ? 2 : 1), per = Math.max(...cols.map(c => [...c].length));
      const size = Math.min(H * 0.5 / per, W * (cols.length > 1 ? 0.12 : 0.15));
      const gap = size * 1.55, cx = W * (0.5 + P.side * 0.1), cy = H * P.y, colH = per * size * 1.04;
      let bb = null;
      cols.forEach((c, i) => {   // 第一列在最右
        const x = cx + ((cols.length - 1) / 2 - i) * gap;
        const top = cy - colH / 2, h = [...c].length * size * 1.04;
        bb = union(bb, J.mainDraw(env, { text: c, font: P.font, size, x, y: top + h / 2, vertical: true, track: 0.04, color: sc.fg, mi: i * 3 }));
      });
      const e = tin(env, 0.1, 0.9) * tout(env);
      const lx = cx - P.side * ((cols.length - 1) / 2 * gap + size * 0.95), y0 = cy - colH / 2 - size * 0.4;
      env.line([[lx, y0], [lx, y0 + (colH + size * 1.4) * e]], sc.sub, hair(env), 0.55, false);
      if (P.seal) seal(env, firstHan(text), cx - P.side * ((cols.length - 1) / 2 * gap) , cy + colH / 2 + size * 0.75, size * 0.48, tin(env, 0.45, 0.35) * tout(env), P.font);
      return bb;
    },
  });

  /* 错落：第一个词放大、靠左，其余小字右对齐落在下方，中间一条朱色短线相连 */
  reg('cnStagger', {
    name: '错落', fits: n => n >= 3 && n <= 14,
    plan: (rng, cut, st) => ({ big: rng.pick(fo(st, ['display'])), small: rng.pick(fo(st, ['serif', 'body'])), y: rng.range(0.38, 0.5), flip: rng.chance(0.35) }),
    render(env) {
      const { W, H, sc } = env, P = env.cut.params, text = strip(env.cut.text);
      let units = J.chunkText(text).map(strip).filter(Boolean);
      if (units.length < 2) { const a = [...text]; const k = Math.ceil(a.length / 2); units = [a.slice(0, k).join(''), a.slice(k).join('')]; }
      const head = units[0], rest = units.slice(1).join('');
      const bs = Math.min(W * 0.8 / [...head].length, W * 0.3), ss = Math.min(bs * 0.36, W * 0.82 / Math.max(1, [...rest].length) / 1.2);
      const bw = meas(head, P.big, bs).w, sw = meas(rest, P.small, ss, { track: 0.18 }).w;
      const L = W * 0.1, R = W * 0.9, by = H * P.y, sy = by + bs * 0.78;
      const bx = P.flip ? R - bw / 2 : L + bw / 2, sx = P.flip ? L + sw / 2 : R - sw / 2;
      let bb = J.mainDraw(env, { text: head, font: P.big, size: bs, x: bx, y: by, color: sc.fg, mi: 0 });
      bb = union(bb, J.mainDraw(env, { text: rest, font: P.small, size: ss, x: sx, y: sy, track: 0.18, color: sc.fg, mi: 4 }));
      const e = tin(env, 0.25, 0.7) * tout(env);
      const a0 = P.flip ? sx + sw / 2 + ss * 0.6 : sx - sw / 2 - ss * 0.6, a1 = P.flip ? R - bw * 0.15 : L + bw * 0.15;
      env.line([[a0, sy], [J.lerp(a0, a1, e), sy]], sc.accent, Math.max(2, ss * 0.06), 0.9, false);
      return bb;
    },
  });

  /* 叠影：同一句放大成淡淡的底纹，正文压在它的下半部分；底纹随时间极慢放大 */
  reg('cnEcho', {
    name: '叠影', fits: n => n >= 2 && n <= 12, emph: 1.4,
    plan: (rng, cut, st) => ({ font: rng.pick(fo(st, ['display'])), up: rng.pick([1, -1]) }),
    render(env) {
      const { W, H, sc } = env, P = env.cut.params, text = strip(env.cut.text), n = [...text].length;
      const back = n > 6 ? J.splitLines(text, Math.ceil(n / 2)) : text;
      const es = J.fitSize(back, P.font, W * 0.94, H * 0.36, { lead: 1.05 }) * (1 + 0.04 * J.clamp(env.lt / Math.max(0.5, env.cut.dur)));
      const front = n > 7 ? J.splitLines(text, Math.ceil(n / 2)) : text;
      const fs = Math.min(es * 0.42, J.fitSize(front, P.font, W * 0.7, H * 0.2, { track: 0.08, lead: 1.25 }));
      const cy = H * 0.45, ea = tin(env, 0, 1.0) * tout(env);
      const bi = { text: back, font: P.font, size: es, x: W / 2, y: cy - P.up * es * 0.18, lead: 1.05, alpha: 0.3 * ea, color: sc.accent, ghost: false };
      env.draw(bi);
      const bb = J.mainDraw(env, { text: front, font: P.font, size: fs, x: W / 2, y: cy + P.up * es * 0.32, track: 0.08, lead: 1.25, color: sc.fg });
      if (bb) { const e = tin(env, 0.3, 0.6) * tout(env), y = bb.y1 + fs * 0.45;
        env.line([[W / 2 - (bb.x1 - bb.x0) * 0.2 * e, y], [W / 2 + (bb.x1 - bb.x0) * 0.2 * e, y]], sc.accent, Math.max(2, fs * 0.05), 0.9, false); }
      return bb;
    },
  });

  /* 窗格：古籍版框式的双线框，四边从角上画出来；正文居中，右下角压一方小印 */
  reg('cnFrame', {
    name: '窗格', fits: n => n >= 2 && n <= 14,
    plan: (rng, cut, st) => ({ font: rng.pick(fo(st, ['serif', 'display'])), vertical: rng.chance(0.4), y: rng.range(0.4, 0.48), seal: rng.chance(0.7) }),
    render(env) {
      const { W, H, sc } = env, P = env.cut.params, text = strip(env.cut.text), n = [...text].length;
      const vert = P.vertical && n <= 8;
      const t = vert ? text : (n > 6 ? J.splitLines(text, Math.ceil(n / 2)) : text);
      const size = vert ? Math.min(H * 0.36 / n, W * 0.11) : Math.min(J.fitSize(t, P.font, W * 0.56, H * 0.2, { track: 0.1, lead: 1.35 }), W * 0.11);
      const m = vert ? { w: size, h: n * size * 1.04 } : meas(t, P.font, size, { track: 0.1, lead: 1.35 });
      const cx = W / 2, cy = H * P.y, pad = size * 0.9;
      const x0 = Math.max(W * 0.07, cx - m.w / 2 - pad), x1 = Math.min(W * 0.93, cx + m.w / 2 + pad), y0 = cy - m.h / 2 - pad, y1 = cy + m.h / 2 + pad;
      const e = E.inOutCubic(J.clamp(env.lt / 0.9)) * tout(env), lw = hair(env), g = size * 0.18;
      const frame = (ax0, ay0, ax1, ay1, w, a) => {
        const hx = (ax1 - ax0) * e, vy = (ay1 - ay0) * e;
        env.line([[ax0, ay0], [ax0 + hx, ay0]], sc.sub, w, a, false); env.line([[ax1, ay1], [ax1 - hx, ay1]], sc.sub, w, a, false);
        env.line([[ax1, ay0], [ax1, ay0 + vy]], sc.sub, w, a, false); env.line([[ax0, ay1], [ax0, ay1 - vy]], sc.sub, w, a, false);
      };
      frame(x0, y0, x1, y1, lw * 2.2, 0.75); frame(x0 + g, y0 + g, x1 - g, y1 - g, lw, 0.5);
      const bb = J.mainDraw(env, { text: t, font: P.font, size, x: cx, y: cy, vertical: vert, track: vert ? 0.04 : 0.1, lead: 1.35, color: sc.fg });
      if (P.seal) seal(env, firstHan(text), x1, y1, size * 0.5, tin(env, 0.7, 0.35) * tout(env), P.font);
      return bb;
    },
  });

  /* 月轮：一轮淡淡的圆（月 / 日），竖排正文贴着圆的一侧，或横排落在圆下 */
  reg('cnMoon', {
    name: '月轮', fits: n => n >= 2 && n <= 12,
    plan: (rng, cut, st) => ({ font: rng.pick(fo(st, ['display', 'serif'])), mode: rng.pick(['v', 'v', 'h']), side: rng.pick([1, -1]), ring: rng.chance(0.5) }),
    render(env) {
      const { W, H, sc } = env, P = env.cut.params, text = strip(env.cut.text), n = [...text].length;
      const R = W * 0.27, mx = W / 2 + (P.mode === 'v' ? -P.side * W * 0.1 : 0), my = H * 0.36;
      const e = tin(env, 0, 1.2) * tout(env);
      env.circle(mx, my, R * (0.96 + 0.04 * e), sc.fg, null, 0, 0.09 * e, false);
      if (P.ring) env.arc(mx, my, R * 1.08, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * E.inOutCubic(J.clamp(env.lt / 1.4)), sc.sub, hair(env), 0.55 * tout(env), false);
      if (P.mode === 'v' && n <= 9) {
        const size = Math.min(H * 0.46 / n, W * 0.14), h = n * size * 1.04;
        return J.mainDraw(env, { text, font: P.font, size, x: mx + P.side * R * 1.05, y: my + R * 0.35 + h * 0.15, vertical: true, track: 0.04, color: sc.fg });
      }
      const t = n > 7 ? J.splitLines(text, Math.ceil(n / 2)) : text;
      const size = Math.min(J.fitSize(t, P.font, W * 0.7, H * 0.16, { track: 0.1, lead: 1.3 }), W * 0.12);
      return J.mainDraw(env, { text: t, font: P.font, size, x: W / 2, y: my + R * 1.55, track: 0.1, lead: 1.3, color: sc.fg });
    },
  });
})();
// 配色：中国传统色。每套两种方案：浅字（暗背景）和深字（亮背景），由对比度自动选择
const hex2 = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const mixHex = (a, b, k) => '#' + hex2(a).map((v, i) => Math.round(v * (1 - k) + hex2(b)[i] * k).toString(16).padStart(2, '0')).join('').toUpperCase();
// 背景巨字、残影用的「暗色」：在文字色和底色之间取一个淡的中间色（原值是给 JIZURA 自己的底色设计的，压在我们的图上会成黑块）
const S = (bg, fg, sub, accent, accent2, dim, ghostA, ghostB) => ({ bg, fg, sub, accent, accent2, ink: fg,
  dim: mixHex(bg, fg, 0.28), ghostA: mixHex(bg, fg, 0.45), ghostB });
const CN_STYLES = {
  cn_ink:    { name: '墨与朱砂', fonts: { display: ['mincho_black', 'brush'], serif: ['mincho_bold', 'mincho_black'], body: ['mincho'], mono: ['mono'] },
               schemes: [S('#15120F', '#F3EDE0', '#B9B0A0', '#C8372D', '#F3EDE0', '#2A2520', '#8C857A', '#C8372D'),
                         S('#EFE6D2', '#1E1A16', '#5E574C', '#B8322A', '#1E1A16', '#E2D7BF', '#9A9284', '#B8322A')] },
  cn_gold:   { name: '夜色鎏金', fonts: { display: ['mincho_black'], serif: ['mincho_bold'], body: ['mincho_light'], mono: ['mono'] },
               schemes: [S('#14100B', '#F6EBD3', '#C8B48A', '#D4A853', '#F6EBD3', '#2A2216', '#8A7550', '#D4A853'),
                         S('#F3E9D2', '#2B2216', '#6B5A3E', '#A67C2E', '#2B2216', '#E6D9BC', '#B39B6E', '#A67C2E')] },
  cn_qinglv: { name: '青绿山水', fonts: { display: ['brush', 'mincho_black'], serif: ['mincho_bold'], body: ['klee'], mono: ['mono'] },
               schemes: [S('#0F1C1C', '#EEF2E8', '#A9C1B5', '#6DB596', '#8EC0D2', '#1A2C2B', '#4E7F73', '#8EC0D2'),
                         S('#EEF0E6', '#1F3B3A', '#4F6B66', '#2E7D6B', '#2E6E8E', '#DDE3D6', '#8FB0A4', '#2E6E8E')] },
  cn_moon:   { name: '月白黛蓝', fonts: { display: ['klee', 'mincho_bold'], serif: ['mincho'], body: ['klee'], mono: ['mono'] },
               schemes: [S('#101722', '#EAF0F5', '#A9B8C8', '#9CC3D5', '#EAF0F5', '#1B2433', '#5B708A', '#9CC3D5'),
                         S('#EEF2F5', '#26324A', '#5A6680', '#4A6E98', '#26324A', '#DDE4EB', '#9AA8BC', '#4A6E98')] },
  cn_rouge:  { name: '胭脂', fonts: { display: ['mincho_black', 'klee'], serif: ['mincho_bold'], body: ['mincho'], mono: ['mono'] },
               schemes: [S('#170D10', '#FBF0EE', '#D9B8B6', '#E2677A', '#F3C9A8', '#2A1519', '#8E4B57', '#E2677A'),
                         S('#F8ECEA', '#3A1A20', '#7A4E55', '#9D2933', '#3A1A20', '#EEDAD6', '#C79AA0', '#9D2933')] },
};
for (const [k, v] of Object.entries(CN_STYLES)) {
  const base = JSON.parse(JSON.stringify(J.STYLES.sumi));          // 结构（偏好权重、纹理……）沿用「墨と朱」
  J.STYLES[k] = Object.assign(base, v, { desc: v.name, hud: false, glow: 0.25,
    decor: { seal: 1.4, watermarkKanji: 1.4, brushStroke: 1.2, verticalStrip: 1.0, petals: 0.8, fireflies: 0.8, starField: 0.6, moonPhases: 0.6 } });
}

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
    // 背景巨字 / 残影的颜色按我们背景图的平均色来混，和画面同色调
    if (a.bgColor && J.STYLES[p.style] && CN_STYLES[p.style]) {
      J.STYLES[p.style].schemes = CN_STYLES[p.style].schemes.map(s => Object.assign({}, s, {
        bg: mixHex(s.bg, a.bgColor, 0.9), dim: mixHex(a.bgColor, s.fg, 0.20), ghostA: mixHex(a.bgColor, s.fg, 0.50) }));
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


def mean_color(imgs: list[Path]) -> str:
    """几张背景图中间区域的平均颜色（十六进制），没有图时按深蓝夜色处理。"""
    import numpy as np
    from PIL import Image

    if not imgs:
        return "#1D2B44"
    cols = []
    for img in imgs:
        im = Image.open(img).convert("RGB")
        w, h = im.size
        cols.append(np.asarray(im.crop((int(w * 0.1), int(h * 0.2), int(w * 0.9), int(h * 0.8))).resize((32, 48)), float).mean(axis=(0, 1)))
    return "#" + "".join(f"{int(round(v)):02X}" for v in np.mean(cols, axis=0))


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
    theme = THEMES[THEME_ALIASES.get(cfg.theme, cfg.theme)]
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
        "palettes": theme["palettes"], "bgLums": bg_lums, "bgColor": mean_color(bgs),
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
        koma = THEMES[THEME_ALIASES.get(cfg.theme, cfg.theme)]["koma"]
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
