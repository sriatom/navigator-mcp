"""navigator-setup: the host settings of NAVIGATOR, asked once and changed later from the host's shell.

  navigator-setup                       the interview (first install, or to go through everything again)
  navigator-setup show                  current values and where they come from
  navigator-setup set KEY VALUE [KEY VALUE ...]
  navigator-setup add KEY VALUE         add to a list (APPROVAL.APPROVERS, APPROVAL.SHIP_ENGINEERS, ...)
  navigator-setup remove KEY VALUE
  navigator-setup test-approval         send a test code the current way and wait for the /pushok
  navigator-setup tls init|renew|show   the local CA and the HTTPS certificate of the HTTP MCP servers
  navigator-setup user add|reset|role|remove|list   HTTP users (admin | engineer), random passwords
  navigator-setup http show             URLs (8791-8793) and how to connect a remote client
  APPROVE.* keys (set / add / remove): rudder-approve's config (~/.config/rudder/approve.yml) for hosts whose
  own /pushok handler calls rudder-approve: APPROVE.PLATFORMS, APPROVE.CHANNELS (platform=ID, ...),
  APPROVE.APPROVERS, APPROVE.APPROVAL_DIR, APPROVE.LOG. An empty APPROVE.PLATFORMS turns it off.
  navigator-setup recovery init|check|refresh   the recovery key of the CREDENTIALS key (48 digits, shown once)
  navigator-setup backup [--out DIR]    one encrypted file with every setting, key, inventory and state
  navigator-setup restore FILE [--list] [--to DIR]   needs the file and the recovery key only

Keys: PROJECT_ROOT (required), SKILLS_ROOT (optional), APPROVAL.METHOD, APPROVAL.BOT, APPROVAL.CHANNEL,
APPROVAL.APPROVERS, APPROVAL.SELF_APPROVE, APPROVAL.SHIP_ENGINEERS, APPROVAL.REPLY_PROFILES, APPROVAL.TTL_MIN,
APPROVAL.PAGES_KEEP_DAYS, APPROVAL.CLEANUP_AT, MODELS.HOST (cloud | local), MODELS.LOCAL_URL, MODELS.LOCAL_MODEL,
MODELS.TIMEOUT_SEC. A local model is saved only if its URL is a local address and it answers with that model.
VALIDATION.MODE (none | batfish | netclaw), VALIDATION.HOST, VALIDATION.PORT,
VALIDATION.ON_UNREACHABLE (block | warn), VALIDATION.POST_PUSH, VALIDATION.TIMEOUT: change validation
(rudder_devices/validation.py). Saved only after the validator answered a test snapshot.
SERVICENOW.MODE (off | link | gate), SERVICENOW.URL, SERVICENOW.CREDENTIAL (an ATLAS credential tag),
SERVICENOW.VERIFY_TLS, SERVICENOW.CLOSE_STATE (rudder_devices/servicenow.py). Saved only after the login
read change_request.

This is a shell command on purpose, never a chat tool: if the agent could change where approvals go, it
could send them to itself. Paths are checked (PROJECT_ROOT must be readable and writable; SKILLS_ROOT
readable). A change to how approvals work (METHOD, BOT, CHANNEL, APPROVERS, SELF_APPROVE, SHIP_ENGINEERS)
is saved only after a test code sent the NEW way was approved there. Every file is backed up first and
every change is logged in <STATE_PATH>/settings.log.

Files: $NAVIGATOR_BOOTSTRAP or ~/.config/navigator/navigator.yml (PROJECT_ROOT, SKILLS_ROOT) and
$RUDDER_LOCAL or ~/.config/rudder/rudder.local.yml (APPROVAL, one section for the whole host).
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

import yaml

from rudder_devices import approval, retention
from rudder_devices import validation as rvalidation

NAV_KEYS = ("PROJECT_ROOT", "SKILLS_ROOT")
LEGACY = {"PROJECT_ROOT": "WORKSPACE", "SKILLS_ROOT": "EXTRA_SKILLS"}
APPROVAL_KEYS = ("METHOD", "BOT", "CHANNEL", "APPROVERS", "SELF_APPROVE", "SHIP_ENGINEERS",
                 "REPLY_PROFILES", "TTL_MIN", "MENTION", "WEB_BIND", "WEB_URL", "WEB_TLS", "WEB_KEEP_MIN",
                 "PAGES_KEEP_DAYS", "CLEANUP_AT")
BIND_RE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|localhost|[A-Za-z0-9.-]+):(\d{2,5})$")
LIST_KEYS = ("APPROVERS", "SHIP_ENGINEERS", "REPLY_PROFILES")
TESTED = ("METHOD", "BOT", "CHANNEL", "APPROVERS", "SELF_APPROVE", "SHIP_ENGINEERS")
MODEL_KEYS = ("HOST", "LOCAL_URL", "LOCAL_MODEL", "TIMEOUT_SEC")
APPROVE_KEYS = ("PLATFORMS", "CHANNELS", "APPROVERS", "APPROVAL_DIR", "LOG")   # rudder-approve (approve_cli.py)
APPROVE_LISTS = ("PLATFORMS", "CHANNELS", "APPROVERS")
VALIDATION_KEYS = ("MODE", "HOST", "PORT", "ON_UNREACHABLE", "POST_PUSH", "TIMEOUT")
SERVICENOW_KEYS = ("MODE", "URL", "CREDENTIAL", "VERIFY_TLS", "CLOSE_STATE")     # rudder_devices/servicenow.py
PLUGIN_METHODS = ("telegram", "slack")       # handled by the Hermes push-approve plugin; others need rudder-approve    # MODELS.<key>; stored nested (models.py)
USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{2,64}$")
PROFILE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class SetupError(Exception):
    pass


def _p(v) -> Path:
    return Path(os.path.expanduser(str(v)))


def nav_file() -> Path:
    return _p(os.environ.get("NAVIGATOR_BOOTSTRAP") or "~/.config/navigator/navigator.yml")


def rudder_file() -> Path:
    return _p(os.environ.get("RUDDER_LOCAL") or "~/.config/rudder/rudder.local.yml")


def hermes_home() -> Path:
    return _p(os.environ.get("HERMES_ROOT") or "~/.hermes")


def _load(p: Path) -> dict:
    return (yaml.safe_load(p.read_text()) or {}) if p.is_file() else {}


# ------------------------------------------------------------------ checks
def check_project_root(v) -> Path:
    p = _p(v)
    if not str(v or "").strip():
        raise SetupError("PROJECT_ROOT is required (the folder where project folders are kept)")
    if not p.is_absolute():
        raise SetupError(f"PROJECT_ROOT must be an absolute path: {v}")
    if not p.is_dir():
        raise SetupError(f"PROJECT_ROOT {p} does not exist (create it first, it holds data only)")
    if not os.access(p, os.R_OK | os.W_OK | os.X_OK):
        raise SetupError(f"PROJECT_ROOT {p} is not readable and writable for {getpass.getuser()}")
    probe = p / f".navigator-setup-{secrets.token_hex(4)}"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise SetupError(f"PROJECT_ROOT {p}: cannot write a file there ({e.strerror})") from None
    return p


def check_skills_root(v) -> tuple[Path | None, int]:
    if not str(v or "").strip():
        return None, 0
    p = _p(v)
    if not p.is_absolute():
        raise SetupError(f"SKILLS_ROOT must be an absolute path: {v}")
    if not p.is_dir():
        raise SetupError(f"SKILLS_ROOT {p} does not exist")
    if not os.access(p, os.R_OK | os.X_OK):
        raise SetupError(f"SKILLS_ROOT {p} is not readable for {getpass.getuser()}")
    n = sum(1 for s in p.glob("*/SKILL.md") if os.access(s, os.R_OK))
    if n == 0:
        raise SetupError(f"SKILLS_ROOT {p} has no <skill>/SKILL.md folders")
    return p, n


def _list(v) -> list[str]:
    if v in (None, ""):
        return []
    items = v if isinstance(v, list) else str(v).split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def normalize_approval(cfg: dict) -> dict:
    """Check an APPROVAL section; returns it cleaned. Raises SetupError on anything wrong."""
    out = dict(cfg)
    m = str(out.get("METHOD") or "telegram").lower()
    if m not in approval.METHODS:
        raise SetupError(f"APPROVAL.METHOD must be one of {', '.join(approval.METHODS)} (api is not built yet)")
    out["METHOD"] = m
    bot = str(out.get("BOT") or "").strip()
    if not bot:
        prof = {str(k).upper(): v for k, v in (out.get("PROFILES") or {}).items()}
        bot = str(next(iter(prof.values()), "")) if prof else ""
    if not PROFILE_RE.match(bot):
        raise SetupError("APPROVAL.BOT: the approval bot's Hermes profile name (e.g. navi)")
    if not (hermes_home() / "profiles" / bot).is_dir() and bot != "default":
        raise SetupError(f"APPROVAL.BOT: there is no Hermes profile {bot!r} on this host")
    out["BOT"] = bot
    replies = [x.lower() for x in _list(out.get("REPLY_PROFILES"))]
    if bot in replies:
        raise SetupError(f"APPROVAL.BOT {bot} is also a chat bot (REPLY_PROFILES): the bot that asks for a "
                         f"change must never be the one that approves it")
    out["REPLY_PROFILES"] = replies
    chan = str(out.get("CHANNEL") or "").strip()
    chan = "" if chan.lower() == "hermes" else chan
    if m == "slack" and not re.match(r"^[CG][A-Z0-9]{6,}$", chan):
        raise SetupError("APPROVAL.CHANNEL: the Slack channel ID (C... or G..., from the channel's details)")
    if chan and not approval.CHAT_ID_RE.match(chan):
        raise SetupError("APPROVAL.CHANNEL must be a channel or chat ID")
    out["CHANNEL"] = chan
    apr = approval.approvers(out)
    if apr != "*":
        bad = [x for x in apr if not USER_ID_RE.match(x)]
        if bad:
            raise SetupError(f"APPROVAL.APPROVERS: not user IDs: {', '.join(bad)} (or \"*\" = anyone in the channel)")
    out["APPROVERS"] = apr
    sa = str(out.get("SELF_APPROVE", True)).strip().lower()
    if sa not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
        raise SetupError("APPROVAL.SELF_APPROVE: true or false")
    out["SELF_APPROVE"] = sa in ("true", "yes", "1", "on")
    ship = _list(out.get("SHIP_ENGINEERS"))
    bad = [x for x in ship if not USER_ID_RE.match(x) or x == "*"]
    if bad:
        raise SetupError(f"APPROVAL.SHIP_ENGINEERS: user IDs only (never \"*\"): {', '.join(bad)}")
    out["SHIP_ENGINEERS"] = ship
    try:
        ttl = int(out.get("TTL_MIN", 30))
    except (TypeError, ValueError):
        raise SetupError("APPROVAL.TTL_MIN: minutes, a number") from None
    if not 5 <= ttl <= 1440:
        raise SetupError("APPROVAL.TTL_MIN: between 5 and 1440 minutes")
    out["TTL_MIN"] = ttl
    bind = str(out.get("WEB_BIND") or "").strip()
    if bind:
        m = BIND_RE.match(bind)
        if not m or not 1024 <= int(m.group(3)) <= 65535:
            raise SetupError("APPROVAL.WEB_BIND: host:port to serve change pages on, e.g. 192.0.2.240:8790 "
                             "(port 1024-65535); empty = no pages")
        if m.group(1) in ("0.0.0.0", "::"):
            raise SetupError("APPROVAL.WEB_BIND: give one address (e.g. the LAN address), not 0.0.0.0")
    out["WEB_BIND"] = bind
    url = str(out.get("WEB_URL") or "").strip().rstrip("/")
    if url and not re.match(r"^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?$", url):
        raise SetupError("APPROVAL.WEB_URL: the base address the links use, e.g. http://navhost.example:8790")
    out["WEB_URL"] = url
    from rudder_devices import web as rweb
    mode = str(out.get("WEB_TLS") or "auto").strip().lower()
    if mode not in rweb.TLS_MODES:
        raise SetupError("APPROVAL.WEB_TLS: auto (HTTPS when the local certificate exists), on, or off")
    out["WEB_TLS"] = mode
    if bind and mode != "off":
        from navigator_gate import tls as gtls
        files = rweb.tls_files(out)
        if mode == "on" and not files:
            raise SetupError("APPROVAL.WEB_TLS on: there is no certificate yet (navigator-setup tls init)")
        if files:
            names = gtls.info().get("names") or []
            host = bind.rpartition(":")[0]
            if host not in names:
                raise SetupError(f"APPROVAL.WEB_BIND {host} is not in the certificate ({', '.join(names)}): "
                                 f"navigator-setup tls renew --name {host}")
            if url.startswith("http://"):
                raise SetupError("APPROVAL.WEB_URL starts with http:// but the pages are served over HTTPS")
    try:
        out["WEB_KEEP_MIN"] = max(0, min(int(out.get("WEB_KEEP_MIN", 60)), 1440))
    except (TypeError, ValueError):
        raise SetupError("APPROVAL.WEB_KEEP_MIN: minutes, a number") from None
    try:
        days = int(out.get("PAGES_KEEP_DAYS", retention.DEFAULT_DAYS))
    except (TypeError, ValueError):
        raise SetupError("APPROVAL.PAGES_KEEP_DAYS: days, a number (default 90)") from None
    if not 1 <= days <= 3650:
        raise SetupError("APPROVAL.PAGES_KEEP_DAYS: between 1 and 3650 days")
    out["PAGES_KEEP_DAYS"] = days
    at = str(out.get("CLEANUP_AT", retention.DEFAULT_AT) or "").strip().lower()
    if at in ("off", "none", "-"):
        at = "off"
    elif not retention.AT_RE.match(at):
        raise SetupError("APPROVAL.CLEANUP_AT: the daily clean-up time HH:MM (local, e.g. 02:00), or off")
    else:
        h, mi = at.split(":")
        at = f"{int(h):02d}:{mi}"
    out["CLEANUP_AT"] = at
    mention = str(out.get("MENTION") or "").strip()
    if mention and not re.match(r"^@[\w .-]{1,40}$", mention):
        raise SetupError("APPROVAL.MENTION: how the approval bot is @mentioned, e.g. @NAVI")
    out["MENTION"] = mention
    out.pop("PROFILES", None) if out.get("BOT") else None
    if str(out.get("TRANSPORT", "hermes")).lower() != "hermes":
        raise SetupError("APPROVAL.TRANSPORT: only hermes is built")
    out.setdefault("TRANSPORT", "hermes")
    return out


# ------------------------------------------------------------------ models (docs/LOCAL_MODELS.md)
def flat_models(m: dict) -> dict:
    """navigator.yml's nested MODELS block as the flat setup keys."""
    m = {str(k).upper(): v for k, v in (m or {}).items()}
    loc = {str(k).upper(): v for k, v in (m.get("LOCAL") or {}).items()}
    return {"HOST": m.get("HOST") or "cloud", "LOCAL_URL": loc.get("URL") or "",
            "LOCAL_MODEL": loc.get("MODEL") or "", "TIMEOUT_SEC": loc.get("TIMEOUT_SEC", 300),
            "_LOCAL": loc}


