"""RUDDER settings, from rudder.yml (paths and switches only, no secrets).

Lookup: $RUDDER_BOOTSTRAP, then rudder.yml at the repo root. Env RUDDER_CONSUMER overrides CONSUMER.
Host-specific values (names of this host's chat profiles, for example) go in a local file merged on
top: $RUDDER_LOCAL, else ~/.config/rudder/rudder.local.yml (only when RUDDER_BOOTSTRAP is not set).
Top-level keys replace; a section (a mapping such as APPROVAL) is merged key by key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _p(v) -> Path:
    return Path(os.path.expanduser(str(v)))


def inv_key(name) -> str:
    """Inventory name as compared everywhere: upper case, without the INVENTORY_ prefix or .yml."""
    n = str(name or "").strip().upper()
    n = n[:-4] if n.endswith(".YML") else n
    return n.removeprefix("INVENTORY_")


@dataclass(frozen=True)
class Settings:
    atlas_socket: Path        # ATLAS link socket (layer 1)
    token_file: Path          # this RUDDER's token for the ATLAS link
    consumer: str             # who this RUDDER instance serves (e.g. NAVI)
    records_path: Path        # where every command output is saved
    eapi_verify_tls: bool     # lab switches use self-signed certificates: default off
    ssh_strict: bool          # refuse unknown SSH host keys
    known_hosts: Path | None
    timeout: int
    max_workers: int
    max_inline_chars: int     # output returned to the model; the full output is always on disk
    state_path: Path = Path("~/.rudder").expanduser()     # tickets, locks, audit log
    approval: dict | None = None    # APPROVAL section (channel, profiles, ttl)
    commit_timer: str = "00:05:00"  # the switch rolls a change back by itself if not confirmed in time
    allow_production: bool = False  # PRODUCTION devices are refused unless this is true
    deny_lines: tuple = ()          # extra config-line patterns to refuse (regex), on top of the built-in ones
    delegated: dict | None = None   # DELEGATED: {HOME, SERVERS: {name: {COMMAND, CWD}}}
    api_verify_tls: bool = False    # vManage and PAN-OS XML API certificates (lab gear is self-signed)
    commit_wait: int = 300          # seconds to wait for a PAN-OS commit job
    schedule: dict | None = None    # SCHEDULE: {TIMEZONE, NOTICE_SEC, LATE_MIN, MAX_DAYS, POLL_SEC}
    lab_inventories: tuple = ()     # LAB_INVENTORIES: inventories that are labs (names without INVENTORY_)
    lab_any_show: bool = False      # LAB_ANY_SHOW: in a lab inventory, any plain "show ..." command may run
    validation: dict | None = None  # VALIDATION: {MODE, HOST, PORT, COMMAND, ON_UNREACHABLE, POST_PUSH, TIMEOUT}
    servicenow: dict | None = None  # SERVICENOW: {MODE off|link|gate, URL, CREDENTIAL, VERIFY_TLS, CLOSE_STATE}


def _local_file() -> Path | None:
    if os.environ.get("RUDDER_LOCAL"):
        return _p(os.environ["RUDDER_LOCAL"])
    if os.environ.get("RUDDER_BOOTSTRAP"):
        return None
    return _p("~/.config/rudder/rudder.local.yml")


def _merge(base: dict, local: dict) -> dict:
    out = dict(base)
    for k, v in local.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> Settings:
    boot = Path(os.environ.get("RUDDER_BOOTSTRAP") or REPO / "rudder.yml")
    raw = (yaml.safe_load(boot.read_text()) or {}) if boot.is_file() else {}
    local = _local_file()
    if local and local.is_file():
        raw = _merge(raw, yaml.safe_load(local.read_text()) or {})
    kh = raw.get("KNOWN_HOSTS")
    timer = str(raw.get("COMMIT_TIMER", "00:05:00"))
    if not __import__("re").match(r"^\d\d:\d\d:\d\d$", timer):
        raise ValueError("COMMIT_TIMER must look like HH:MM:SS")
    return Settings(
        state_path=_p(raw.get("STATE_PATH", "~/.rudder")),
        approval=raw.get("APPROVAL") or {"CHANNEL": "hermes"},
        commit_timer=timer,
        allow_production=bool(raw.get("ALLOW_PRODUCTION", False)),
        deny_lines=tuple(raw.get("DENY_LINES") or ()),
        delegated=raw.get("DELEGATED") or None,
        api_verify_tls=bool(raw.get("API_VERIFY_TLS", raw.get("EAPI_VERIFY_TLS", False))),
        commit_wait=int(raw.get("COMMIT_WAIT", 300)),
        schedule=raw.get("SCHEDULE") or None,
        lab_inventories=tuple(inv_key(x) for x in (raw.get("LAB_INVENTORIES") or [])),
        lab_any_show=bool(raw.get("LAB_ANY_SHOW", False)),
        validation=raw.get("VALIDATION") or None,
        servicenow=raw.get("SERVICENOW") or None,
        atlas_socket=_p(raw.get("ATLAS_SOCKET", "~/.atlas/link.sock")),
        token_file=_p(raw.get("TOKEN_FILE", "~/.config/rudder/atlas-link.token")),
        consumer=str(os.environ.get("RUDDER_CONSUMER") or raw.get("CONSUMER") or "NAVI").upper(),
        records_path=_p(raw.get("RECORDS_PATH", "~/.rudder/records")),
        eapi_verify_tls=bool(raw.get("EAPI_VERIFY_TLS", False)),
        ssh_strict=bool(raw.get("SSH_STRICT", False)),
        known_hosts=_p(kh) if kh else None,
        timeout=int(raw.get("TIMEOUT", 30)),
        max_workers=int(raw.get("MAX_WORKERS", 16)),
        max_inline_chars=int(raw.get("MAX_INLINE_CHARS", 60000)),
    )
