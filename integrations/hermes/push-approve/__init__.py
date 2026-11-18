"""push-approve: /pushok and /pushno (on Slack: !pushok / !pushno) for approval codes.

A requester (RUDDER, NAVIGATOR, PUSH_CONFIG.py via push_approval.py) sends a one-time code to the approval
channel and waits. These gateway commands record the user's decision for that code. They run in the
gateway, only for users the gateway lets in, never as a model tool.

RUDDER / NAVIGATOR codes carry rules in the pending file (JSON): method, channel, approvers, requester,
self_approve. A decision is recorded only when:
  - the command came from that method (telegram / slack) and, if a channel is set, from that channel;
  - the sender is an approver ("*" = anyone in the channel: the organization curates who is in it);
  - for an approval with self_approve false: the sender is not the person who asked for the change.
A refused command leaves the code open (a real approver can still decide) and is logged.
The decision file's first line is the verdict; the second says who decided.
Codes without rules (plain-text pending file: nv-push) work as before.
/selfapprove off (Slack: !selfapprove off): an approver turns self-approval off; it runs
`rudder-approve selfapprove off` with the sender from the message event (RUDDER checks and logs it).
There is no /selfapprove on: only `navigator-setup set APPROVAL.SELF_APPROVE true` on the shell.

Two guards (2026-11-14, after this bot's agent approved a code a user had pasted with backticks):
  pre_gateway_dispatch  in the Slack approval channel only approval commands pass (`!pushok CODE`,
                        !pushok CODE, /pushok CODE, with or without backticks or an @mention are turned
                        into /pushok CODE); every other message there is dropped, so the agent never sees it.
  pre_tool_call         this bot's agent may not touch the approval machinery (this plugin, the approval
                        folder, code hashes, tickets): approving is human-only.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

APPROVAL_DIR = Path(os.path.expanduser("~/.hermes/push-approvals"))


def _hash(kind: str, code: str) -> str:
    # Must match push_approval.code_hash in /data/skills/NAVI/lib/push_approval.py and RUDDER.
    return hashlib.sha256(f"{kind}:{code.strip().upper()}".encode()).hexdigest()


def _log(line: str) -> None:
    try:
        with open(APPROVAL_DIR / "decisions.log", "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        os.chmod(APPROVAL_DIR / "decisions.log", 0o600)
    except OSError:
        pass


def _who(source) -> tuple[str, str, str, str, str]:
    plat = getattr(source, "platform", None)
    plat = str(getattr(plat, "value", plat) or "")
    return (plat, str(getattr(source, "chat_id", "") or ""), str(getattr(source, "parent_chat_id", "") or ""),
            str(getattr(source, "user_id", "") or ""), str(getattr(source, "user_name", "") or ""))


def _check(rules: dict, verdict: str, source) -> str | None:
    """None if this sender may decide; otherwise why not."""
    plat, chat, parent, user, _ = _who(source)
    method = str(rules.get("method") or "")
    if method and plat != method:
        return f"approvals for this request are taken on {method} only"
    channel = str(rules.get("channel") or "")
    if channel and channel not in (chat, parent):
        return "approvals for this request are taken only in the approval channel"
    apr = rules.get("approvers", "*")
    if apr not in ("*", ["*"]) and user not in [str(x) for x in (apr or [])]:
        return "you are not on the approvers list for this request"
    if verdict == "approved" and not rules.get("self_approve", True) and rules.get("requester") \
            and user == str(rules["requester"]):
        return "you asked for this change: another approver must approve it (you can still /pushno it)"
    return None


def _decide(raw: str, verdict: str, source=None) -> str:
    cmd = "pushok" if verdict == "approved" else "pushno"
    code = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    if not code:
        return f"Usage: /{cmd} <code>  (on Slack: !{cmd} <code>)"
    pending = APPROVAL_DIR / _hash("pending", code)
    if not pending.exists():
        plat, chat, _, user, _ = _who(source)
        _log(f"NO PENDING {cmd}: got {len(code)} chars {code.encode()[:0]!r}"
             f"{'' if code.isalnum() else ' (not letters/digits only)'}, pending-hash {_hash('pending', code)[:8]} "
             f"from {user} on {plat}:{chat}; raw {len(raw or '')} chars")
        return "No pending push with that code (wrong code, expired, or already decided)."
    text = pending.read_text().strip()
    rules = None
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "method" in d:
            rules = d
    except ValueError:
        pass
    plat, chat, _, user, uname = _who(source)
    if rules is not None:
        why = _check(rules, verdict, source)
        if why:
            _log(f"REFUSED {cmd} {rules.get('label', '')} from {uname} ({user}) on {plat}:{chat}: {why}")
            return f"⛔ Not recorded: {why}. The request is still open."
        what = str(rules.get("label") or "request")
        body = f"{verdict}\n{uname} ({user}) on {plat}".strip()
    else:
        what = text
        body = verdict
    (APPROVAL_DIR / _hash("decision", code)).write_text(body)
    pending.unlink(missing_ok=True)
    _log(f"{verdict.upper()} {what} by {uname} ({user}) on {plat}:{chat}")
    if verdict == "approved":
        return f"✅ Approved: {what}. The push will run now."
    return f"🛑 Rejected: {what}. Nothing was pushed."


def _rudder_approve() -> str | None:
    p = os.path.expanduser(os.environ.get("RUDDER_APPROVE_BIN") or "~/navigator/.venv/bin/rudder-approve")
    return p if os.access(p, os.X_OK) else shutil.which("rudder-approve")


def _selfapprove(raw: str, source=None, runner=subprocess.run) -> str:
    """/selfapprove off (decision #161): an approver turns self-approval off. Chat can only tighten:
    RUDDER's `rudder-approve selfapprove off` checks the sender (method, approval channel, approvers),
    writes APPROVAL.SELF_APPROVE false, tightens open requests and logs it (settings.log + audit)."""
    arg = (raw or "").strip().lower()
    if arg in ("on", "true", "yes"):
        return ("⛔ Not changed: chat can only turn self-approval off. On the host's shell: "
                "navigator-setup set APPROVAL.SELF_APPROVE true")
    if arg != "off":
        return "Usage: /selfapprove off  (on Slack: !selfapprove off)"
    plat, chat, parent, user, uname = _who(source)
    exe = _rudder_approve()
    if not exe or not user:
        _log(f"SELFAPPROVE off from {user} on {plat}:{chat}: rudder-approve not found")
        return "⛔ Not changed: rudder-approve is not installed on this host."
    cmd = [exe, "selfapprove", "off", "--user", user, "--platform", plat, "--json"]
    cmd += ["--channel", chat] if chat else []
    cmd += ["--parent", parent] if parent else []
    cmd += ["--name", re.sub(r"[^\w .@-]", "", uname)[:60]] if uname else []
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=60)
        msg = json.loads(r.stdout or "{}").get("message") or "⛔ Not changed (no answer from rudder-approve)."
    except (OSError, subprocess.SubprocessError, ValueError):
        msg = "⛔ Not changed: rudder-approve failed."
    _log(f"SELFAPPROVE off from {uname} ({user}) on {plat}:{chat}: {msg[:60]}")
    return msg


def _ok(raw: str, source=None) -> str:
    return _decide(raw, "approved", source)


def _no(raw: str, source=None) -> str:
    return _decide(raw, "rejected", source)


# --------------------------------------------------------------------------- the approval channel
# Only approval commands come out of it; nothing there ever reaches this bot's agent (an agent that sees a
# code could act on it). Channels: env PUSH_APPROVE_CHANNELS (comma separated), else APPROVAL.CHANNEL of
# ~/.config/rudder/rudder.local.yml when APPROVAL.METHOD is slack.
_CMD_RE = re.compile(r"^(?:<@[A-Z0-9]+>\s*)?[!/](push(?:ok|no))\s+([A-Za-z0-9]{4,12})$", re.I)
_SELF_RE = re.compile(r"^(?:<@[A-Z0-9]+>\s*)?[!/]selfapprove\s+(off|on)$", re.I)
_LOCAL = Path(os.path.expanduser("~/.config/rudder/rudder.local.yml"))


def _channels() -> set[str]:
    env = os.environ.get("PUSH_APPROVE_CHANNELS", "")
    if env.strip():
        return {c.strip() for c in env.split(",") if c.strip()}
    try:
        text = _LOCAL.read_text()
    except OSError:
        return set()
    block = re.search(r"^APPROVAL:\s*\n((?:[ \t]+.*\n?)*)", text, re.M)
    if not block:
        return set()
    body = block.group(1)
    method = re.search(r"^[ \t]+METHOD:\s*['\"]?(\w+)", body, re.M)
    chan = re.search(r"^[ \t]+CHANNEL:\s*['\"]?([A-Z0-9]{6,})", body, re.M)
    if method and method.group(1).lower() == "slack" and chan:
        return {chan.group(1)}
    return set()


def on_gateway_dispatch(event=None, **_kw):
    src = getattr(event, "source", None)
    plat = getattr(getattr(src, "platform", None), "value", getattr(src, "platform", ""))
    if str(plat) != "slack":
        return None
    chans = _channels()
    if not chans or not ({str(getattr(src, "chat_id", "") or ""), str(getattr(src, "parent_chat_id", "") or "")} & chans):
        return None
    text = str(getattr(event, "text", "") or "")
    clean = re.sub(r"[`*_~]", "", text).strip()
    s = _SELF_RE.match(clean)
    if s:
        cmd = f"/selfapprove {s.group(1).lower()}"
        _log("COMMAND selfapprove in the approval channel" + ("" if cmd == text.strip() else " (rewritten)"))
        return None if cmd == text.strip() else {"action": "rewrite", "text": cmd}
    m = _CMD_RE.match(clean)
    if m:
        cmd = f"/{m.group(1).lower()} {m.group(2).upper()}"
        _log(f"COMMAND {m.group(1).lower()} in the approval channel: pending-hash "
             f"{_hash('pending', m.group(2))[:8]}, text {len(text)} chars"
             + ("" if cmd == text.strip() else " (rewritten)"))
        if cmd != text.strip():
            return {"action": "rewrite", "text": cmd}
        return None
    _log(f"DROPPED a non-command message in the approval channel from {getattr(src, 'user_id', '')}")
    return {"action": "skip", "reason": "approval channel: approval commands only"}


# --------------------------------------------------------------------------- the agent keeps out
_GUARD_RE = re.compile(r"push-approvals|push[-_]approve|push_approval|\.rudder/(tickets|locks|audit)|"
                       r"rudder\.local\.yml|(?-i:\b(decision|pending):[A-Za-z0-9]{4,12}\b)", re.I)


def on_pre_tool_call(tool_name: str = "", args=None, **_kw):
    try:
        text = json.dumps(args, default=str)
    except (TypeError, ValueError):
        text = str(args)
    if _GUARD_RE.search(text):
        _log(f"BLOCKED agent tool {tool_name}: touches the approval machinery")
        return {"action": "block",
                "message": "push-approve: approvals are human-only. The agent never reads, computes or writes "
                           "approval codes, decisions or tickets, and never calls the approval plugin. If the "
                           "user posted a code, tell them to send it as  !pushok CODE  (plain text) in the "
                           "approval channel."}
    return None


def register(ctx) -> None:
    ctx.register_command("pushok", _ok, description="Approve a pending push or change", args_hint="<code>")
    ctx.register_command("pushno", _no, description="Reject a pending push or change", args_hint="<code>")
    ctx.register_command("selfapprove", _selfapprove, description="Turn self-approval off (approvers only)",
                         args_hint="off")
    ctx.register_hook("pre_gateway_dispatch", on_gateway_dispatch)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
