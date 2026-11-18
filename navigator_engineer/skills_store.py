"""Skills: the ones NAVIGATOR ships (skills/*.md, roles/*.md) plus, optionally, the user's own skill folders
(EXTRA_SKILLS, one folder per skill with a SKILL.md, read-only). Nothing here is required to work."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .config import PKG, Settings

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _meta(text: str) -> dict:
    if text.startswith("---"):
        try:
            return yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError:
            return {}
    return {}


def _all(s: Settings) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted((PKG / "skills").glob("*.md")) + sorted((PKG / "roles").glob("*.md")):
        out[_meta(p.read_text(encoding="utf-8")).get("name") or p.stem] = p
    for base in s.extra_skills:
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "SKILL.md").is_file() and _NAME.match(d.name) and d.name not in out:
                    out[d.name] = d / "SKILL.md"
    return out


def skill_list(s: Settings, query: str | None = None) -> list[dict]:
    rows = []
    for name, p in _all(s).items():
        m = _meta(p.read_text(encoding="utf-8", errors="replace"))
        desc = str(m.get("description") or "")
        if query and query.lower() not in (name + " " + desc).lower():
            continue
        rows.append({"skill": name, "description": desc[:300], "source": "navigator" if PKG in p.parents else "extra"})
    return rows


def skill_load(s: Settings, name: str) -> str:
    skills = _all(s)
    if name not in skills:
        raise KeyError(f"no skill {name!r}; use skill_list")
    return skills[name].read_text(encoding="utf-8", errors="replace")
