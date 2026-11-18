"""Roles (NAVI's 8 subagents, decision #98), the QC checklist, lessons learned, and skill export.

Role text ships in roles/<ROLE>.md (front matter + body). role_brief() returns the role's text plus the
project facts the role needs, ready for the host agent to hand to a subagent (Hermes delegate_task,
OpenClaw subagents). export_skills() writes them as SKILL.md folders for hosts that load skills from disk.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
from pathlib import Path

import yaml

import navigator_packs

from .config import PKG, Settings, load, state_dir
from .projects import Projects, utc

ROLE_DIR = PKG / "roles"
SKILL_DIR = PKG / "skills"
QC_ITEMS = [
    ("brief", "Answers the brief completely; gaps declared"),
    ("version", "Vendor / OS / version match the reference table for every command, line, doc and log"),
    ("evidence", "Every claim cites file + line, record path or doc URL; two citations spot-checked"),
    ("no_invention", "Names, IPs, versions, log lines, URLs and outputs exist where cited"),
    ("safety", "Configs / change plans: real values, rollback, verification, NEEDS APPROVAL marks, one OS"),
    ("data_handling", "No secrets; files only in the project; LOCAL data not in public queries; sanitize used"),
    ("lessons", "No issue already in the lessons learned is repeated"),
    ("format", "Follows the user's preferences; short final message listing every file created"),
]
VERDICTS = ("PASS", "PASS WITH NOTES", "REJECT")
CATEGORIES = ("technical", "process", "format", "safety")


class RoleError(Exception):
    pass


# Old role names (before 2026-11-17) are still accepted: in role_brief and in vendor pack headings.
ALIASES = {"DATA_COLLECT": "DATA_COLLECTOR", "CONFIG_PUSH": "CONFIG_PUSHER"}


def canonical(role: str) -> str:
    r = (role or "").strip().upper()
    return ALIASES.get(r, r)


def _split(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.lstrip("\n")
    return {}, text


def roles() -> dict[str, dict]:
    out = {}
    for p in sorted(ROLE_DIR.glob("*.md")):
        meta, body = _split(p.read_text(encoding="utf-8"))
        out[meta.get("role", p.stem)] = {"meta": meta, "body": body, "path": p}
    return out


def role_list() -> list[dict]:
    return [{"role": r, "description": v["meta"].get("description", "")} for r, v in roles().items()]


def role_brief(s: Settings, role: str, task: str, project: str | None = None) -> dict:
    allr = roles()
    key = next((k for k in allr if k.upper() == canonical(role)), None)
    if not key:
        raise RoleError(f"no role {role!r}; roles: {', '.join(allr)}")
    if not (task or "").strip():
        raise RoleError("give the task for the role")
    facts = ""
    if project:
        p = Projects(s).show(project)
        facts = (f"\n## This brief\n\n- Project: {p['project']} ({p['path']})\n- MODE: {p['mode']}\n"
                 f"- Inventory: {p['inventory'] or '-'}\n- Device reference table: TASK.md in the project\n")
    brief = (f"{allr[key]['body'].rstrip()}\n{facts}\n{TOOL_NAMES}\n{vendor_notes(key)}"
             f"## Task\n\n{task.strip()}\n")
    return {"role": key, "brief": brief,
            "note": "Hand this brief to a subagent (Hermes delegate_task / OpenClaw subagent) and send its "
                    "result through SANITY_AGENT (qc action=check) before the user sees it."}


def vendor_notes(role: str) -> str:
    """The enabled vendor packs' notes for this role (navigator_packs/<pack>/knowledge.md). Adding a pack teaches
    every role at once: its commands.yml gives DATA_COLLECTOR and STATE_VALIDATOR the commands per category, and
    its knowledge.md sections reach the roles they name."""
    notes = navigator_packs.role_knowledge(role)
    if not notes:
        return ""
    body = "\n\n".join(f"### {n['vendor']} (pack `{n['pack']}`)\n\n" + re.sub(r"^## ", "#### ", n["text"], flags=re.M)
                         for n in notes)
    return ("## Vendor notes (from the vendor packs)\n\n"
            "Commands per platform and category come from the packs too: `inventory action=commands platform=...`.\n\n"
            f"{body}\n\n")


# The role texts name the tools of the three separate servers. Since P7d the agent sees one NAVIGATOR
# server with grouped tools; every brief carries this map so a subagent can call them by either name.
TOOL_NAMES = """## Tool names (one NAVIGATOR server)

