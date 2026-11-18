"""rudder-approve: record a human's /pushok or /pushno for a RUDDER approval code, for any host.

A host (Hermes, OpenClaw, a Teams bot, a Slack app, ...) owns its chat connections. Its own /pushok
handler runs, for the person who typed the command:

    rudder-approve ok CODE --user U123 --name "Pat" --platform slack --channel C0123ABCDEF
    rudder-approve no CODE --user ... --platform telegram --channel -1001234567890
    rudder-approve selfapprove off --user U123 --platform slack --channel C0123ABCDEF
                                   (an approver's /selfapprove off; chat can only tighten, never 'on')

and posts the one line it prints back to that person. Exit code: 0 recorded, 1 refused / no such code,
2 usage or config error. --json prints {"recorded", "verdict", "message"}.

The platform, channel and user MUST come from the host's own message event, never from a model: the
handler is gateway code, not an agent tool. The code itself is the secret: it only ever went to the
approval channel.

Config (required): --config PATH, else $RUDDER_APPROVE_CONFIG, else ~/.config/rudder/approve.yml.
It must be owned by this user and not writable by group or others.

    APPROVAL_DIR: ~/.hermes/push-approvals   # the decision store (RUDDER's APPROVAL.APPROVAL_DIR)
    PLATFORMS: [slack, telegram, teams]      # where approvals may come from (required, at least one)
    CHANNELS:                                # optional per platform: only these chats / channels
      slack: [C0123ABCDEF]
    APPROVERS: "*"                           # "*" or user IDs: a host-wide list, on top of each request's own
    LOG: ~/.rudder/approve.log               # every call, accepted or refused (no codes in it)

Checks, in order (the same as the Hermes push-approve plugin, plus the host gates):
  host:     platform in PLATFORMS; channel in CHANNELS[platform] when set; user in APPROVERS;
  request:  the rules RUDDER wrote in the pending file: method (= platform), channel, approvers,
            self_approve (the requester may not approve their own change, but may still reject it).
A refused call leaves the code open. Never a model tool; nothing here approves on its own.
Keys in this file: none today. Encrypting secrets in host configs is planned (Bitwarden is an option).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import yaml

from .approval import code_hash

DEFAULT_CONFIG = "~/.config/rudder/approve.yml"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,20}$")
CODE_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
ID_RE = re.compile(r"^-?[A-Za-z0-9_.@:+-]{1,80}$")


class ApproveError(Exception):
    """A usage or configuration problem (exit 2)."""


def _ids(v) -> list[str] | str:
    if v in (None, "", "*", ["*"]):
        return "*"
    items = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
    return "*" if not items or "*" in items else items


def load_config(path: str | None = None) -> dict:
    p = Path(os.path.expanduser(path or os.environ.get("RUDDER_APPROVE_CONFIG") or DEFAULT_CONFIG))
    if not p.is_file():
        raise ApproveError(f"config file {p} not found (required: see rudder-approve --help)")
    st = p.stat()
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ApproveError(f"config file {p} must be owned by this user")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ApproveError(f"config file {p} must not be writable by group or others (chmod 600)")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ApproveError(f"config file {p}: not valid YAML ({e.__class__.__name__})") from None
    if not isinstance(raw, dict):
        raise ApproveError(f"config file {p}: expected a mapping")
    cfg = {str(k).upper(): v for k, v in raw.items()}
    plats = [str(x).strip().lower() for x in (cfg.get("PLATFORMS") or []) if str(x).strip()]
    if not plats or any(not NAME_RE.match(x) for x in plats):
        raise ApproveError("PLATFORMS: list at least one platform name (slack, telegram, teams, ...)")
    chans = cfg.get("CHANNELS") or {}
    if not isinstance(chans, dict):
        raise ApproveError("CHANNELS: a mapping platform -> list of channel IDs")
    return {"path": str(p), "approval_dir": Path(os.path.expanduser(str(cfg.get("APPROVAL_DIR")
                                                                          or "~/.hermes/push-approvals"))),
            "platforms": plats,
            "channels": {str(k).lower(): [str(x) for x in (v if isinstance(v, list) else [v])]
                         for k, v in chans.items() if v},
            "approvers": _ids(cfg.get("APPROVERS", "*")),
            "log": Path(os.path.expanduser(str(cfg.get("LOG") or "~/.rudder/approve.log")))}


def check_host(cfg: dict, platform: str, channel: str, user: str) -> str | None:
    if platform not in cfg["platforms"]:
        return f"approvals are not taken on {platform} here"
    allowed = cfg["channels"].get(platform)
    if allowed and channel not in allowed:
        return "approvals are taken only in the approval channel"
    if cfg["approvers"] != "*" and user not in cfg["approvers"]:
        return "you are not on the approvers list"
    return None


def check_rules(rules: dict, verdict: str, platform: str, channel: str, user: str,
                parent: str = "") -> str | None:
    """The push-approve plugin's _check, for a host that is not Hermes."""
    method = str(rules.get("method") or "")
    if method and platform != method:
        return f"approvals for this request are taken on {method} only"
    want = str(rules.get("channel") or "")
    if want and want not in (channel, parent):
        return "approvals for this request are taken only in the approval channel"
    apr = rules.get("approvers", "*")
    if apr not in ("*", ["*"]) and user not in [str(x) for x in (apr or [])]:
        return "you are not on the approvers list for this request"
    if verdict == "approved" and not rules.get("self_approve", True) and rules.get("requester") \
            and user == str(rules["requester"]):
        return "you asked for this change: another approver must approve it (you can still /pushno it)"
    return None