def normalize_models(flat: dict) -> dict:
    """Check the flat keys and return the nested MODELS block (other LOCAL keys, e.g. ALLOW_HOSTS, are kept)."""
    from urllib.parse import urlparse
    from .models import _resolves_local
    host = str(flat.get("HOST") or "cloud").strip().lower()
    if host not in ("cloud", "local"):
        raise SetupError("MODELS.HOST: cloud or local (the model your MCP host itself runs)")
    url = str(flat.get("LOCAL_URL") or "").strip().rstrip("/")
    model = str(flat.get("LOCAL_MODEL") or "").strip()
    try:
        tmo = int(flat.get("TIMEOUT_SEC") or 300)
    except (TypeError, ValueError):
        raise SetupError("MODELS.TIMEOUT_SEC: seconds, a number (default 300)") from None
    if not 10 <= tmo <= 3600:
        raise SetupError("MODELS.TIMEOUT_SEC: between 10 and 3600 seconds")
    local = dict(flat.get("_LOCAL") or {})
    if url:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise SetupError("MODELS.LOCAL_URL: http(s)://host:port/v1 (Ollama: http://127.0.0.1:11434/v1)")
        if not _resolves_local(u.hostname, local.get("ALLOW_HOSTS") or []):
            raise SetupError(f"MODELS.LOCAL_URL: {u.hostname} is not a local address (loopback, private or "
                             "link-local); a LOCAL project's data must not leave your network")
        if not model:
            raise SetupError("MODELS.LOCAL_MODEL: the model's name on that endpoint (e.g. qwen3.5:9b)")
    if host == "local" and not url:
        raise SetupError("MODELS.HOST local needs MODELS.LOCAL_URL and MODELS.LOCAL_MODEL")
    local.update(URL=url, MODEL=model if url else "", TIMEOUT_SEC=tmo)
    for k, v in (("API_KEY_ENV", ""), ("MAX_INPUT_CHARS", 60000), ("ALLOW_HOSTS", [])):
        local.setdefault(k, v)
    return {"HOST": host, "LOCAL": local}


