"""本地风格库：data/styles/<slug>/{style.json, SKILL.md}，并可安装到 Claude Code 的 skills 目录。"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .profile import StyleProfile, slugify

DATA_DIR = Path(os.environ.get("CHATCUT_STUDIO_DATA", Path(__file__).parent / "data")) / "styles"
CLAUDE_SKILLS_DIR = Path(os.environ.get("CHATCUT_STUDIO_SKILLS_DIR", Path.home() / ".claude" / "skills"))


def _dir(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,59}", slug):
        raise ValueError("非法的风格 id")
    return DATA_DIR / slug


def list_styles() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    out = []
    for d in sorted(DATA_DIR.iterdir()):
        f = d / "style.json"
        if f.exists():
            p = StyleProfile.model_validate_json(f.read_text("utf-8"))
            out.append({"slug": d.name, "name": p.name, "description": p.description,
                        "installed": (CLAUDE_SKILLS_DIR / d.name / "SKILL.md").exists()})
    return out


def get(slug: str) -> dict:
    d = _dir(slug)
    if not (d / "style.json").exists():
        raise FileNotFoundError(slug)
    return {
        "slug": slug,
        "profile": StyleProfile.model_validate_json((d / "style.json").read_text("utf-8")).model_dump(),
        "skill_md": (d / "SKILL.md").read_text("utf-8") if (d / "SKILL.md").exists() else "",
    }


def save(profile: StyleProfile, skill_md: str) -> str:
    slug = slugify(profile.name)
    d = _dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "style.json").write_text(profile.model_dump_json(indent=2), "utf-8")
    (d / "SKILL.md").write_text(skill_md, "utf-8")
    return slug


def delete(slug: str) -> None:
    shutil.rmtree(_dir(slug), ignore_errors=True)


def install(slug: str) -> str:
    """复制到 ~/.claude/skills/<slug>/SKILL.md，Claude Code（以及 ChatCut Desktop 里选用的 Claude Code agent）可加载。"""
    src = _dir(slug) / "SKILL.md"
    if not src.exists():
        raise FileNotFoundError(slug)
    dst = CLAUDE_SKILLS_DIR / slug / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return str(dst)
