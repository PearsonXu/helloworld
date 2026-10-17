"""常用风格库：最多 5 个风格，每个风格保留版本历史，可恢复预设、回退版本、安装到 Claude Code。

目录结构：data/styles/<slug>/style.json（当前版本）、SKILL.md（由当前版本渲染）、history.jsonl（全部版本）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from .presets import MAX_STYLES, PRESETS
from .profile import StyleProfile, render_skill_md, slugify
from .settings import DATA_ROOT

DATA_DIR = DATA_ROOT / "styles"
CLAUDE_SKILLS_DIR = Path(os.environ.get("CHATCUT_STUDIO_SKILLS_DIR", Path.home() / ".claude" / "skills"))


def codex_skills_dir() -> Path:
    """Codex（ChatCut Desktop 里的「ChatGPT」Agent）读 skill 的目录：$CODEX_HOME/skills，默认 ~/.codex/skills。"""
    from .gateway import codex_config_path
    return codex_config_path().parent / "skills"


def _targets(slug: str) -> dict[str, Path]:
    return {"Claude Code": CLAUDE_SKILLS_DIR / slug / "SKILL.md", "Codex": codex_skills_dir() / slug / "SKILL.md"}


def installed(slug: str) -> bool:
    return any(p.exists() for p in _targets(slug).values())


class LimitReached(Exception):
    pass


def _dir(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,59}", slug):
        raise ValueError("非法的风格 id")
    return DATA_DIR / slug


def _slugs() -> list[str]:
    ensure_seeded()
    if not DATA_DIR.exists():
        return []
    order = {s: i for i, s in enumerate(PRESETS)}  # 内置风格按预设顺序在前，自定义的按名字在后
    return sorted((d.name for d in DATA_DIR.iterdir() if (d / "style.json").exists()),
                  key=lambda s: (order.get(s, len(order)), s))


def _write(slug: str, p: StyleProfile, note: str) -> None:
    d = _dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "style.json").write_text(p.model_dump_json(indent=2), "utf-8")
    (d / "SKILL.md").write_text(render_skill_md(p, slug), "utf-8")
    if installed(slug):          # 装过的风格改了之后自动更新，Agent 下次用的就是新版
        install(slug)
    with open(d / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": int(time.time()), "note": note, "profile": p.model_dump()}, ensure_ascii=False) + "\n")


def _history(slug: str) -> list[dict]:
    f = _dir(slug) / "history.jsonl"
    return [json.loads(l) for l in f.read_text("utf-8").splitlines() if l.strip()] if f.exists() else []


def ensure_seeded() -> None:
    """第一次启动时写入内置风格。之后用户删掉的预设不会自动回来（可以手动恢复）。"""
    marker = DATA_DIR / ".seeded"
    if marker.exists():
        return
    for slug, p in PRESETS.items():
        if not (DATA_DIR / slug / "style.json").exists():
            _write(slug, p, "内置预设")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def list_styles() -> list[dict]:
    ensure_seeded()
    out = []
    for slug in _slugs():
        p = StyleProfile.model_validate_json((_dir(slug) / "style.json").read_text("utf-8"))
        out.append({"slug": slug, "name": p.name, "description": p.description, "preset": slug in PRESETS,
                    "versions": len(_history(slug)),
                    "installed": installed(slug)})
    return out


def missing_presets() -> list[dict]:
    have = set(_slugs())
    return [{"slug": s, "name": p.name} for s, p in PRESETS.items() if s not in have]


def get(slug: str) -> dict:
    ensure_seeded()
    d = _dir(slug)
    if not (d / "style.json").exists():
        raise FileNotFoundError(slug)
    hist = _history(slug)
    return {
        "slug": slug,
        "preset": slug in PRESETS,
        "profile": StyleProfile.model_validate_json((d / "style.json").read_text("utf-8")).model_dump(),
        "skill_md": (d / "SKILL.md").read_text("utf-8"),
        "history": [{"v": i + 1, "ts": h["ts"], "note": h["note"]} for i, h in enumerate(hist)][::-1],
    }


def update(slug: str, p: StyleProfile, note: str = "手动修改") -> dict:
    if not (_dir(slug) / "style.json").exists():
        raise FileNotFoundError(slug)
    _write(slug, p, note)
    return get(slug)


def create(p: StyleProfile, note: str = "新建") -> str:
    ensure_seeded()
    if len(_slugs()) >= MAX_STYLES:
        raise LimitReached(f"常用风格最多 {MAX_STYLES} 个，请先删除一个")
    base = slugify(p.name)
    slug, i = base, 2
    while (DATA_DIR / slug).exists():
        slug, i = f"{base[:56]}-{i}", i + 1
    _write(slug, p, note)
    return slug


def restore_preset(slug: str) -> dict:
    """把内置风格恢复成出厂设置（被删掉的也可以加回来，受数量上限约束）。"""
    if slug not in PRESETS:
        raise FileNotFoundError(slug)
    if slug not in _slugs() and len(_slugs()) >= MAX_STYLES:
        raise LimitReached(f"常用风格最多 {MAX_STYLES} 个，请先删除一个")
    _write(slug, PRESETS[slug], "恢复为内置预设")
    return get(slug)


def revert(slug: str, version: int) -> dict:
    hist = _history(slug)
    if not 1 <= version <= len(hist):
        raise FileNotFoundError(f"{slug} v{version}")
    _write(slug, StyleProfile.model_validate(hist[version - 1]["profile"]), f"回退到 v{version}")
    return get(slug)


def delete(slug: str) -> None:
    shutil.rmtree(_dir(slug), ignore_errors=True)


def install(slug: str) -> dict[str, str]:
    """复制到 Claude Code（~/.claude/skills）和 Codex（~/.codex/skills）的 skill 目录：
    ChatCut Desktop 里选「Claude」或「ChatGPT」（Codex）的 Agent 都能加载，不管它背后用的是 DeepSeek 还是 Claude。"""
    src = _dir(slug) / "SKILL.md"
    if not src.exists():
        raise FileNotFoundError(slug)
    out = {}
    for name, dst in _targets(slug).items():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        out[name] = str(dst)
    return out