def check_local_model(block: dict, client=None) -> str:
    """The endpoint must answer and list the model. Returns a one-line result; raises SetupError."""
    from . import config as nconfig
    from .models import LocalModel
    st = nconfig.Settings(workspace=Path("."), state_path=Path("."), models=block)
    st_out = LocalModel(st, client=client).status()["local"]
    if not st_out.get("reachable"):
        raise SetupError(f"MODELS.LOCAL_URL {st_out.get('url')}: no answer ({st_out.get('error', 'unreachable')}); "
                         "nothing was saved")
    if not st_out.get("has_model"):
        have = ", ".join(st_out.get("models") or []) or "none"
        raise SetupError(f"MODELS.LOCAL_MODEL {st_out.get('model')}: not on that endpoint (it has: {have}); "
                         "nothing was saved")
    return f"local model {st_out['model']} at {st_out['url']}: answers ({st_out.get('seconds')} s)"


def write_models(block: dict) -> None:
    """Replace navigator.yml's MODELS block (the key and its indented lines), keeping everything else."""
    p = nav_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text().splitlines() if p.is_file() else []
    _backup(p)
    out, skip = [], False
    for ln in lines:
        if re.match(r"^MODELS:", ln):
            skip = True
            continue
        if skip and (ln.startswith((" ", "\t")) or not ln.strip()):
            continue
        skip = False
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    dump = yaml.safe_dump({"MODELS": block}, sort_keys=False, default_flow_style=False).rstrip().splitlines()
    out += ["", "# Models (docs/LOCAL_MODELS.md; navigator-setup). The host's model thinks; NAVIGATOR calls only the local one."]
    out += dump
    p.write_text("\n".join(out) + "\n")


# ------------------------------------------------------------------ rudder-approve (approve_cli.py)
def approve_file() -> Path:
    from rudder_devices import approve_cli
    return _p(os.environ.get("RUDDER_APPROVE_CONFIG") or approve_cli.DEFAULT_CONFIG)


def _channels(v) -> dict:
    """{"slack": ["C1"], ...} from a mapping, or from "slack=C1,teams=19:x@thread" (repeat a platform for more)."""
    if isinstance(v, dict):
        return {str(k).lower(): [str(x) for x in (x if isinstance(x, list) else [x])] for k, x in v.items() if x}
    out: dict = {}
    for item in _list(v):
        plat, sep, cid = item.partition("=")
        if not sep or not plat.strip() or not cid.strip():
            raise SetupError("APPROVE.CHANNELS: platform=ID pairs separated by commas, e.g. "
                             "slack=C0123ABCDEF,teams=19:abc@thread.tacv2")
        out.setdefault(plat.strip().lower(), [])
        if cid.strip() not in out[plat.strip().lower()]:
            out[plat.strip().lower()].append(cid.strip())
    return out


def _channels_text(ch: dict) -> str:
    return ",".join(f"{p}={c}" for p, ids in (ch or {}).items() for c in ids)


def current_approve() -> dict:
    """approve.yml as the setup keys ({} when there is none)."""
    p = approve_file()
    if not p.is_file():
        return {}
    raw = {str(k).upper(): v for k, v in (_load(p) or {}).items()}
    return {"PLATFORMS": [str(x).lower() for x in raw.get("PLATFORMS") or []],
            "CHANNELS": _channels(raw.get("CHANNELS") or {}),
            "APPROVERS": raw.get("APPROVERS", "*"),
            "APPROVAL_DIR": raw.get("APPROVAL_DIR") or "", "LOG": raw.get("LOG") or ""}


def normalize_approve(cfg: dict, appr: dict) -> dict | None:
    """Checked approve.yml content; None = turned off (no platforms)."""
    from rudder_devices import approve_cli
    plats = [x.strip().lower() for x in _list(cfg.get("PLATFORMS")) if x.strip()]
    if not plats:
        return None
    for x in plats:
        if not approve_cli.NAME_RE.match(x):
            raise SetupError(f"APPROVE.PLATFORMS: {x!r} is not a platform name (slack, telegram, teams, ...)")
    ch = _channels(cfg.get("CHANNELS") or {})
    for plat, ids in ch.items():
        if plat not in plats:
            raise SetupError(f"APPROVE.CHANNELS: {plat} is not in APPROVE.PLATFORMS ({', '.join(plats)})")
        for i in ids:
            if not approve_cli.ID_RE.match(i):
                raise SetupError(f"APPROVE.CHANNELS: {i!r} is not a channel ID")
    apr = approve_cli._ids(cfg.get("APPROVERS", "*"))
    if apr != "*":
        for u in apr:
            if not approve_cli.ID_RE.match(u):
                raise SetupError(f"APPROVE.APPROVERS: {u!r} is not a user ID")
    adir = str(cfg.get("APPROVAL_DIR") or appr.get("APPROVAL_DIR") or "~/.hermes/push-approvals")
    log_ = str(cfg.get("LOG") or "~/.rudder/approve.log")
    return {"APPROVAL_DIR": adir, "PLATFORMS": plats, "CHANNELS": ch, "APPROVERS": apr, "LOG": log_}


