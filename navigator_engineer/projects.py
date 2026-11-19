"""Project folders (moved from NAVI's navi_project.py, decision #97): create / open / list / status /
close, the run log, and the CLOUD / LOCAL mode.

A project is a folder directly under WORKSPACE (or WORKSPACE/ADHOC_WORK/<JOB>_<stamp> for a job with no
name). Files: TASK.md, RUNLOG.md, NAVIGATOR_STATE.json, .navigator_mode, .navigator_inventory and LOGS/
CONFIG/ OUT/ RECORDS/ RAW/. Older projects made by NAVI have NAVI_STATE.json, .navi_mode and .navi_inventory:
those are still read and updated in place (never renamed), so they open unchanged (decision #151).
Nothing existing is ever overwritten.

Mode = the project's model (decision #156): CLOUD = cloud models may see project data; LOCAL = local model
only. A new project takes the model the host runs when it is created, unless mode is given. The words OPEN
(= CLOUD) and LOCKED (= LOCAL) of older projects are still read and accepted. A folder without a mode file
counts as LOCAL. LOCAL -> CLOUD needs the user's out-of-band approval (/pushok), like a device change.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import PKG, Settings, atomic_write

CLOUD, LOCAL = "CLOUD", "LOCAL"
OPEN, LOCKED = CLOUD, LOCAL                 # the older words for the same two modes
_WORDS = {"CLOUD": CLOUD, "OPEN": CLOUD, "LOCAL": LOCAL, "LOCKED": LOCAL}


def norm_mode(m) -> str | None:
    """CLOUD / LOCAL from any of CLOUD, LOCAL, OPEN, LOCKED (any case); None otherwise."""
    return _WORDS.get(str(m or "").strip().upper())

MODE_FILE, INVENTORY_MARK, STATE_FILE = ".navigator_mode", ".navigator_inventory", "NAVIGATOR_STATE.json"
OLD_NAMES = {MODE_FILE: ".navi_mode", INVENTORY_MARK: ".navi_inventory", STATE_FILE: "NAVI_STATE.json"}
SUBDIRS = ("LOGS", "CONFIG", "OUT", "RECORDS", "RAW")
STATUSES = ("active", "waiting_input", "done")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_TICKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")     # an outside ticket: CHG0012345, INC123...


class ProjectError(Exception):
    pass


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_file(pdir: Path, name: str) -> Path:
    """<pdir>/<name>, or the NAVI-era name when only that one exists (older projects keep their files)."""
    new = pdir / name
    old = pdir / OLD_NAMES.get(name, name)
    return old if not new.exists() and old.exists() else new


def _write_new(p: Path, text: str) -> bool:
    if p.exists():
        return False
    p.write_text(text, encoding="utf-8")
    return True


class Projects:
    def __init__(self, s: Settings):
        self.s = s

    # ------------------------------------------------------------------ lookup
    def _check_name(self, name: str) -> None:
        if not _NAME.match(name or "") or name in self.s.not_projects:
            raise ProjectError(f"invalid project name {name!r} (letters, digits, . _ - only)")

    def find(self, name: str) -> Path:
        """A project by name (WORKSPACE/<name> or WORKSPACE/ADHOC_WORK/<name>); never a path outside."""
        self._check_name(name)
        hits = [b / name for b in (self.s.workspace, self.s.workspace / self.s.adhoc) if (b / name).is_dir()]
        if not hits:
            raise ProjectError(f"no project {name!r} in {self.s.workspace}")
        if len(hits) > 1:
            raise ProjectError(f"{name!r} exists both at the top and under {self.s.adhoc}; ask the user which one")
        return hits[0]

    def mode(self, pdir: Path) -> str:
        try:
            m = project_file(pdir, MODE_FILE).read_text().strip().upper()
        except OSError:
            return LOCAL
        return norm_mode(m) or LOCAL

    def _state(self, pdir: Path) -> dict:
        try:
            return json.loads(project_file(pdir, STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def log(self, pdir: Path, actor: str, action: str, result: str) -> None:
        with open(pdir / "RUNLOG.md", "a", encoding="utf-8") as fh:
            fh.write(f"\n## {utc()} -- {actor}\nAction: {action}\nResult: {result}\n")

    # ------------------------------------------------------------------ tools
    def create(self, mode: str | None, description: str, name: str | None = None, inventory: str | None = None,
               ticket_id: str | None = None, host: str | None = None) -> dict:
        """mode None = the host's model now (host: "local" / "cloud", else MODELS.HOST)."""
        if mode is None or not str(mode).strip():
            from .models import host_kind
            mode = LOCAL if (host or host_kind(self.s)) == "local" else CLOUD
        mode = norm_mode(mode)
        if mode is None:
            raise ProjectError("mode must be CLOUD or LOCAL")
        if not (description or "").strip():
            raise ProjectError("give a short job description")
        ticket_id = (ticket_id or "").strip() or None
        if ticket_id and not _TICKET.match(ticket_id):
            raise ProjectError("ticket_id: letters, digits and . _ - only (up to 40), e.g. CHG0012345")
        if name:
            self._check_name(name)
            try:
                pdir = self.find(name)
            except ProjectError as e:
                if "both" in str(e):
                    raise
                pdir = self.s.workspace / name
        else:
            slug = re.sub(r"[^A-Za-z0-9]+", "_", description).strip("_").upper()[:50] or "JOB"
            pdir = self.s.workspace / self.s.adhoc / f"{slug}_{datetime.now().strftime('%Y%m%d-%H%M')}"
        if not self.s.workspace.is_dir():
            raise ProjectError(f"the workspace folder {self.s.workspace} does not exist")
        existed = pdir.is_dir()
        for sub in SUBDIRS:
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        notes = []
        if existed and project_file(pdir, MODE_FILE).exists():
            if self.mode(pdir) != mode:
                notes.append(f"the project already has mode {self.mode(pdir)}; it was not changed (use mode_set)")
            mode = self.mode(pdir)
        else:
            _write_new(pdir / MODE_FILE, mode + "\n")
        inv = inventory or self.s.default_inventory
        if project_file(pdir, INVENTORY_MARK).exists():
            cur = project_file(pdir, INVENTORY_MARK).read_text().strip()
            if inventory and cur != inventory:
                notes.append(f"the project already uses inventory {cur}; it was not changed")
            inv = cur
        elif inv:
            _write_new(pdir / INVENTORY_MARK, inv + "\n")
        tpl = (PKG / "templates" / "TASK.md").read_text(encoding="utf-8")
        task = (tpl.replace("{PROJECT}", pdir.name).replace("{ROOT}", str(pdir)).replace("{MODE}", mode)
                .replace("{INVENTORY}", inv or "-").replace("{CREATED}", utc()).replace("{DESC}", description))
        wrote_task = _write_new(pdir / "TASK.md", task)
        _write_new(pdir / "RUNLOG.md", f"# RUNLOG — {pdir.name}\n")
        self.log(pdir, "NAVIGATOR", f"project {'opened' if existed else 'created'} (mode {mode}): {description}", "OK")
        _write_new(project_file(pdir, STATE_FILE), json.dumps({"incident": pdir.name, "mode": mode, "status": "active",
                                                  "phase": "interview", "last_update": utc()}, indent=2) + "\n")
        if ticket_id:
            st = self._state(pdir)
            cur = st.get("ticket_id")
            if cur and cur != ticket_id:
                notes.append(f"the project already has ticket ID {cur}; it was not changed")
            elif not cur:
                st["ticket_id"] = ticket_id
                st["last_update"] = utc()
                atomic_write(project_file(pdir, STATE_FILE), json.dumps(st, indent=2) + "\n")
                self.log(pdir, "NAVIGATOR", f"ticket ID {ticket_id}", "OK")
        out = self.show(pdir.name, host)
        out.update(created=not existed, task_written=wrote_task, notes=notes)
        return out

    def show(self, name: str, host: str | None = None) -> dict:
        pdir = self.find(name)
        st = self._state(pdir)
        own = sorted(e.name for e in pdir.iterdir() if not e.name.startswith(".") and e.name not in
                     SUBDIRS + ("TASK.md", "RUNLOG.md", STATE_FILE, OLD_NAMES[STATE_FILE], "SANITIZED", "RESTORED",
                                                    "DATA_COLLECT", "DATA_COLLECTOR"))
        raw = sorted(e.name for e in (pdir / "RAW").iterdir()) if (pdir / "RAW").is_dir() else []
        mode = self.mode(pdir)
        return {"project": pdir.name, "path": str(pdir), "mode": mode,
                "inventory": (project_file(pdir, INVENTORY_MARK).read_text().strip()
                              if project_file(pdir, INVENTORY_MARK).exists() else None),
                "ticket_id": st.get("ticket_id"),
                "status": st.get("status", "unknown"), "phase": st.get("phase"), "ask": st.get("ask"),
                "user_files": own[:50], "raw_files": raw[:50], "folders": _folders(pdir),
                "next": self._next(mode, host)}

    def _next(self, mode: str, host: str | None = None) -> str:
        if mode != LOCAL:
            return ("CLOUD: the host's model may read the project files: project action=search finds text in them "
                "(with the config block around it), action=files lists them.")
        from .models import host_kind
        if (host or host_kind(self.s)) == "local":
            return ("LOCAL: this host runs a local model, so you may read the project files: project action=search "
                    "finds text in them (with the config block around it), action=files lists them.")
        return ("LOCAL: only a local model may read this project's files, logs and configs, and this host's model "
                "is a cloud model. Do not open them yourself: use local_model action=ask (the answer is written to "
                "the project's OUT folder), or ask the user to switch the host to the local model.")

    def list(self) -> list[dict]:
        out = []
        for base, adhoc in ((self.s.workspace, False), (self.s.workspace / self.s.adhoc, True)):
            if not base.is_dir():
                continue
            for p in sorted(base.iterdir()):
                if p.is_dir() and not p.name.startswith(".") and p.name not in self.s.not_projects \
                        and ((p / "TASK.md").exists() or project_file(p, MODE_FILE).exists()):
                    st = self._state(p)
                    row = {"project": p.name, "adhoc": adhoc, "mode": self.mode(p),
                           "status": st.get("status", "unknown"), "phase": st.get("phase")}
                    if st.get("ticket_id"):
                        row["ticket_id"] = st["ticket_id"]
                    out.append(row)
        return out

    def status(self, name: str, status: str, phase: str | None = None, ask: str | None = None,
               note: str | None = None) -> dict:
        pdir = self.find(name)
        status = (status or "").lower()
        if status not in STATUSES:
            raise ProjectError(f"status must be one of {', '.join(STATUSES)}")
        if status == "waiting_input" and not ask:
            raise ProjectError("waiting_input needs the question for the user (ask)")
        st = self._state(pdir) or {"incident": pdir.name, "mode": self.mode(pdir)}
        old = (st.get("status"), st.get("phase"))
        st["status"] = status
        if phase:
            st["phase"] = phase
        for k in ("alert_count", "last_alert") + (() if status == "waiting_input" else ("ask", "nudge_count", "last_nudge")):
            st.pop(k, None)
        if status == "waiting_input":
            st["ask"] = ask
        st["last_update"] = utc()
        atomic_write(project_file(pdir, STATE_FILE), json.dumps(st, indent=2) + "\n")
        if (st["status"], st.get("phase")) != old or note:
            self.log(pdir, "NAVIGATOR", note or "status update",
                     f"STATUS {old[0]} -> {status}, PHASE {old[1]} -> {st.get('phase')}" + (f"; ASK: {ask}" if ask else ""))
        return {"project": pdir.name, "status": status, "phase": st.get("phase"), "last_update": st["last_update"]}

    def record(self, name: str, actor: str, action: str, result: str) -> dict:
        pdir = self.find(name)
        actor = re.sub(r"[^A-Z0-9_ -]", "", (actor or "NAVIGATOR").upper())[:40] or "NAVIGATOR"
        self.log(pdir, actor, " ".join(action.split())[:2000], " ".join(result.split())[:2000])
        return {"project": pdir.name, "logged": True}

    def set_mode_now(self, name: str, mode: str) -> dict:
        """Write the mode. The caller has checked the approval when going to CLOUD."""
        pdir = self.find(name)
        old = self.mode(pdir)
        project_file(pdir, MODE_FILE).write_text(mode + "\n")
        self.log(pdir, "NAVIGATOR", f"MODE {old} -> {mode}", "OK")
        return {"project": pdir.name, "mode": mode, "was": old}


def _folders(pdir: Path) -> dict:
    from .project_files import overview
    try:
        return overview(pdir)
    except OSError:
        return {}
