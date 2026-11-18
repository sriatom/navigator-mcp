"""Vendor packs: everything one vendor needs, across the three layers, in one folder.

    navigator_packs/<pack>/
        pack.yml       platforms (catalogue entries: aliases, tool names, connection types, read / write owner),
                       ports, the fields the inventory interview asks, validators
        commands.yml   allowed read commands by category, per platform id
        adapters.py    the pack's own RUDDER adapters (built-in methods)
        knowledge.md   notes for NAVIGATOR's roles (verify commands, pitfalls). A `## Heading (ROLE, ROLE)`
                       section goes only to those roles' briefs; a heading without roles goes to every role

The loader merges every enabled pack into ATLAS's catalogue and command lists at runtime. Which packs are
enabled: env NAVIGATOR_PACKS ("arista,cisco" or "all"; default all). A disabled pack's platforms are not
known at all (not even as generic netmiko platforms).
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# Old role names still accepted in knowledge.md headings (renamed 2026-11-17).
ROLE_ALIASES = {"DATA_COLLECT": "DATA_COLLECTOR", "CONFIG_PUSH": "CONFIG_PUSHER"}


def available() -> list[str]:
    return sorted(p.parent.name for p in HERE.glob("*/pack.yml"))


def enabled() -> list[str]:
    want = os.environ.get("NAVIGATOR_PACKS", "all").strip()
    have = available()
    if want.lower() in ("", "all", "*"):
        return have
    names = [n.strip().lower() for n in want.split(",") if n.strip()]
    return [n for n in have if n in names]


@lru_cache(maxsize=32)
def _yaml(path: str, mtime: float) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _read(path: Path) -> dict:
    return _yaml(str(path), path.stat().st_mtime) if path.is_file() else {}


def pack(name: str) -> dict:
    """One pack's pack.yml (with its name)."""
    return {"name": name, **_read(HERE / name / "pack.yml")}


def platforms(names: list[str] | None = None) -> dict[str, dict]:
    """{platform id: catalogue entry} from the given (default: enabled) packs; each entry gets pack=<name>."""
    out: dict[str, dict] = {}
    for n in enabled() if names is None else names:
        for pid, entry in (pack(n).get("platforms") or {}).items():
            out[pid] = {**entry, "pack": n, "source": f"pack:{n}"}
    return out


def claimed_netmiko_names() -> set[str]:
    """netmiko device types owned by ANY pack (enabled or not), so they never become generic platforms."""
    return {e.get("netmiko") for e in platforms(available()).values() if e.get("netmiko")}


def commands(names: list[str] | None = None) -> dict[str, dict[str, list[str]]]:
    """{platform id: {category: [commands]}} from the packs' commands.yml."""
    out: dict[str, dict[str, list[str]]] = {}
    for n in enabled() if names is None else names:
        for pid, cats in (_read(HERE / n / "commands.yml") or {}).items():
            if isinstance(cats, dict):
                bucket = out.setdefault(str(pid), {})
                for cat, cmds in cats.items():
                    if isinstance(cmds, list):
                        bucket.setdefault(str(cat), []).extend(str(c) for c in cmds)
    return out


def knowledge(name: str) -> str:
    p = HERE / name / "knowledge.md"
    return p.read_text() if p.is_file() else ""


_ROLES_IN_HEADING = re.compile(r"^##\s+(?P<title>.*?)\s*(?:\((?P<roles>[A-Z_,\s]+)\))?\s*$")


def knowledge_sections(name: str) -> list[dict]:
    """One pack's knowledge.md as sections: {title, roles (empty = every role), text}. The text before the first
    `## ` heading is a section for every role."""
    out: list[dict] = []
    cur = {"title": "", "roles": [], "lines": []}
    for ln in knowledge(name).splitlines():
        m = _ROLES_IN_HEADING.match(ln) if ln.startswith("## ") else None
        if m:
            out.append(cur)
            roles = [r.strip() for r in (m.group("roles") or "").split(",") if r.strip()]
            cur = {"title": m.group("title"), "roles": roles, "lines": [ln]}
        elif ln.startswith("# ") and not cur["title"] and not cur["lines"]:
            continue                                    # the file's own title
        else:
            cur["lines"].append(ln)
    out.append(cur)
    return [{"title": c["title"], "roles": c["roles"], "text": "\n".join(c["lines"]).strip()}
            for c in out if "\n".join(c["lines"]).strip()]


def role_knowledge(role: str, names: list[str] | None = None) -> list[dict]:
    """What every enabled pack (or the given ones) tells this role: [{pack, vendor, text}]. A new pack's
    knowledge.md reaches every role's brief without changing the roles."""
    role = ROLE_ALIASES.get((role or "").strip().upper(), (role or "").strip().upper())
    out = []
    for n in enabled() if names is None else names:
        parts = [sec["text"] for sec in knowledge_sections(n)
                 if not sec["roles"] or role in {ROLE_ALIASES.get(r.upper(), r.upper()) for r in sec["roles"]}]
        if parts:
            out.append({"pack": n, "vendor": pack(n).get("vendor") or n.upper(), "text": "\n\n".join(parts)})
    return out


def summary() -> list[dict]:
    """One line per pack: name, enabled, platforms, methods (for docs and `inventory action=platforms`)."""
    on = set(enabled())
    rows = []
    for n in available():
        p = pack(n)
        rows.append({"pack": n, "enabled": n in on, "vendor": p.get("vendor"),
                     "platforms": sorted((p.get("platforms") or {}).keys()),
                     "methods": sorted((p.get("methods") or {}).keys())})
    return rows