def write_approve(doc: dict | None) -> str:
    """Write approve.yml (mode 600) after rudder-approve's own checks pass on it; None moves it aside."""
    from rudder_devices import approve_cli
    p = approve_file()
    if doc is None:
        if p.is_file():
            b = _backup(p)
            p.unlink()
            return f"rudder-approve turned off (no platforms); the old file is {b}"
        return "rudder-approve stays off"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = ("# rudder-approve config, written by navigator-setup (APPROVE.*). Your host's own /pushok handler runs:\n"
            "#   rudder-approve ok CODE --user <id> --platform <p> --channel <c>   (docs/APPROVALS.md section 13)\n"
            + yaml.safe_dump({k: v for k, v in doc.items() if not (k == "CHANNELS" and not v)},
                             sort_keys=False, default_flow_style=False))
    tmp = p.with_name(p.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    try:
        approve_cli.load_config(str(tmp))               # the same checks rudder-approve makes at every call
    except approve_cli.ApproveError as e:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"approve.yml: {e}") from None
    _backup(p)
    os.replace(tmp, p)
    return f"rudder-approve config saved: {p} (mode 600)"


# ------------------------------------------------------------------ current values
def current() -> dict:
    """{"nav": {...}, "approval": {...}} as the host has them now (approval: RUDDER's merged view)."""
    from rudder_devices import config as rconfig
    nav = _load(nav_file())
    try:
        rs = rconfig.load()
        appr, val, snow = dict(rs.approval or {}), dict(rs.validation or {}), dict(rs.servicenow or {})
    except Exception:  # noqa: BLE001
        appr, val, snow = {}, {}, {}
    if nav.get("APPROVAL") and not _load(rudder_file()).get("APPROVAL"):
        appr = {**appr, **nav["APPROVAL"]}
    return {"nav": nav, "approval": appr, "models": flat_models(nav.get("MODELS") or {}),
            "approve": current_approve(), "validation": val, "servicenow": snow}


def show() -> str:
    cur = current()
    nav, appr = cur["nav"], cur["approval"]
    pr = nav.get("PROJECT_ROOT") or nav.get("WORKSPACE")
    sk = nav.get("SKILLS_ROOT") or nav.get("EXTRA_SKILLS")
    rows = [f"settings files: {nav_file()}  +  {rudder_file()}",
            f"PROJECT_ROOT            {pr or '(not set)'}" + ("" if nav.get("PROJECT_ROOT") or not pr else "  (old key WORKSPACE)"),
            f"SKILLS_ROOT             {sk or '(none)'}" + ("" if nav.get("SKILLS_ROOT") or not sk else "  (old key EXTRA_SKILLS)")]
    try:
        ch = approval.HermesChannel(appr, str(nav.get("CONSUMER") or "NAVI"))
        rows += [f"APPROVAL.METHOD         {ch.method}",
                 f"APPROVAL.BOT            {ch.profile}",
                 f"APPROVAL.CHANNEL        {ch.chat or '(the bot home channel)'}",
                 f"APPROVAL.APPROVERS      {ch.approvers if ch.approvers == '*' else ', '.join(ch.approvers)}"
                 + ("   (* = anyone in the approval channel)" if ch.approvers == "*" else ""),
                 f"APPROVAL.SELF_APPROVE   {str(ch.self_approve).lower()}"
                 + ("   (an approver turns it off: /selfapprove off in the approval channel)" if ch.self_approve
                    else "   (only the shell turns it back on: set APPROVAL.SELF_APPROVE true)")]
    except approval.ApprovalError as e:
        rows.append(f"APPROVAL                broken: {e}")
    rows += [f"APPROVAL.SHIP_ENGINEERS {', '.join(_list(appr.get('SHIP_ENGINEERS'))) or '(none)'}",
             f"APPROVAL.REPLY_PROFILES {', '.join(_list(appr.get('REPLY_PROFILES'))) or '(none)'}",
             f"APPROVAL.TTL_MIN        {appr.get('TTL_MIN', 30)}",
             f"APPROVAL.MENTION        {appr.get('MENTION') or '(default @<BOT>)'}",
             f"APPROVAL.WEB_BIND       {appr.get('WEB_BIND') or '(no change pages)'}",
             f"APPROVAL.WEB_URL        {appr.get('WEB_URL') or '(<scheme>://<WEB_BIND>)'}",
             f"APPROVAL.WEB_TLS        {_web_tls_row(appr)}",
             f"APPROVAL.PAGES_KEEP_DAYS {retention.keep_days(appr)}   (finished tickets, pages, baseline records)",
             f"APPROVAL.CLEANUP_AT     {appr.get('CLEANUP_AT') or retention.DEFAULT_AT}   (daily clean-up, local time)"]
    ap = cur["approve"]
    if ap:
        apr = ap["APPROVERS"]
        rows += [f"APPROVE.PLATFORMS       {', '.join(ap['PLATFORMS'])}   (rudder-approve: {approve_file()})",
                 f"APPROVE.CHANNELS        {_channels_text(ap['CHANNELS']) or '(any channel)'}",
                 f"APPROVE.APPROVERS       {apr if apr in ('*', ['*']) else ', '.join(map(str, apr))}",
                 f"APPROVE.APPROVAL_DIR    {ap['APPROVAL_DIR'] or '(RUDDER APPROVAL_DIR)'}",
                 f"APPROVE.LOG             {ap['LOG'] or '~/.rudder/approve.log'}"]
    else:
        rows.append("APPROVE.PLATFORMS       (rudder-approve not set up: it refuses every call)")
    m = cur["models"]
    rows += [f"MODELS.HOST             {m['HOST']}   (the MCP host's own model: cloud | local)",
             f"MODELS.LOCAL_URL        {m['LOCAL_URL'] or '(no local model)'}",
             f"MODELS.LOCAL_MODEL      {m['LOCAL_MODEL'] or '-'}",
             f"MODELS.TIMEOUT_SEC      {m['TIMEOUT_SEC']}"]
    try:
        v = rvalidation.settings(type("S", (), {"validation": cur.get("validation") or {}})())
        rows.append(f"VALIDATION.MODE         {v['MODE']}   (none | batfish | netclaw)")
        if v["MODE"] in ("batfish", "netclaw"):
            rows.append(f"VALIDATION.HOST         {v['HOST']}:{v['PORT'] or '(pybatfish default port)'}")
        if v["MODE"] != "none":
            rows += [f"VALIDATION.ON_UNREACHABLE {v['ON_UNREACHABLE']}",
                     f"VALIDATION.POST_PUSH    {str(v['POST_PUSH']).lower()}   (check again after the commit; a fail rolls back)"]
    except rvalidation.ValidationError as e:
        rows.append(f"VALIDATION              broken: {e}")
    from rudder_devices import servicenow as rsn
    try:
        sn = rsn.settings(type("S", (), {"servicenow": cur.get("servicenow") or {}})())
        rows.append(f"SERVICENOW.MODE         {sn['MODE']}   (off | link: read the ticket, write results back | "
                    f"gate: also needs the change request in Implement inside its window)")
        if sn["MODE"] != "off":
            rows.append(f"SERVICENOW.URL          {sn['URL']}   (credential {sn['CREDENTIAL']})")
    except rsn.ServiceNowError as e:
        rows.append(f"SERVICENOW              broken: {e}")
    return "\n".join(rows)


def _web_tls_row(appr: dict) -> str:
    from rudder_devices import web as rweb
    mode, sch = rweb.tls_mode(appr), rweb.scheme(appr)
    now = {"https": "HTTPS (local CA)", "http": "plain HTTP", None: "pages OFF: no certificate"}[sch]
    return f"{mode}   (now: {now})"


