"""/selfapprove off: an approver turns self-approval off from the approval channel (decision #161).

Self-approval (APPROVAL.SELF_APPROVE) is on by default: the person who asked for a change may approve
it. The interview no longer asks; it prints a note. From chat it can only be TIGHTENED:

    /selfapprove off      (Slack: !selfapprove off)  in the approval channel, by an approver

The approval handler (the Hermes push-approve plugin, or a host's handler through
`rudder-approve selfapprove off --user U --platform P --channel C`) calls turn_off(). It checks the sender
the way a /pushok is checked (platform = APPROVAL.METHOD, the approval channel, APPROVAL.APPROVERS), then:
  - writes APPROVAL.SELF_APPROVE: false into the host file (rudder.local.yml, backed up first);
  - tightens the requests that are open now (their pending rules get self_approve false), so a
    requester cannot still approve a change opened a minute before;
  - logs the change in settings.log and RUDDER's audit log, with who sent it and from where;
  - returns the confirmation for the approval channel, and posts it to the chat thread of every open
    change that came from a chat (reply_to), so both channels see it.
There is no "/selfapprove on". Loosening is shell-only and keeps the test-code rule of every approval change:
    navigator-setup set APPROVAL.SELF_APPROVE true
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml

from . import approval
from . import config as rconfig

ON_HINT = "navigator-setup set APPROVAL.SELF_APPROVE true"
NOTE = ("Self-approval is on: the person who asks for a change may approve it (fine for a lab or a small "
        "team). To turn it off, an approver sends `/selfapprove off` in the approval channel "
        "(on Slack: `!selfapprove off`). Only the host's shell turns it back on: "
        f"`{ON_HINT}`.")


def _local_file() -> Path:
    return Path(os.path.expanduser(os.environ.get("RUDDER_LOCAL") or "~/.config/rudder/rudder.local.yml"))


def check_sender(appr: dict, platform: str, channel: str, user: str, parent: str = "") -> str | None:
    """None if this sender may turn self-approval off: an approver, in the approval channel."""
    method = str(appr.get("METHOD") or "telegram").lower()
    if platform != method:
        return f"approval settings are changed on {method} only"
    want = str(appr.get("CHANNEL") or "").strip()
    want = "" if want.lower() == "hermes" else want
    if want and want not in (channel, parent):
        return "send it in the approval channel"
    if not want and method in approval.CHANNEL_REQUIRED:
        return "there is no approval channel set"
    apr = approval.approvers(appr)
    if apr != "*" and user not in apr:
        return "you are not on the approvers list"
    if not user:
        return "the sender is unknown"
    return None


def _write_local(off_by: str) -> Path | None:
    """APPROVAL.SELF_APPROVE: false in the host file, other keys kept. Returns the backup (or None)."""
    from navigator_engineer import setup as nsetup      # one writer for settings files (backup + mode 600)
    p = _local_file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (yaml.safe_load(p.read_text()) or {}) if p.is_file() else {}
    bak = nsetup._backup(p)
    appr = dict(data.get("APPROVAL") or {})
    appr["SELF_APPROVE"] = False
    data["APPROVAL"] = appr
    head = ("# Host-specific RUDDER / NAVIGATOR settings, merged over rudder.yml (sections key by key).\n"
            "# Written by navigator-setup; change with: navigator-setup set APPROVAL.<KEY> <value>\n"
            f"# APPROVAL.SELF_APPROVE false: /selfapprove off by {off_by} ({time.strftime('%Y-%m-%d %H:%M')})\n")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(head + yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return bak


def tighten_pending(directory: Path) -> int:
    """Open requests with rules get self_approve false. Plain-text (nv-push) requests are left alone.
    Each file is replaced atomically, keeping its mode; returns how many were tightened."""
    n = 0
    if not directory.is_dir():
        return 0
    for f in directory.iterdir():
        if not f.is_file() or len(f.name) != 64:
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or "method" not in d or d.get("self_approve") is False:
            continue
        d["self_approve"] = False
        tmp = f.with_name(f".{f.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(d) + "\n").encode())
        finally:
            os.close(fd)
        if f.exists():                     # decided meanwhile: the pending file is gone, keep it gone
            os.replace(tmp, f)
            n += 1
        else:
            tmp.unlink(missing_ok=True)
    return n


def _audit(state_path: Path, line: str) -> None:
    state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_path / "audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"{time.strftime('%Y-%m-%d %H:%M:%S')}\tSETTINGS\t-\t{line}\n".encode())
    finally:
        os.close(fd)


def _open_threads(s) -> list[tuple[str, str]]:
    """(ticket id, reply_to) of the open changes that came from a chat."""
    out = []
    tdir = Path(s.state_path) / "tickets"
    if not tdir.is_dir():
        return out
    for f in sorted(tdir.glob("*.json")):
        try:
            t = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if t.get("state") in ("waiting_approval", "approved", "scheduled") and t.get("reply_to"):
            out.append((str(t.get("id") or f.stem), str(t["reply_to"])))
    return out


def turn_off(*, user: str, platform: str, channel: str = "", parent: str = "", name: str = "",
             settings=None, runner=None) -> tuple[bool, str]:
    """-> (changed or already off, the message for the approval channel). Never loosens anything."""
    s = settings or rconfig.load()
    appr = dict(s.approval or {})
    who = f"{name} ({user})" if name else user
    where = f"{platform}:{channel}" if channel else platform
    why = check_sender(appr, platform, channel, user, parent)
    if why:
        _audit(Path(s.state_path), f"REFUSED /selfapprove off from {who} on {where}: {why}")
        return False, f"⛔ Not changed: {why}."
    adir = Path(os.path.expanduser(str(appr.get("APPROVAL_DIR", "~/.hermes/push-approvals"))))
    was = str(appr.get("SELF_APPROVE", True)).strip().lower() not in ("0", "false", "no", "off")
    if was:
        _write_local(who)
        from navigator_engineer import setup as nsetup
        nsetup._settings_log(f"APPROVAL.SELF_APPROVE: True -> False (/selfapprove off by {who} on {where})")
    n = tighten_pending(adir)
    _audit(Path(s.state_path), f"SELF_APPROVE {'on -> off' if was else 'already off'} by {who} on {where}"
                               f"; {n} open request(s) tightened")
    text = ("🔒 Self-approval is now OFF" if was else "🔒 Self-approval was already off") + \
           f" (by {who}): a requester can no longer approve their own change; another approver must." + \
           (f" {n} open request(s) now follow this rule." if n else "") + \
           f" Only the host's shell turns it back on: {ON_HINT}"
    if was:
        try:
            ch = approval.HermesChannel(appr, s.consumer, **({"runner": runner} if runner else {}))
            for tid, target in _open_threads(s):
                if ch.reply_ok(target):
                    try:
                        ch.reply(target, f"🔒 Change {tid}: self-approval was turned off in the approval "
                                         "channel; another approver must approve it.")
                    except approval.ApprovalError:
                        pass
        except approval.ApprovalError:
            pass
    return True, text