def _log(cfg: dict, line: str) -> None:
    try:
        cfg["log"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(cfg["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def decide(cfg: dict, verdict: str, code: str, *, user: str, platform: str, channel: str = "",
           name: str = "", parent: str = "") -> tuple[bool, str]:
    """-> (recorded, message for the person). Never raises for a refusal."""
    cmd = "pushok" if verdict == "approved" else "pushno"
    code = (code or "").strip()
    who = f"{name} ({user})".strip() if name else user
    where = f"{platform}:{channel}" if channel else platform
    if not CODE_RE.match(code):
        _log(cfg, f"BAD CODE {cmd} from {who} on {where}")
        return False, f"Usage: /{cmd} <code>"
    why = check_host(cfg, platform, channel, user)
    if why:
        _log(cfg, f"REFUSED {cmd} from {who} on {where}: {why}")
        return False, f"⛔ Not recorded: {why}. The request is still open."
    pending = cfg["approval_dir"] / code_hash("pending", code)
    if not pending.is_file():
        _log(cfg, f"NO PENDING {cmd} (pending-hash {code_hash('pending', code)[:8]}) from {who} on {where}")
        return False, "No pending request with that code (wrong code, expired, or already decided)."
    text = pending.read_text().strip()
    rules = None
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "method" in d:
            rules = d
    except ValueError:
        pass
    if rules is None:
        _log(cfg, f"REFUSED {cmd} from {who} on {where}: a request without rules")
        return False, "⛔ Not recorded: this request has no approval rules; use the host it was sent to."
    why = check_rules(rules, verdict, platform, channel, user, parent)
    label = str(rules.get("label") or "request")
    if why:
        _log(cfg, f"REFUSED {cmd} {label} from {who} on {where}: {why}")
        return False, f"⛔ Not recorded: {why}. The request is still open."
    try:
        fd = os.open(cfg["approval_dir"] / code_hash("decision", code), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False, "That request was already decided."
    try:
        os.write(fd, f"{verdict}\n{who} on {platform}".encode())
    finally:
        os.close(fd)
    pending.unlink(missing_ok=True)
    _log(cfg, f"{verdict.upper()} {label} by {who} on {where}")
    if verdict == "approved":
        return True, f"✅ Approved: {label}. RUDDER applies it now (or at its scheduled time)."
    return True, f"🛑 Rejected: {label}. Nothing is changed."


def _selfapprove(a) -> int:
    """rudder-approve selfapprove off: the same sender checks as a /pushok, then selfapprove.turn_off.
    With an approve.yml (a host handler's config) its host gates apply too; the Hermes plugin calls this
    without one. 'on' is always refused: loosening is shell-only."""
    from . import selfapprove

    def say(ok: bool, msg: str) -> int:
        print(json.dumps({"changed": ok, "message": msg}) if a.json else msg)
        return 0 if ok else 1

    for field in ("user", "platform", "channel", "parent"):
        v = getattr(a, field)
        if v and not ID_RE.match(v):
            print(f"rudder-approve: --{field} has characters an ID cannot have", file=sys.stderr)
            return 2
    if a.state == "on":
        return say(False, "⛔ Not changed: chat can only turn self-approval off. On the host's shell: "
                          f"{selfapprove.ON_HINT}")
    platform, name = a.platform.lower(), re.sub(r"[^\w .@-]", "", a.name)[:60]
    cfg = None
    conf = Path(os.path.expanduser(a.config or os.environ.get("RUDDER_APPROVE_CONFIG") or DEFAULT_CONFIG))
    if a.config or conf.is_file():
        try:
            cfg = load_config(a.config)
        except ApproveError as e:
            print(f"rudder-approve: {e}", file=sys.stderr)
            return 2
    if cfg:
        why = check_host(cfg, platform, a.channel, a.user)
        if why:
            _log(cfg, f"REFUSED selfapprove off from {name or a.user} ({a.user}) on {platform}:{a.channel}: {why}")
            return say(False, f"⛔ Not changed: {why}.")
    ok, msg = selfapprove.turn_off(user=a.user, platform=platform, channel=a.channel, parent=a.parent, name=name)
    if cfg:
        _log(cfg, f"SELFAPPROVE OFF by {name or a.user} ({a.user}) on {platform}:{a.channel}: "
                  + ("done" if ok else "refused"))
    return say(ok, msg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rudder-approve", description=__doc__.split("\n\n")[0],
                                 epilog="See the module help: python -m rudder_devices.approve_cli --help",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for verb in ("ok", "no"):
        p = sub.add_parser(verb, help=("approve" if verb == "ok" else "reject") + " the request with this code")
        p.add_argument("code")
        p.add_argument("--user", required=True, help="the sender's user ID, from the host's message event")
        p.add_argument("--platform", required=True, help="slack, telegram, teams, ... (from the host)")
        p.add_argument("--channel", default="", help="the chat / channel ID the command came from")
        p.add_argument("--parent", default="", help="the parent channel of a thread, if the host has one")
        p.add_argument("--name", default="", help="the sender's display name (for the record)")
    sub.add_parser("check", help="validate the config file")
    p = sub.add_parser("selfapprove", help="'off': an approver turns self-approval off (chat can only tighten)")
    p.add_argument("state", choices=("off", "on"))
    p.add_argument("--user", required=True, help="the sender's user ID, from the host's message event")
    p.add_argument("--platform", required=True, help="slack, telegram, teams, ... (from the host)")
    p.add_argument("--channel", default="", help="the chat / channel ID the command came from")
    p.add_argument("--parent", default="", help="the parent channel of a thread, if the host has one")
    p.add_argument("--name", default="", help="the sender's display name (for the record)")
    for p in [ap, *sub.choices.values()]:
        p.add_argument("--config", help=f"YAML config (default $RUDDER_APPROVE_CONFIG or {DEFAULT_CONFIG})")
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "selfapprove":
        return _selfapprove(a)
    try:
        cfg = load_config(a.config)
    except ApproveError as e:
        print(json.dumps({"recorded": False, "error": str(e)}) if a.json else f"rudder-approve: {e}",
              file=sys.stdout if a.json else sys.stderr)
        return 2
    if a.cmd == "check":
        out = {k: (str(v) if isinstance(v, Path) else v) for k, v in cfg.items()}
        print(json.dumps(out, indent=None if a.json else 2))
        return 0
    for field in ("user", "platform", "channel", "parent"):
        v = getattr(a, field)
        if v and not ID_RE.match(v):
            print(f"rudder-approve: --{field} has characters an ID cannot have", file=sys.stderr)
            return 2
    verdict = "approved" if a.cmd == "ok" else "rejected"
    ok, msg = decide(cfg, verdict, a.code, user=a.user, platform=a.platform.lower(), channel=a.channel,
                     name=re.sub(r"[^\w .@-]", "", a.name)[:60], parent=a.parent)
    print(json.dumps({"recorded": ok, "verdict": verdict if ok else None, "message": msg}) if a.json else msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