# ------------------------------------------------------------------ writing
def _backup(p: Path) -> Path | None:
    if not p.is_file():
        return None
    b = p.with_name(f"{p.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(p, b)
    return b


def _settings_log(line: str) -> None:
    nav = _load(nav_file())
    d = _p(nav.get("STATE_PATH") or "~/.navigator")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(d / "settings.log", "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{getpass.getuser()}\t{line}\n")


def _yaml_value(v) -> str:
    return yaml.safe_dump(v, default_flow_style=True, width=1000).strip().removesuffix("...").strip()


def write_nav(key: str, value) -> None:
    """Set one top-level key in navigator.yml, keeping its comments; the old key's line is commented out."""
    p = nav_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text().splitlines() if p.is_file() else []
    _backup(p)
    new = f"{key}: {_yaml_value(value)}"
    done, out = False, []
    for ln in lines:
        k = re.match(r"^([A-Z_]+):", ln)
        if k and k.group(1) == key:
            if not done:
                out.append(new)
                done = True
            continue
        if k and k.group(1) == LEGACY.get(key):
            out.append(f"# {ln}    # replaced by {key} (navigator-setup)")
            continue
        out.append(ln)
    if not done:
        out.insert(0, new)
    if value in (None, "", []):
        out = [ln for ln in out if ln != new]
    p.write_text("\n".join(out) + "\n")


def _comment_nav_approval() -> None:
    """navigator.yml's own APPROVAL block would shadow the host one: comment it out (backed up)."""
    p = nav_file()
    if not p.is_file() or not _load(p).get("APPROVAL"):
        return
    _backup(p)
    out, inside = [], False
    for ln in p.read_text().splitlines():
        if re.match(r"^APPROVAL:", ln):
            inside = True
            out.append(f"# {ln}    # moved to {rudder_file()} (navigator-setup)")
            continue
        if inside and (ln.startswith((" ", "\t")) or not ln.strip()):
            out.append(f"# {ln}" if ln.strip() else ln)
            continue
        inside = False
        out.append(ln)
    p.write_text("\n".join(out) + "\n")


def write_approval(cfg: dict) -> None:
    p = rudder_file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _load(p)
    _backup(p)
    data["APPROVAL"] = cfg
    head = ("# Host-specific RUDDER / NAVIGATOR settings, merged over rudder.yml (sections key by key).\n"
            "# Written by navigator-setup; change with: navigator-setup set APPROVAL.<KEY> <value>\n")
    p.write_text(head + yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    os.chmod(p, 0o600)
    _comment_nav_approval()


# ------------------------------------------------------------------ the approval test
def test_approval(cfg: dict, consumer: str = "NAVI", timeout: int = 300, out=print, runner=None,
                  sleep=time.sleep) -> bool:
    """Send a test code THE NEW WAY and wait until it is approved there. Nothing else changes."""
    ch = approval.HermesChannel(cfg, consumer, **({"runner": runner} if runner else {}))

    def message(code: str) -> str:
        return (f"🧪 NAVIGATOR setup: approval test for this channel ({ch.where}).\n"
                f"If you are setting up approvals, approve it: /pushok {code}\n"
                f"Nothing changes on any device. Expires in {timeout // 60} min.")

    hashes = ch.request("NAVIGATOR setup test", message, requester=None)
    out(f"A test code was sent to {ch.where}. Approve it there with {ch.how()} "
        f"(waiting up to {timeout // 60} min)...")
    try:
        end = time.time() + timeout
        while time.time() < end:
            st = ch.state(hashes)
            if st == "approved":
                who = ch.who(hashes)
                out("Approved" + (f" by {who}" if who else "") + ": the new approval settings work.")
                return True
            if st in ("rejected", "gone"):
                out(f"Test {st}: the settings were NOT saved.")
                return False
            sleep(2)
        out("No approval in time: the settings were NOT saved.")
        return False
    finally:
        ch.close(hashes)


# ------------------------------------------------------------------ set / add / remove
def _split(key: str) -> tuple[str, str]:
    k = key.strip().upper()
    if k in NAV_KEYS:
        return "nav", k
    if k.startswith("APPROVAL.") and k[9:] in APPROVAL_KEYS:
        return "approval", k[9:]
    if k.startswith("MODELS.") and k[7:] in MODEL_KEYS:
        return "models", k[7:]
    if k.startswith("APPROVE.") and k[8:] in APPROVE_KEYS:
        return "approve", k[8:]
    if k.startswith("VALIDATION.") and k[11:] in VALIDATION_KEYS:
        return "validation", k[11:]
    if k.startswith("SERVICENOW.") and k[11:] in SERVICENOW_KEYS:
        return "servicenow", k[11:]
    raise SetupError(f"unknown key {key}: one of {', '.join(NAV_KEYS)}, "
                     + ", ".join(f"APPROVAL.{k}" for k in APPROVAL_KEYS) + ", "
                     + ", ".join(f"MODELS.{k}" for k in MODEL_KEYS) + ", "
                     + ", ".join(f"APPROVE.{k}" for k in APPROVE_KEYS) + ", "
                     + ", ".join(f"VALIDATION.{k}" for k in VALIDATION_KEYS) + ", "
                     + ", ".join(f"SERVICENOW.{k}" for k in SERVICENOW_KEYS))


def normalize_validation(raw: dict) -> dict:
    """The VALIDATION block as stored: checked, defaults left out, empty values dropped."""
    raw = {k: v for k, v in raw.items() if v not in ("", None, "-")}
    try:
        cfg = rvalidation.settings(type("S", (), {"validation": raw})())
    except (rvalidation.ValidationError, ValueError) as e:
        raise SetupError(str(e)) from None
    if cfg["MODE"] in ("batfish", "netclaw"):
        try:
            rvalidation.check_private(cfg["HOST"])
        except rvalidation.ValidationError as e:
            raise SetupError(str(e)) from None
    out = {"MODE": cfg["MODE"]}
    if cfg["MODE"] != "none":
        out["ON_UNREACHABLE"] = cfg["ON_UNREACHABLE"]
        out["POST_PUSH"] = cfg["POST_PUSH"]
        if cfg["TIMEOUT"] != 300:
            out["TIMEOUT"] = cfg["TIMEOUT"]
    if cfg["MODE"] in ("batfish", "netclaw") and raw.get("HOST"):
        out["HOST"] = cfg["HOST"]
    if cfg["MODE"] in ("batfish", "netclaw") and cfg["PORT"]:
        out["PORT"] = cfg["PORT"]
    return out


def check_validation(block: dict) -> str:
    try:
        return rvalidation.test(rvalidation.settings(type("S", (), {"validation": block})()))
    except rvalidation.ValidationError as e:
        raise SetupError(f"validation test failed, nothing saved: {e}") from None


def write_validation(block: dict) -> None:
    p = rudder_file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _load(p)
    _backup(p)
    data["VALIDATION"] = block
    head = ("# Host-specific RUDDER / NAVIGATOR settings, merged over rudder.yml (sections key by key).\n"
            "# Written by navigator-setup; change with: navigator-setup set APPROVAL.<KEY> <value>\n")
    p.write_text(head + yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    os.chmod(p, 0o600)


def normalize_servicenow(raw: dict) -> dict:
    """The SERVICENOW block as stored: checked; off keeps only MODE (and the other keys for later)."""
    from rudder_devices import servicenow as rsn
    raw = {k: v for k, v in raw.items() if v not in ("", None, "-")}
    try:
        cfg = rsn.settings(type("S", (), {"servicenow": raw})())
    except rsn.ServiceNowError as e:
        raise SetupError(str(e)) from None
    out = {"MODE": cfg["MODE"]}
    for k in ("URL", "CREDENTIAL"):
        if cfg[k]:
            out[k] = cfg[k]
    if not cfg["VERIFY_TLS"]:
        out["VERIFY_TLS"] = False
    if cfg["CLOSE_STATE"]:
        out["CLOSE_STATE"] = cfg["CLOSE_STATE"]
    return out


def check_servicenow(block: dict) -> str:
    from rudder_devices import config as rconfig
    from rudder_devices import servicenow as rsn
    try:
        s = rconfig.load()
        s = type("S", (), {**{k: getattr(s, k) for k in s.__dataclass_fields__}, "servicenow": block})()
        return rsn.test(s)
    except rsn.ServiceNowError as e:
        raise SetupError(f"ServiceNow test failed, nothing saved: {e}") from None


def write_servicenow(block: dict) -> None:
    p = rudder_file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _load(p)
    _backup(p)
    data["SERVICENOW"] = block
    head = ("# Host-specific RUDDER / NAVIGATOR settings, merged over rudder.yml (sections key by key).\n"
            "# Written by navigator-setup; change with: navigator-setup set APPROVAL.<KEY> <value>\n")
    p.write_text(head + yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    os.chmod(p, 0o600)


def apply_changes(pairs: list[tuple[str, object]], test=test_approval, out=print, timeout: int = 300,
                  check_model=check_local_model, check_val=None, check_sn=None) -> list[str]:
    """Check and save a set of changes. Approval changes that alter how approving works are saved only after
    the test passes with the new settings."""
    cur = current()
    nav_new, appr_new, models_new = {}, dict(cur["approval"]), dict(cur["models"])
    approve_new, approve_touched = dict(cur["approve"]), False
    val_new = dict(cur.get("validation") or {})
    sn_new = dict(cur.get("servicenow") or {})
    for key, val in pairs:
        where, k = _split(key)
        if where == "validation":
            val_new[k] = val
            continue
        if where == "servicenow":
            sn_new[k] = val
            continue
        if where == "nav":
            nav_new[k] = val
        elif where == "models":
            models_new[k] = val
        elif where == "approve":
            approve_new[k] = val
            approve_touched = True
        else:
            appr_new[k] = val
    msgs = []
    if "PROJECT_ROOT" in nav_new:
        nav_new["PROJECT_ROOT"] = str(check_project_root(nav_new["PROJECT_ROOT"]))
        msgs.append(f"PROJECT_ROOT {nav_new['PROJECT_ROOT']}: exists, readable and writable")
    if "SKILLS_ROOT" in nav_new:
        sp, n = check_skills_root(nav_new["SKILLS_ROOT"])
        nav_new["SKILLS_ROOT"] = str(sp) if sp else ""
        msgs.append(f"SKILLS_ROOT {sp}: readable, {n} skill(s)" if sp else "SKILLS_ROOT removed")
    appr_changed = {k for k in APPROVAL_KEYS if _norm(appr_new.get(k)) != _norm(cur["approval"].get(k))}
    if appr_changed:
        appr_new = normalize_approval(appr_new)
        if appr_changed & set(TESTED):
            consumer = str(cur["nav"].get("CONSUMER") or "NAVI")
            if not test(appr_new, consumer, timeout=timeout, out=out):
                raise SetupError("the approval test did not pass; nothing was saved")
    val_block = None
    if val_new != dict(cur.get("validation") or {}):
        val_block = normalize_validation(val_new)
        if val_block == normalize_validation(dict(cur.get("validation") or {"MODE": "none"})):
            val_block = None
        elif val_block["MODE"] != "none":
            msgs.append((check_val or check_validation)(val_block))
    sn_block = None
    if sn_new != dict(cur.get("servicenow") or {}):
        sn_block = normalize_servicenow(sn_new)
        if sn_block == normalize_servicenow(dict(cur.get("servicenow") or {"MODE": "off"})):
            sn_block = None
        elif sn_block["MODE"] != "off":
            msgs.append((check_sn or check_servicenow)(sn_block))
    models_changed = {k for k in MODEL_KEYS if _norm(str(models_new.get(k) or "") or None)
                      != _norm(str(cur["models"].get(k) or "") or None)}
    models_block = None
    if models_changed:
        models_block = normalize_models(models_new)
        if models_block["LOCAL"]["URL"]:
            msgs.append(check_model(models_block))
    for k, v in nav_new.items():
        old = cur["nav"].get(k) or cur["nav"].get(LEGACY[k])
        write_nav(k, v)
        _settings_log(f"{k}: {old!r} -> {v!r}")
    if appr_changed:
        write_approval(appr_new)
        for k in sorted(appr_changed):
            _settings_log(f"APPROVAL.{k}: {cur['approval'].get(k)!r} -> {appr_new.get(k)!r}")
        msgs.append("APPROVAL saved: " + ", ".join(f"{k}={_yaml_value(appr_new.get(k))}" for k in sorted(appr_changed)))
        if "SELF_APPROVE" in appr_changed:
            msgs.append(self_approve_note(appr_new))
    approve_doc, approve_changed = None, False
    if approve_touched:
        approve_doc = normalize_approve(approve_new, appr_new)
        old = normalize_approve(cur["approve"], cur["approval"]) if cur["approve"] else None
        approve_changed = approve_doc != old
    if approve_changed:
        msgs.append(write_approve(approve_doc))
        _settings_log(f"APPROVE: {old!r} -> {approve_doc!r}")
        meth = str(appr_new.get("METHOD") or "telegram").lower()
        if approve_doc and meth not in PLUGIN_METHODS and meth not in approve_doc["PLATFORMS"]:
            msgs.append(f"WARNING: APPROVAL.METHOD is {meth} but APPROVE.PLATFORMS does not list it: "
                        "rudder-approve would refuse every approval for it")
        msgs.append("Check it end to end: navigator-setup test-approval (send !pushok from your host).")
    if models_block is not None:
        write_models(models_block)
        for k in sorted(models_changed):
            _settings_log(f"MODELS.{k}: {cur['models'].get(k)!r} -> {models_new.get(k)!r}")
        msgs.append(f"MODELS saved: HOST={models_block['HOST']}, LOCAL_URL={models_block['LOCAL']['URL'] or '-'}, "
                    f"LOCAL_MODEL={models_block['LOCAL']['MODEL'] or '-'}")
    if val_block is not None:
        write_validation(val_block)
        _settings_log(f"VALIDATION: {cur.get('validation')!r} -> {val_block!r}")
        msgs.append("VALIDATION saved: " + ", ".join(f"{k}={_yaml_value(v)}" for k, v in val_block.items()))
    if sn_block is not None:
        write_servicenow(sn_block)
        _settings_log(f"SERVICENOW: {cur.get('servicenow')!r} -> {sn_block!r}")
        msgs.append("SERVICENOW saved: " + ", ".join(f"{k}={_yaml_value(v)}" for k, v in sn_block.items()))
    if nav_new or appr_changed or models_block is not None or val_block is not None or sn_block is not None:
        msgs.append("Now: systemctl --user restart rudder-scheduler, and /reload-mcp in chat "
                    "(or restart the MCP host) so NAVIGATOR reads the new settings.")
    return msgs


def _norm(v):
    if isinstance(v, list):
        return sorted(str(x) for x in v)
    return v if v not in ("", None) else None


def edit_list(key: str, value: str, add: bool, **kw) -> list[str]:
    where, k = _split(key)
    if where == "approve" and k in APPROVE_LISTS:
        cur_ap = current()["approve"]
        if k == "CHANNELS":
            items = _channels_text(cur_ap.get("CHANNELS") or {})
            items = [x for x in _list(items)]
        elif k == "APPROVERS":
            a = cur_ap.get("APPROVERS", "*")
            items = [] if a in ("*", ["*"], None) else [str(x) for x in (a if isinstance(a, list) else _list(a))]
        else:
            items = list(cur_ap.get("PLATFORMS") or [])
        if add:
            items = ["*"] if (k == "APPROVERS" and value == "*") else items + ([value] if value not in items else [])
        else:
            items = [x for x in items if x != value]
        if k == "APPROVERS" and not items:
            items = "*"
        return apply_changes([(f"APPROVE.{k}", items if not isinstance(items, list) or k != "CHANNELS"
                               else ",".join(items))], **kw)
    if where != "approval" or k not in LIST_KEYS:
        raise SetupError(f"add/remove works on {', '.join('APPROVAL.' + x for x in LIST_KEYS)}, "
                         + ", ".join("APPROVE." + x for x in APPROVE_LISTS))
    cur = current()["approval"]
    items = approval.approvers(cur) if k == "APPROVERS" else _list(cur.get(k))
    items = [] if items == "*" else list(items)
    if add:
        if value == "*":
            items = ["*"]
        elif value not in items:
            items.append(value)
    else:
        items = [x for x in items if x != value]
    if k == "APPROVERS" and not items:
        items = "*"
    return apply_changes([(f"APPROVAL.{k}", items)], **kw)


# ------------------------------------------------------------------ the interview
def self_approve_note(appr: dict) -> str:
    """Printed by the interview and by `set APPROVAL.SELF_APPROVE` (decision #161: no question any more)."""
    from rudder_devices import selfapprove
    on = str(appr.get("SELF_APPROVE", True)).strip().lower() not in ("0", "false", "no", "off")
    if on:
        return selfapprove.NOTE
    return ("Self-approval is off: another approver must approve each change. Only the host's shell turns it "
            f"back on: {selfapprove.ON_HINT}")


def validation_questions(q, cur, out=print) -> list[tuple[str, object]]:
    """Interview: validate every change before the approval (and again after the commit)?"""
    v = dict(cur.get("validation") or {})
    mode = q("VALIDATION.MODE: check each change before it is approved? none, batfish (a Batfish service; we can "
             "start one: docker run -d -p 127.0.0.1:9996:9996 batfish/allinone), or netclaw (NetClaw's Batfish)", str(v.get("MODE") or "none")).strip().lower()
    out_pairs = [("VALIDATION.MODE", mode)]
    if mode == "netclaw":
        found = rvalidation.netclaw_service()
        out(f"NetClaw's Batfish: {found[0]}:{found[1] or 'default port'}" if found else
            "NetClaw's Batfish settings (BATFISH_HOST) were not found; give the host.")
        out_pairs.append(("VALIDATION.HOST", q("VALIDATION.HOST: the Batfish host",
                                               v.get("HOST") or (found[0] if found else "127.0.0.1"))))
    elif mode == "batfish":
        out_pairs.append(("VALIDATION.HOST", q("VALIDATION.HOST: the Batfish host (local or private address)",
                                               v.get("HOST") or "127.0.0.1")))
        out_pairs.append(("VALIDATION.PORT", q("VALIDATION.PORT: its port ('-' = pybatfish's default, 9996)",
                                               str(v.get("PORT") or "-"))))
    if mode not in ("none", ""):
        out_pairs.append(("VALIDATION.ON_UNREACHABLE", q("VALIDATION.ON_UNREACHABLE: if the validator is down, "
                                                         "block the change or warn and go on? block/warn",
                                                         str(v.get("ON_UNREACHABLE") or "block"))))
        out("A failed check before the push blocks the change; after the commit it is checked again and a "
            "fail rolls the device back (VALIDATION.POST_PUSH false turns that second check off).")
    return out_pairs


def servicenow_questions(q, cur) -> list[tuple[str, object]]:
    """Interview: "Use ServiceNow?" no / link / gate (decision #159). The credential is an ATLAS tag."""
    sn = dict(cur.get("servicenow") or {})
    mode = q("SERVICENOW.MODE: use ServiceNow? off, link (read the ticket by ticket_id and write the diff, "
             "validation, result and close code back) or gate (the push also needs the change request in "
             "Implement inside its window, plus /pushok)", str(sn.get("MODE") or "off")).strip().lower()
    out = [("SERVICENOW.MODE", mode)]
    if mode in ("link", "gate"):
        out.append(("SERVICENOW.URL", q("SERVICENOW.URL: https://<instance>.service-now.com", sn.get("URL") or "")))
        out.append(("SERVICENOW.CREDENTIAL", q("SERVICENOW.CREDENTIAL: the ATLAS credential tag of the ServiceNow "
                                               "account (make it first: atlas-interview run credential_new)",
                                               sn.get("CREDENTIAL") or "")))
    return out


def interview(ask=input, out=print, test=test_approval, check_model=check_local_model,
              secret_ask=None, check_val=None) -> list[str]:
    cur = current()
    nav, appr = cur["nav"], cur["approval"]

    def q(text, default=""):
        a = ask(f"{text}" + (f" [{default}]" if default not in ("", None) else "") + ": ").strip()
        return a or (default if default is not None else "")

    out("NAVIGATOR setup. Enter keeps the value in [brackets].")
    pairs = [("PROJECT_ROOT", q("PROJECT_ROOT: the folder that holds project folders (required)",
                                nav.get("PROJECT_ROOT") or nav.get("WORKSPACE") or ""))]
    sk = nav.get("SKILLS_ROOT") or nav.get("EXTRA_SKILLS") or ""
    sk = sk[0] if isinstance(sk, list) and sk else sk
    pairs.append(("SKILLS_ROOT", q("SKILLS_ROOT: a folder of your own skills, one folder + SKILL.md each "
                                   "(optional, '-' for none)", sk or "-")))
    ch_default = appr.get("METHOD") or "telegram"
    m = q("APPROVAL.METHOD: telegram or slack (Hermes' push-approve plugin), or another platform your host "
          "sends to (teams, discord...; its /pushok handler calls rudder-approve). One method at a time",
          ch_default).lower()
    pairs.append(("APPROVAL.METHOD", m))
    prof = {str(k).upper(): v for k, v in (appr.get("PROFILES") or {}).items()}
    pairs.append(("APPROVAL.BOT", q("APPROVAL.BOT: the approval bot's Hermes profile (not the chat bot)",
                                    appr.get("BOT") or next(iter(prof.values()), "navi"))))
    chan = str(appr.get("CHANNEL") or "")
    chan = "" if chan.lower() == "hermes" else chan
    if m == "slack":
        pairs.append(("APPROVAL.CHANNEL", q("APPROVAL.CHANNEL: the private approval channel's ID (C...)", chan)))
    elif m in approval.CHANNEL_REQUIRED:
        pairs.append(("APPROVAL.CHANNEL", q(f"APPROVAL.CHANNEL: the {m} approval channel's ID", chan)))
    else:
        pairs.append(("APPROVAL.CHANNEL", q("APPROVAL.CHANNEL: Telegram chat ID ('-' = the bot's home channel)",
                                            chan or "-")))
    apr = approval.approvers(appr)
    pairs.append(("APPROVAL.APPROVERS", q("APPROVAL.APPROVERS: '*' = anyone in the channel (the organization "
                                          "curates it), or user IDs separated by commas",
                                          "*" if apr == "*" else ",".join(apr))))
    out(self_approve_note(appr))        # decision #161: no question; the default stays true
    pairs.append(("APPROVAL.SHIP_ENGINEERS", q("APPROVAL.SHIP_ENGINEERS: user IDs whose lab changes need no "
                                               "approval (optional, comma separated, '-' for none)",
                                               ",".join(_list(appr.get("SHIP_ENGINEERS"))) or "-")))
    pairs.append(("APPROVAL.PAGES_KEEP_DAYS", q("APPROVAL.PAGES_KEEP_DAYS: days to keep finished change tickets, "
                                                "their pages and baseline records (the audit log is kept)",
                                                str(retention.keep_days(appr)))))
    pairs.append(("APPROVAL.CLEANUP_AT", q("APPROVAL.CLEANUP_AT: daily clean-up time, local HH:MM ('off' = never)",
                                           str(appr.get("CLEANUP_AT") or retention.DEFAULT_AT))))
    pairs += _approve_questions(q, cur, m, chan, apr)
    mods = cur["models"]
    pairs.append(("MODELS.HOST", q("MODELS.HOST: the model your MCP host itself runs: cloud (Claude, GPT, Gemini...) "
                                   "or local (Ollama...). A cloud host never reads LOCAL project files",
                                   mods["HOST"]).lower()))
    lurl = q("MODELS.LOCAL_URL: a local OpenAI-compatible endpoint for LOCAL work (Ollama: "
             "http://127.0.0.1:11434/v1; '-' for none)", mods["LOCAL_URL"] or "-")
    pairs.append(("MODELS.LOCAL_URL", lurl))
    if lurl not in ("", "-"):
        pairs.append(("MODELS.LOCAL_MODEL", q("MODELS.LOCAL_MODEL: the model's name there (e.g. qwen3.5:9b)",
                                              mods["LOCAL_MODEL"])))
    pairs += validation_questions(q, cur, out)
    pairs += servicenow_questions(q, cur)
    pairs = [(k, ("" if v == "-" else v)) for k, v in pairs]
    pairs = [(k, _list(v) if k.split(".")[-1] in LIST_KEYS and v != "*" else v) for k, v in pairs]
    msgs = apply_changes(pairs, test=test, out=out, check_model=check_model, check_val=check_val)
    msgs += https_step(q, out)
    msgs += recovery_step(q, out, secret_ask)
    return msgs


def recovery_step(q, out=print, secret_ask=None) -> list[str]:
    """Interview: the CREDENTIALS key's recovery key (made here if there is none) and a first full backup."""
    from atlas_inventory import config as aconfig
    from atlas_inventory import recovery
    from . import backup
    secret_ask = secret_ask or getpass.getpass
    try:
        cfg = aconfig.load()
    except Exception as e:  # noqa: BLE001
        return [f"recovery key: skipped (ATLAS settings not readable: {type(e).__name__})"]
    msgs = []
    if not recovery.load_doc(cfg):
        a = q("RECOVERY: make the recovery key of the CREDENTIALS key now (48 digits, shown once; "
              "needed for backups and restores)? yes/no", "yes").strip().lower()
        if a in ("y", "yes"):
            backup.main(["recovery", "init"], out=out, ask=secret_ask)
            msgs.append("recovery key made")
    if recovery.load_doc(cfg):
        a = q("BACKUP: make a full encrypted backup now (asks for the recovery key)? yes/no", "no").strip().lower()
        if a in ("y", "yes"):
            folder = q("BACKUP: folder for the backup file", "~/navigator-backups")
            if backup.main(["backup", "--out", folder], out=out, ask=secret_ask) == 0:
                msgs.append(f"backup made in {folder}")
    return msgs


def https_step(q, out=print) -> list[str]:
    """Interview: HTTPS access for remote MCP clients (ports 8791-8793): the local CA and a first admin user."""
    from navigator_gate import tls, users
    from navigator_gate.cli import _show_password
    ready = tls.info().get("ready")
    a = q("HTTPS: let MCP clients on other machines connect over HTTPS with a login (ports 8791-8793)? yes/no",
          "yes" if ready else "no").strip().lower()
    if a not in ("y", "yes"):
        return []
    msgs = []
    r = tls.init()
    msgs.append("TLS: made " + ", ".join(r["made"]) if r["made"] else "TLS: already set up")
    if not users.listing():
        name = q("HTTPS: the first admin user's name", "admin").strip().lower()
        _show_password(name, users.add(name, "admin"), out)
        msgs.append(f"HTTP user {name} (admin) made")
    msgs.append("Start: systemctl --user enable --now navigator-mcp-https (and atlas-mcp-https, rudder-mcp-https "
                "if you want them on their own); details: navigator-setup http show")
    return msgs


def _approve_questions(q, cur, method: str, chan: str, apr) -> list[tuple[str, object]]:
    """Interview: rudder-approve, for hosts whose own /pushok handler calls it (not the Hermes plugin)."""
    ap = cur["approve"]
    need = method not in PLUGIN_METHODS
    a = q("RUDDER-APPROVE: a host other than Hermes' push-approve plugin (OpenClaw, a Teams or Discord bot...) "
          "records !pushok with rudder-approve. Set it up? yes/no",
          "yes" if (ap or need) else "no").strip().lower()
    if a not in ("y", "yes"):
        return []
    plats = ",".join(ap.get("PLATFORMS") or []) or method
    chans = _channels_text(ap.get("CHANNELS") or {}) or (f"{method}={chan}" if chan and chan != "-" else "")
    a_apr = ap.get("APPROVERS")
    a_apr = "*" if a_apr in (None, "*", ["*"]) and apr == "*" else ",".join(map(str, a_apr if isinstance(a_apr, list)
                                                                                   else (apr if apr != "*" else [])))
    out = [("APPROVE.PLATFORMS", q("APPROVE.PLATFORMS: the platforms approvals may come from, comma separated "
                                    "(slack, telegram, teams, discord...)", plats))]
    out.append(("APPROVE.CHANNELS", q("APPROVE.CHANNELS: only these channels, platform=ID pairs "
                                      "(e.g. teams=19:abc@thread.tacv2; '-' = any channel of those platforms)",
                                      chans or "-")))
    out.append(("APPROVE.APPROVERS", q("APPROVE.APPROVERS: '*' = anyone allowed by each request's own rules, or "
                                       "user IDs separated by commas", a_apr or "*")))
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("tls", "user", "http"):
        from navigator_gate import cli as gate_cli
        return gate_cli.main(argv)
    if argv and argv[0] == "connect":
        from . import connect
        return connect.main(argv[1:])
    if argv and argv[0] in ("recovery", "backup", "restore"):
        from . import backup
        return backup.main(argv)
    ap = argparse.ArgumentParser(prog="navigator-setup", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("items", nargs="+", help="KEY VALUE [KEY VALUE ...]")
    for name in ("add", "remove"):
        x = sub.add_parser(name)
        x.add_argument("key")
        x.add_argument("value")
    t = sub.add_parser("test-approval")
    for x in (s, t):
        x.add_argument("--timeout", type=int, default=300, help="seconds to wait for the test /pushok")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "show":
            print(show())
            return 0
        if a.cmd == "set":
            if len(a.items) % 2:
                raise SetupError("set takes KEY VALUE pairs")
            pairs = [(a.items[i], a.items[i + 1]) for i in range(0, len(a.items), 2)]
            pairs = [(k, _list(v) if k.upper().split(".")[-1] in LIST_KEYS and v != "*" else v) for k, v in pairs]
            msgs = apply_changes(pairs, timeout=a.timeout)
        elif a.cmd in ("add", "remove"):
            msgs = edit_list(a.key, a.value, a.cmd == "add")
        elif a.cmd == "test-approval":
            cur = current()
            ok = test_approval(normalize_approval(cur["approval"]), str(cur["nav"].get("CONSUMER") or "NAVI"),
                               timeout=a.timeout)
            return 0 if ok else 1
        else:
            msgs = interview()
    except (SetupError, approval.ApprovalError) as e:
        print(f"navigator-setup: {e}", file=sys.stderr)
        return 2
    print("\n".join(msgs) if msgs else "Nothing changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
