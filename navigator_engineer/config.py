"""NAVIGATOR settings, from navigator.yml (paths and switches only, no secrets).

Lookup: $NAVIGATOR_BOOTSTRAP, then navigator.yml at the repo root. Env NAVIGATOR_CONSUMER overrides CONSUMER.
Everything NAVIGATOR needs ships in this package (decision #97); SKILLS_ROOT only adds the user's own.
PROJECT_ROOT (old name WORKSPACE) and SKILLS_ROOT (old name EXTRA_SKILLS, one path or a list) are set by
`navigator-setup`. APPROVAL: one section for the whole host. Without its own APPROVAL section NAVIGATOR uses
RUDDER's (rudder.yml + rudder.local.yml), which is where navigator-setup writes it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PKG = Path(__file__).resolve().parent


def _p(v) -> Path:
    return Path(os.path.expanduser(str(v)))


@dataclass(frozen=True)
class Settings:
    workspace: Path                       # project folders live here (data only, never software)
    adhoc: str = "ADHOC_WORK"             # sub-folder for jobs without a project name
    state_path: Path = Path("~/.navigator").expanduser()   # lessons, scrambler maps, approvals state
    consumer: str = "NAVI"
    default_inventory: str = ""
    extra_skills: tuple = ()              # optional folders of the user's own skills (read-only)
    not_projects: tuple = ("ADHOC_WORK", "OLD_SKILLS", "COMPLETED_PROJECTS", "AGENT_PROFILES", "Backups")
    scrambler_maps: Path | None = None    # where scrambler maps live (private); default <state>/scrambler
    keep_nets: tuple = ()                 # CIDRs the scrambler never maps (lab space that is really public)
    approval: dict = field(default_factory=dict)   # same APPROVAL section as rudder.yml (LOCAL -> CLOUD)
    search: dict = field(default_factory=dict)     # SEARCH: {URL, TIMEOUT, MAX_RESULTS, MAX_FETCH, MAX_PAGE_CHARS}
    models: dict = field(default_factory=dict)     # MODELS: {HOST: cloud|local, LOCAL: {URL, MODEL, ...}} (models.py)

    @property
    def maps_path(self) -> Path:
        return self.scrambler_maps or self.state_path / "scrambler"

    @property
    def lessons_file(self) -> Path:
        return self.state_path / "LESSONS_LEARNED.md"


def _paths(v) -> list:
    if not v:
        return []
    return [_p(x) for x in (v if isinstance(v, (list, tuple)) else [v]) if str(x).strip()]


def _approval(raw: dict) -> dict:
    if raw.get("APPROVAL"):
        return raw["APPROVAL"]
    try:
        from rudder_devices import config as rconfig
        return rconfig.load().approval or {}
    except Exception:  # noqa: BLE001   RUDDER settings missing or broken: approvals then fail loudly later
        return {}


def load() -> Settings:
    boot = Path(os.environ.get("NAVIGATOR_BOOTSTRAP") or REPO / "navigator.yml")
    raw = (yaml.safe_load(boot.read_text()) or {}) if boot.is_file() else {}
    maps = raw.get("SCRAMBLER_MAPS")
    skills = []
    for p in _paths(raw.get("SKILLS_ROOT")) + _paths(raw.get("EXTRA_SKILLS")):
        if p not in skills:
            skills.append(p)
    return Settings(
        workspace=_p(raw.get("PROJECT_ROOT") or raw.get("WORKSPACE") or "~/workspace"),
        adhoc=str(raw.get("ADHOC", "ADHOC_WORK")),
        state_path=_p(raw.get("STATE_PATH", "~/.navigator")),
        consumer=str(os.environ.get("NAVIGATOR_CONSUMER") or raw.get("CONSUMER") or "NAVI").upper(),
        default_inventory=str(raw.get("DEFAULT_INVENTORY") or ""),
        extra_skills=tuple(skills),
        scrambler_maps=_p(maps) if maps else None,
        keep_nets=tuple(str(x) for x in (raw.get("KEEP_NETS") or [])),
        approval=_approval(raw),
        search=raw.get("SEARCH") or {},
        models=raw.get("MODELS") or {},
    )


def atomic_write(p: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file + rename: a crash leaves the old file or the new one, never half of one."""
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)


def state_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p
