"""Troubleshooting sessions (SHIP_ENGINEER): one open session per project (or one for ad hoc work).

While a session is open, every device read, compare and credential test through NAVIGATOR is appended to
<project>/RECORDS/TROUBLESHOOT/<time>-<title>.md: the command, the connection method, the adapter, the
time taken, the status, errors, the output (shortened; the full output is in the record files) and the
debug log when one was asked for. Read-only: changes still go through tickets and /pushok.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, atomic_write, state_dir

MAX_OUT = 4000


class TroubleshootError(Exception):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _text(o) -> str:
    if o is None:
        return ""
    t = o if isinstance(o, str) else json.dumps(o, indent=1, sort_keys=True)
    return t if len(t) <= MAX_OUT else t[:MAX_OUT] + f"\n... ({len(t) - MAX_OUT} more characters in the record file)"


class Troubleshoot:
    def __init__(self, s: Settings):
        self.s = s
        self.dir = state_dir(s.state_path / "troubleshoot")

    @staticmethod
    def _key(project: str | None) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", project) if project else "_ADHOC"

    def _state(self, project) -> Path:
        return self.dir / f"{self._key(project)}.json"

    def current(self, project: str | None) -> dict | None:
        p = self._state(project)
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def _put(self, project, st: dict) -> None:
        atomic_write(self._state(project), json.dumps(st, indent=1), mode=0o600)

    def start(self, title: str, project: str | None, folder: Path) -> dict:
        title = " ".join(str(title or "").split())
        if not title:
            raise TroubleshootError("say what is broken, e.g. title='pyATS reads on EDGE1 time out'")
        cur = self.current(project)
        if cur:
            raise TroubleshootError(f"a session is already open: {cur['title']!r} ({cur['file']}); stop it first")
        folder.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:50] or "session"
        f = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}.md"
        f.write_text(f"# Troubleshooting: {title}\n\n- Project: {project or '(ad hoc)'}\n- Started: {_utc()}\n\n")
        st = {"title": title, "project": project, "file": str(f), "started": time.time(), "steps": 0}
        self._put(project, st)
        return {"session": "open", **st}

    def _append(self, project, text: str) -> dict | None:
        st = self.current(project)
        if not st:
            return None
        with open(st["file"], "a", encoding="utf-8") as fh:
            fh.write(text)
        st["steps"] += 1
        self._put(project, st)
        return st

    def note(self, project: str | None, text: str) -> dict:
        if not self.current(project):
            raise TroubleshootError("no open troubleshooting session; start one first")
        st = self._append(project, f"## {_utc()} — note\n\n{text.strip()}\n\n")
        return {"logged": True, "file": st["file"], "steps": st["steps"]}

    def log_call(self, project: str | None, what: str, args: dict, result: dict) -> bool:
        """Append one device call if a session is open. Returns whether it was logged."""
        if not self.current(project):
            return False
        lines = [f"## {_utc()} — {what}", "", "```", json.dumps(args, sort_keys=True, default=str), "```", ""]
        devs = result.get("devices") or ([result] if "device" in result else [])
        if "methods" in result:                                   # compare
            devs = [{**m, "device": f"{result.get('device')} via {m['via']}"} for m in result["methods"]]
        for d in devs:
            lines.append(f"### {d.get('device')}: {d.get('status')} — adapter {d.get('adapter')}"
                         + (f", {d.get('seconds')} s" if d.get("seconds") is not None else ""))
            if d.get("error") or d.get("reason"):
                lines.append(f"Error: {d.get('error') or d.get('reason')}")
            for r in d.get("results") or []:
                lines += ["", f"`{r['command']}` — {'ok' if r['ok'] else 'FAILED'} ({r['format']})"
                          + (f": {r['error']}" if r.get("error") else ""), "```", _text(r.get("output")), "```"]
                if r.get("record"):
                    lines.append(f"Record: {r['record']}")
            if d.get("debug"):
                lines += ["", "Debug log:", "```", "\n".join(map(str, d["debug"]))[-8000:], "```"]
            lines.append("")
        for c in result.get("compare") or []:
            lines.append(f"Compare `{c['command']}` {c['between'][0]} vs {c['between'][1]}: "
                         + ("same" if c.get("same") else (c.get("note") or "different")))
            if c.get("diff"):
                lines += ["```diff", c["diff"], "```"]
        self._append(project, "\n".join(lines) + "\n\n")
        return True

    def stop(self, project: str | None, summary: str | None) -> dict:
        st = self.current(project)
        if not st:
            raise TroubleshootError("no open troubleshooting session")
        mins = round((time.time() - st["started"]) / 60, 1)
        with open(st["file"], "a", encoding="utf-8") as fh:
            fh.write(f"## Summary ({_utc()}, {st['steps']} step(s), {mins} min)\n\n{(summary or '-').strip()}\n")
        self._state(project).unlink(missing_ok=True)
        return {"session": "closed", "file": st["file"], "steps": st["steps"], "minutes": mins}

    def show(self, project: str | None, with_text: bool) -> dict:
        st = self.current(project)
        folder_hint = None
        if not st:
            return {"session": None, "note": "no open session", "folder": folder_hint}
        out = {"session": "open", **st}
        if with_text:
            t = Path(st["file"]).read_text(encoding="utf-8")
            out["text"] = t[-20000:]
        return out