| In this text | Call |
|---|---|
| inventory_list / inventory_show / inventory_query / inventory_validate / inventory_history | `inventory action=list / show / query / validate / history` |
| platform_list / platform_show / command_list / template_get / netbox_export | `inventory action=platforms / platform / commands / template / netbox_export` |
| interview_start / _answer / _status / _preview / _commit / _cancel / _list | `inventory_edit action=start / answer / status / preview / commit / cancel / list` |
| credential_list / credential_test | `credential action=list / test` |
| device_read / device_facts / device_compare | `device action=read / facts / compare` (via=, raw=, debug=) |
| adapter_list | `adapter action=list` |
| change_propose / change_schedule / change_status / change_apply / change_cancel / change_list | `change action=propose / schedule / status / apply / cancel / list` |
| project_create / _show / _list / _status / _close, task_record | `project action=create / show / list / status / close / task` |
| mode_get / mode_set, role_list / role_brief, qc_check / qc_record | `mode`, `role`, `qc` with action= |
| lesson_add / lessons_list, skill_list / skill_load | `lesson action=add / list`, `skill action=list / load` |
| sanitize / sanitize_text / restore / scrambler_map | `scrambler action=sanitize / text / restore / map` |

Pass `project=` on inventory, device, change and credential calls so records land in the project.
"""


# A citation the evidence check can see: a file or record path, a URL, "line N", or quoted command output.
_CITE = re.compile(r"(?i)(https?://|\b[\w.-]+/[\w./-]+|\.(md|json|txt|log|yml|yaml|cfg)\b|\bline \d+|\brecords?\b|"
                   r"\bRAW/|\bOUT/|```)")
VERDICT_LINE = "QC VERDICT: <PASS | PASS WITH NOTES | REJECT>"


REJECT_NO_EVIDENCE = ("QC VERDICT: REJECT\n- evidence: the claim cites no record, file or output\n"
                      "- no_invention: nothing shows where the claim comes from")
RESEND = ("QC judges the evidence that comes with a claim; it does not go and collect it. To get a PASS, send "
          "the claim again with its evidence: the command output (in a code block), a record or file path, "
          "or a URL.")


def qc_check(role: str, item: str) -> dict:
    """The checklist, plus what can be seen mechanically: an item that cites nothing fails 'evidence' and
    'no_invention', so the verdict is REJECT. Small (local) models get the verdict and its format spelled out."""
    cited = bool(_CITE.search(str(item or "")))
    out = {"item": item, "role": role, "checks": [{"id": k, "check": t} for k, t in QC_ITEMS],
           "verdicts": list(VERDICTS), "evidence_cited": cited,
           "reply_format": f"Start the reply with the line '{VERDICT_LINE}', then one finding per failed check.",
           "next": "Check every item, then qc_record with the verdict and a finding per failed item; "
                   "add a lesson (lesson_add) for every issue flagged. QC judges the evidence that came WITH "
                   "the result: do not collect new evidence yourself and do not say whether the claim is "
                   "true; say whether it is supported. Evidence that is missing, does not show the claim, or contradicts it = REJECT; \"could not verify\" is REJECT, never PASS WITH NOTES."}
    if not cited:
        out["failed"] = ["evidence", "no_invention"]
        out["verdict"] = "REJECT"
        out["next"] = ("This result cites no evidence (no record path, file + line, command output or URL), so it "
                       "FAILS checks 'evidence' and 'no_invention'. The verdict is REJECT. Do not go and check the "
                       "devices yourself. Reply exactly like this, first line first:\n"
                       + REJECT_NO_EVIDENCE + "\n"
                       "Then (with a project) record it: qc action=record verdict=REJECT findings=[...].")
    return out


def qc_record(s: Settings, project: str, item: str, role: str, verdict: str, findings: list[str] | None = None) -> dict:
    v = (verdict or "").upper()
    if v not in VERDICTS:
        raise RoleError(f"verdict must be one of {', '.join(VERDICTS)}")
    if v != "PASS" and not findings:
        raise RoleError("give the findings for anything but PASS")
    pr = Projects(s)
    pdir = pr.find(project)
    pr.log(pdir, "SANITY_AGENT", f"reviewed {item} from {role}",
           v + ("" if not findings else ": " + "; ".join(" ".join(f.split()) for f in findings)))
    return {"project": project, "item": item, "verdict": v}


_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def lesson_add(s: Settings, role: str, title: str, category: str, issue: str, rule: str,
               project: str | None = None) -> dict:
    cat = (category or "").lower()
    if cat not in CATEGORIES:
        raise RoleError(f"category must be one of {', '.join(CATEGORIES)}")
    if not all(x and x.strip() for x in (role, title, issue, rule)):
        raise RoleError("role, title, issue and rule are all needed")
    source = project or "-"
    if project:
        pr = Projects(s)
        pdir = pr.find(project)
        if pr.mode(pdir) == "LOCAL":
            source = "LOCAL project"
            text = " ".join((title, issue, rule))
            leaks = [m for m in _IP.findall(text) if _ok_ip(m)] + [project] * (project.lower() in text.lower())
            if leaks:
                raise RoleError("a LOCAL project's lesson must be generic: no IPs, hostnames or project names "
                                "(found: " + ", ".join(sorted(set(leaks)))[:200] + ")")
    state_dir(s.state_path)
    one = lambda t: " ".join(t.split())  # noqa: E731
    entry = (f"\n## {utc()[:10]} — {one(role).upper()} — {one(title)}\n- Category: {cat}\n"
             f"- Issue: {one(issue)}\n- Rule: {one(rule)}\n- Source project: {source}\n")
    fd = os.open(s.lessons_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, entry.encode())
    finally:
        os.close(fd)
    return {"added": True, "file": str(s.lessons_file)}


def _ok_ip(t: str) -> bool:
    try:
        ipaddress.ip_address(t)
        return True
    except ValueError:
        return False


def lessons_list(s: Settings, role: str | None = None) -> str:
    try:
        text = s.lessons_file.read_text(encoding="utf-8")
    except OSError:
        return "no lessons yet"
    if not role:
        return text
    blocks = re.split(r"(?=^## )", text, flags=re.M)
    return "".join(b for b in blocks if f"— {role.upper()} —" in b) or f"no lessons for {role}"


# ---------------------------------------------------------------------------- skill export (hosts)
def export_skills(dest: Path) -> list[str]:
    """roles/*.md and skills/*.md -> <dest>/<name>/SKILL.md (skill folders of any agent host). Never
    overwrites a SKILL.md that was not written by this export."""
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for src in sorted(list(ROLE_DIR.glob("*.md")) + list(SKILL_DIR.glob("*.md"))):
        meta, body = _split(src.read_text(encoding="utf-8"))
        name = meta.get("name") or f"navigator-{src.stem.lower()}"
        fm = {"name": name, "description": meta.get("description", ""), "version": meta.get("version", "1.0.0"),
              "author": "navigator", "metadata": {"hermes": {"tags": meta.get("tags", [])}, "generated_by": "navigator"}}
        out = dest / name / "SKILL.md"
        if out.exists() and "generated_by: navigator" not in out.read_text(encoding="utf-8"):
            raise RoleError(f"{out} exists and was not written by navigator; not overwritten")
        out.parent.mkdir(exist_ok=True)
        out.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body, encoding="utf-8")
        written.append(str(out))
    return written


def main() -> None:
    ap = argparse.ArgumentParser(prog="navigator-skills", description="NAVIGATOR roles and skills for a host.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    e = sub.add_parser("export", help="write each role / skill as <DIR>/<name>/SKILL.md")
    e.add_argument("dir")
    a = ap.parse_args()
    if a.cmd == "list":
        for r in role_list():
            print(f"{r['role']:18} {r['description']}")
    else:
        for p in export_skills(Path(os.path.expanduser(a.dir))):
            print(p)


if __name__ == "__main__":
    load()
    main()
