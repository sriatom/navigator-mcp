"""The ATLAS interview: create, update and remove inventories, records, credentials and access.

One engine, two front ends:
  - MCP tools in server.py (an agent asks the questions in chat);
  - the `atlas-interview` command on the host (you, in a shell; secrets with getpass).

Rules
  1. Nothing is written until preview + commit. Commit needs the code that preview printed,
     so what is committed is exactly what was shown.
  2. No secret goes through chat. Secret fields are refused by the MCP tools; they are set with
     `atlas-interview secret <draft> <FIELD>` on the host, and stored in the draft already encrypted.
  3. Drafts live in STATE_PATH/drafts/<id>.yml (mode 600) and expire after 7 days.
  4. Deletes are soft (IS_ENABLED: false). Hard deletes need the out-of-band /pushok approval,
     which arrives with RUDDER's approver (P4); until then they are refused.
  5. consumer_access (who may use what) is host-only: an agent cannot widen its own access.
  6. Every commit makes a new version of each file it touches (store.py) and an audit line.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import getpass
import hashlib
import io
import ipaddress
import re
import secrets as pysecrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from . import catalog as catalog_mod
from . import config, consumers, credentials, inventory, source, store, vault, yamlpatch

DRAFT_TTL = 7 * 24 * 3600
TOPICS = ("inventory_new", "record_add", "record_edit", "record_remove", "bulk_import", "bulk_edit",
          "inventory_restore", "credential_new", "credential_update", "credential_remove",
          "consumer_access", "source_import", "inventory_sync")
SOURCE_TOPICS = ("source_import", "inventory_sync")          # NetBox / Nautobot (source.py, decision #160)
SOURCE_ANSWERS = ("hand", "netbox", "nautobot", "file")
FILTER_KEYS = ("site", "location", "tag", "role", "status", "platform", "tenant", "q")
HOST_ONLY_TOPICS = {"consumer_access"}
LATER_TOPICS = {"setup": "the setup interview belongs to NAVIGATOR (phase P7)"}
NOT_SECRET = {"USERNAME", "AUTH_USER"}       # typed in chat is fine; still stored encrypted
DEFAULT_PORTS = {"SSH": 22, "TELNET": 23, "EAPI": 443, "HTTPS": 443, "API": 443, "XML_API": 443,
                 "NXAPI": 443, "RESTCONF": 443, "VMANAGE_API": 443, "REDFISH": 443,
                 "NETCONF": 830, "GNMI": 6030}
_INV_NAME = re.compile(r"^[A-Z0-9][A-Z0-9_]{0,60}$")
_TAG = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,60}$")


class InterviewError(Exception):
    pass


# --------------------------------------------------------------------------- value helpers
def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("y", "yes", "true", "1", "on"):
        return True
    if s in ("n", "no", "false", "0", "off"):
        return False
    raise InterviewError(f"answer yes or no, not {v!r}")


def _list(v) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _name(v) -> str:
    s = str(v).strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", s):
        raise InterviewError("use only letters, digits, . _ - (start with a letter or digit)")
    return s


def _addr(v) -> str:
    s = str(v).strip()
    try:
        ipaddress.ip_address(s)
    except ValueError:
        if not inventory._HOST.match(s):
            raise InterviewError(f"{s!r} is not an IP address or host name") from None
    return s


def _port(v) -> int:
    try:
        p = int(v)
    except (TypeError, ValueError):
        raise InterviewError("a port is a number 1-65535") from None
    if not 1 <= p <= 65535:
        raise InterviewError("a port is a number 1-65535")
    return p


def _url(v) -> str:
    s = str(v).strip()
    u = urlparse(s)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise InterviewError("give a URL like https://host[:port][/path]")
    if u.username or u.password:
        raise InterviewError("the URL must not contain a user name or password")
    return s


# --------------------------------------------------------------------------- context
@dataclass
class Ctx:
    cfg: config.Config
    cat: catalog_mod.Catalog | None
    draft: dict
    host: bool                       # True = atlas-interview on the host, False = MCP (agent)

    @property
    def target(self) -> dict:
        return self.draft["target"]

    def inv_path(self, name: str | None = None) -> Path:
        n = name or self.target.get("inventory")
        if not n:
            raise InterviewError("this topic needs an inventory name")
        return self.cfg.data_path / f"INVENTORY_{re.sub(r'^INVENTORY_', '', n)}.yml"

    def inv_doc(self, name: str | None = None) -> dict:
        p = self.inv_path(name)
        if not p.is_file():
            raise InterviewError(f"inventory {p.stem} not found")
        return yaml.safe_load(p.read_text()) or {}

    def defaults(self) -> dict:
        try:
            return ((self.inv_doc().get("META") or {}).get("DEFAULTS")) or {}
        except InterviewError:
            return {}

    def cred_tags(self) -> set[str]:
        return credentials.tags(self.cfg.credential_file)


@dataclass
class Q:
    """One question."""
    name: str
    text: str
    required: Any = False            # bool, or a function of the context
    check: Callable[[Any, Ctx], Any] | None = None
    default: Callable[[Ctx], Any] | None = None
    choices: Callable[[Ctx], list[str]] | None = None
    secret: bool = False
    when: Callable[[Ctx], bool] = field(default=lambda c: True)


def _req(q: Q, c: Ctx) -> bool:
    return bool(q.required(c)) if callable(q.required) else bool(q.required)


def _active(c: Ctx) -> dict[str, Q]:
    """Questions that apply now (by name)."""
    return {q.name: q for q in questions(c) if q.when(c)}


def _answer(c: Ctx, k, fallback=None):
    a = c.draft["answers"]
    return a[k] if k in a else fallback


def _current(c: Ctx) -> dict:
    """The record being edited (record_edit / record_remove), or {}."""
    return c.draft.get("current") or {}


def _cur_or(c: Ctx, k, fallback=None):
    cur = _current(c)
    if k in cur:
        return cur[k]
    if k in inventory.TAGS_FIELDS:
        return (cur.get("TAGS") or {}).get(k, fallback)
    return fallback


# --------------------------------------------------------------------------- field checks
def _check_new_record_name(v, c: Ctx) -> str:
    n = _name(v)
    if c.draft["topic"] == "record_edit":
        return n
    doc = c.inv_doc()
    taken = {inventory.record_name({**r, "RECORD_TYPE": rt}).upper()
             for rt, key in inventory.RECORD_LISTS.items() for r in doc.get(key) or [] if isinstance(r, dict)}
    taken |= {str(r.get("DEVICE_ID", "")).upper() for r in doc.get("INVENTORY") or [] if isinstance(r, dict)}
    if n.upper() in taken:
        raise InterviewError(f"{n} already exists in {c.inv_path().stem}")
    return n


def _check_addr_unique(v, c: Ctx) -> str:
    a = _addr(v)
    if a == _cur_or(c, "DEVICE_ADDR") or a == _cur_or(c, "ADDR"):
        return a
    doc = c.inv_doc()
    for key, fld in (("INVENTORY", "DEVICE_ADDR"), ("OOB", "ADDR")):
        for r in doc.get(key) or []:
            if isinstance(r, dict) and str(r.get(fld, "")).strip() == a:
                raise InterviewError(f"{a} is already used by {r.get('DEVICE_NAME') or r.get('NAME')}")
    return a


def _check_os(v, c: Ctx) -> str:
    if c.cat is None:
        return str(v).strip().upper()
    pid = c.cat.resolve(v)
    if not pid:
        raise InterviewError(f"unknown platform {v!r}; use platform_list (e.g. EOS, IOSXE, NXOS, PANOS)")
    return pid


def _platform(c: Ctx) -> dict:
    os_v = _answer(c, "OS", _cur_or(c, "OS"))
    if c.cat is None or not os_v:
        return {}
    pid = c.cat.resolve(os_v)
    return c.cat.platforms.get(pid, {}) if pid else {}


def _check_conn(v, c: Ctx) -> str:
    s = str(v).strip().upper()
    conns = _platform(c).get("conn") or []
    if conns and s not in conns and not (s == "HTTPS" and "EAPI" in conns):
        raise InterviewError(f"{s} is not a connection type for this platform; choose {', '.join(conns)}")
    if not conns and s not in inventory.KNOWN_CONN:
        raise InterviewError(f"unknown connection type {s}; choose one of {', '.join(sorted(inventory.KNOWN_CONN))}")
    return s


def _check_cred_ref(v, c: Ctx) -> str:
    t = str(v).strip()
    if t not in c.cred_tags():
        raise InterviewError(f"credential tag {t} does not exist; create it first (topic credential_new)")
    if not c.host and not consumers.allowed_tag(c.cfg, t):
        raise InterviewError(f"consumer {c.cfg.consumer} may not use credential {t}")
    return t


def _check_product(v, c: Ctx) -> str:
    if c.cat is None:
        return str(v).strip().upper()
    p = c.cat.controller(v)
    if not p:
        raise InterviewError(f"unknown controller product; choose {', '.join(c.cat.controllers)}")
    return p["PRODUCT"]


def _check_oob_type(v, c: Ctx) -> str:
    if c.cat is None:
        return str(v).strip().upper()
    p = c.cat.oob_type(v)
    if not p:
        raise InterviewError(f"choose {', '.join(c.cat.oob_types)}")
    return p["TYPE"]


def _check_ports(v, c: Ctx) -> dict:
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items()}
    out = {}
    for part in _list(v):
        if "=" not in part:
            raise InterviewError("give PORTS as port=DEVICE pairs, e.g. 1=R1,2=R2")
        k, x = part.split("=", 1)
        out[k.strip()] = x.strip()
    return out


def _check_inv_new(v, c: Ctx) -> str:
    n = re.sub(r"^INVENTORY_", "", str(v).strip().upper())
    if not _INV_NAME.match(n):
        raise InterviewError("inventory names use A-Z, 0-9 and _ only")
    if c.inv_path(n).is_file():
        raise InterviewError(f"INVENTORY_{n} already exists")
    if not c.host and not consumers.allowed_inventory(c.cfg, f"INVENTORY_{n}"):
        raise InterviewError(f"consumer {c.cfg.consumer} may not use an inventory named INVENTORY_{n}")
    return n


def _check_tag_new(v, c: Ctx) -> str:
    t = str(v).strip().upper()
    if not _TAG.match(t):
        raise InterviewError("credential tags use A-Z, 0-9, _ and - only")
    if t in c.cred_tags():
        raise InterviewError(f"credential {t} already exists; use credential_update")
    if not c.host and not consumers.allowed_tag(c.cfg, t):
        raise InterviewError(f"consumer {c.cfg.consumer} may not use a credential named {t}")
    return t


def _check_ctype(v, c: Ctx) -> str:
    t = str(v).strip().upper()
    if t not in credentials.CREDENTIAL_TYPES:
        raise InterviewError(f"choose {', '.join(credentials.CREDENTIAL_TYPES)}")
    return t


def _check_version(v, c: Ctx) -> str:
    names = [p.stem for p in store.list_versions(c.cfg, c.inv_path())]
    s = str(v).strip().removesuffix(".yml")
    if s not in names:
        raise InterviewError(f"no such version; choose one of {', '.join(names[:10]) or 'none'}")
    return s


def _check_list(v, c: Ctx) -> list[str]:
    return _list(v)


def _pairs(v) -> dict[str, str]:
    if isinstance(v, dict):
        return {str(k).strip().upper(): str(x).strip() for k, x in v.items()}
    out = {}
    for part in _list(v):
        if "=" not in part:
            raise InterviewError(f"{part!r}: use FIELD=value")
        k, x = part.split("=", 1)
        out[k.strip().upper()] = x.strip()
    if not out:
        raise InterviewError("give at least one FIELD=value")
    return out


BULK_LOCKED = {"DEVICE_NAME", "DEVICE_ID", "NAME", "DEVICE_ADDR", "ADDR", "URL"}   # unique per record


def _check_match(v, c: Ctx) -> dict[str, str]:
    m = _pairs(v)
    if "PLATFORM" in m and c.cat is not None:
        bad = [p for p in m["PLATFORM"].split("|") if not c.cat.resolve(p)]
        if bad:
            raise InterviewError(f"unknown platform(s): {', '.join(bad)}")
    return m


def _check_set(v, c: Ctx) -> dict:
    want = _pairs(v)
    qs = {q.name: q for q in RECORD_QUESTIONS[c.target.get("record_type", "DEVICE")]()}
    out = {}
    for k, x in want.items():
        if k in BULK_LOCKED:
            raise InterviewError(f"{k} is unique per record; change it with record_edit")
        if k not in qs:
            raise InterviewError(f"{k} is not a record field ({', '.join(q for q in qs if q not in BULK_LOCKED)})")
        out[k] = qs[k].check(x, c) if qs[k].check else x
    if "OS" in out or "CONN_TYPE" in out:
        raise InterviewError("OS and CONN_TYPE depend on each other per device; change them with record_edit")
    return out


def _matches(c: Ctx, rec: dict, match: dict[str, str]) -> bool:
    for k, want in match.items():
        opts = [w.strip() for w in want.split("|")]
        if k == "PLATFORM":
            have = c.cat.resolve(rec.get("OS")) if c.cat else str(rec.get("OS", "")).upper()
            if have not in {(c.cat.resolve(o) if c.cat else o.upper()) for o in opts}:
                return False
            continue
        vals = inventory._values(rec, k)
        ci = k in inventory.CASE_INSENSITIVE_FIELDS
        if not any((v.upper() if ci else v) in {(o.upper() if ci else o) for o in opts} for v in vals):
            return False
    return True


# --------------------------------------------------------------------------- question sets
def _prod_default(c: Ctx):
    cur = _cur_or(c, "PRODUCTION")
    if cur is not None:
        return cur
    d = c.defaults().get("PRODUCTION")
    return True if d is None else d       # unanswered counts as production (the safe default)


def _tags_q() -> list[Q]:
    return [Q("SITE", "Site?", check=lambda v, c: str(v).strip(),
              default=lambda c: _cur_or(c, "SITE", c.defaults().get("SITE"))),
            Q("FAB", "Fabric / group?", check=lambda v, c: str(v).strip(),
              default=lambda c: _cur_or(c, "FAB", c.defaults().get("FAB"))),
            Q("ROLE", "Role(s), comma separated (e.g. LEAF, SPINE, FIREWALL)?", check=_check_list,
              default=lambda c: _cur_or(c, "ROLE"))]


def _device_q() -> list[Q]:
    return [
        Q("DEVICE_NAME", "Device name?", True, _check_new_record_name, lambda c: _cur_or(c, "DEVICE_NAME")),
        Q("DEVICE_ID", "Device ID (usually the same as the name)?", True, lambda v, c: _name(v),
          lambda c: _cur_or(c, "DEVICE_ID", _answer(c, "DEVICE_NAME"))),
        Q("DEVICE_ADDR", "Management IP address or DNS name?", True, _check_addr_unique,
          lambda c: _cur_or(c, "DEVICE_ADDR")),
        Q("OS", "Platform / OS? (platform_list shows the names)", True, _check_os, lambda c: _cur_or(c, "OS")),
        Q("VENDOR", "Vendor?", True, lambda v, c: str(v).strip().upper(),
          lambda c: _cur_or(c, "VENDOR", _platform(c).get("vendor"))),
        Q("CONN_TYPE", "How do we connect?", True, _check_conn,
          lambda c: _cur_or(c, "CONN_TYPE", (_platform(c).get("conn") or [None])[0]),
          lambda c: list(_platform(c).get("conn") or [])),
        Q("PORT", "Port?", True, lambda v, c: _port(v),
          lambda c: _cur_or(c, "PORT", DEFAULT_PORTS.get(str(_answer(c, "CONN_TYPE", _cur_or(
              c, "CONN_TYPE", (_platform(c).get("conn") or [""])[0]))).upper()))),
        Q("CREDENTIAL", "Which credential tag?", True, _check_cred_ref, lambda c: _cur_or(c, "CREDENTIAL"),
          lambda c: sorted(t for t in c.cred_tags() if c.host or consumers.allowed_tag(c.cfg, t))),
        *_tags_q(),
        Q("PRODUCTION", "Is this a production device? (yes/no)", True, lambda v, c: _bool(v), _prod_default),
        Q("IS_ENABLED", "Enabled now? (yes/no)", True, lambda v, c: _bool(v), lambda c: _cur_or(c, "IS_ENABLED", True)),
        Q("ADAPTER", "Connection method instead of the platform's default (eapi, ssh, netmiko, pyats, multivendor; "
                     "'default' = the platform's own)?", check=_check_adapter, default=lambda c: _cur_or(c, "ADAPTER"),
          choices=lambda c: ["default", *inventory.ADAPTER_CHOICES]),
    ]


def _check_adapter(v, c: "Ctx"):
    s = str(v or "").strip().lower()
    if s in ("", "default", "none", "auto"):
        return None
    if s not in inventory.ADAPTER_CHOICES:
        raise InterviewError(f"ADAPTER must be one of {', '.join(inventory.ADAPTER_CHOICES)} or default")
    return s


def _controller_q() -> list[Q]:
    return [
        Q("NAME", "Controller name?", True, _check_new_record_name, lambda c: _cur_or(c, "NAME")),
        Q("URL", "URL (https://host[:port])?", True, lambda v, c: _url(v), lambda c: _cur_or(c, "URL")),
        Q("PRODUCT", "Which product?", True, _check_product, lambda c: _cur_or(c, "PRODUCT"),
          lambda c: list(c.cat.controllers) if c.cat else []),
        Q("VENDOR", "Vendor?", True, lambda v, c: str(v).strip().upper(),
          lambda c: _cur_or(c, "VENDOR", ((c.cat.controller(_answer(c, "PRODUCT", "")) or {}).get("vendor")
                                           if c.cat else None))),
        Q("CREDENTIAL", "Which credential tag (usually an API_TOKEN)?", True, _check_cred_ref,
          lambda c: _cur_or(c, "CREDENTIAL")),
        Q("ORG_ID", "Organisation / tenant id (if the product has one)?", check=lambda v, c: str(v).strip(),
          default=lambda c: _cur_or(c, "ORG_ID")),
        Q("MANAGES", "Devices it manages (names, comma separated)?", check=_check_list,
          default=lambda c: _cur_or(c, "MANAGES")),
        *_tags_q(),
        Q("PRODUCTION", "Is this production? (yes/no)", True, lambda v, c: _bool(v), _prod_default),
        Q("IS_ENABLED", "Enabled now? (yes/no)", True, lambda v, c: _bool(v), lambda c: _cur_or(c, "IS_ENABLED", True)),
    ]


def _oob_q() -> list[Q]:
    return [
        Q("NAME", "Name?", True, _check_new_record_name, lambda c: _cur_or(c, "NAME")),
        Q("ADDR", "IP address or DNS name?", True, _check_addr_unique, lambda c: _cur_or(c, "ADDR")),
        Q("TYPE", "Type?", True, _check_oob_type, lambda c: _cur_or(c, "TYPE"),
          lambda c: list(c.cat.oob_types) if c.cat else []),
        Q("VENDOR", "Vendor (e.g. LANTRONIX, OPENGEAR, DELL)?", True, lambda v, c: str(v).strip().upper(),
          lambda c: _cur_or(c, "VENDOR")),
        Q("CONN_TYPE", "How do we connect (SSH, REDFISH, API)?", check=lambda v, c: str(v).strip().upper(),
          default=lambda c: _cur_or(c, "CONN_TYPE")),
        Q("PORT", "Port?", check=lambda v, c: _port(v), default=lambda c: _cur_or(c, "PORT")),
        Q("CREDENTIAL", "Which credential tag?", True, _check_cred_ref, lambda c: _cur_or(c, "CREDENTIAL")),
        Q("PORTS", "Console ports to devices (e.g. 1=R1,2=R2)?", check=_check_ports,
          default=lambda c: _cur_or(c, "PORTS")),
        *_tags_q(),
        Q("IS_ENABLED", "Enabled now? (yes/no)", True, lambda v, c: _bool(v), lambda c: _cur_or(c, "IS_ENABLED", True)),
    ]


def _check_source_answer(v, c: Ctx) -> str:
    s = str(v or "").strip().lower()
    if s not in SOURCE_ANSWERS:
        raise InterviewError(f"answer {', '.join(SOURCE_ANSWERS)}")
    return s


def _cur_source(c: Ctx) -> dict:
    try:
        return dict(((c.inv_doc().get("META") or {}).get("SOURCE")) or {})
    except InterviewError:
        return {}


def _check_kind(v, c: Ctx) -> str:
    s = str(v or "").strip().lower()
    if s not in source.KINDS:
        raise InterviewError("SOURCE is netbox or nautobot (by hand: record_add; a file: bulk_import)")
    return s


def _check_filter(v, c: Ctx) -> dict:
    if v in (None, "", "-", {}):
        return {}
    out = {k.lower(): x for k, x in _pairs(v).items()}
    bad = [k for k in out if k not in FILTER_KEYS]
    if bad:
        raise InterviewError(f"FILTER keys: {', '.join(FILTER_KEYS)} (not {', '.join(bad)})")
    return out


def _check_pmap(v, c: Ctx) -> dict:
    if v in (None, "", "-", {}):
        return {}
    raw = v if isinstance(v, dict) else {}
    if not raw:
        for part in _list(v):
            if "=" not in part:
                raise InterviewError("PLATFORM_MAP: source-platform=OS pairs, e.g. arista-eos=EOS")
            k, x = part.split("=", 1)
            raw[k.strip()] = x.strip()
    return {str(k): _check_os(x, c) for k, x in raw.items()}


def _check_cred_map(v, c: Ctx) -> str:
    try:
        rules = source.parse_cred_map(v)
    except source.SourceError as e:
        raise InterviewError(str(e)) from None
    for _, _, tag in rules:
        _check_cred_ref(tag, c)
    return ", ".join("*=" + t if k == "*" else f"{k}:{val}={t}" for k, val, t in rules)


def _source_q() -> list[Q]:
    cur = lambda c, k, d=None: _cur_source(c).get(k, d)  # noqa: E731
    return [
        Q("SOURCE", "Where is your device list: netbox or nautobot?", True, _check_kind,
          lambda c: cur(c, "TYPE"), lambda c: list(source.KINDS)),
        Q("URL", "Its URL (https://host[:port])?", True, lambda v, c: _url(v), lambda c: cur(c, "URL")),
        Q("AUTH_CREDENTIAL", "The credential tag holding its API token (TYPE API_TOKEN; create it first with "
                             "credential_new and set API_KEY on the host)?", True, _check_cred_ref,
          lambda c: cur(c, "AUTH_CREDENTIAL")),
        Q("VERIFY_TLS", "Check its TLS certificate? (yes/no)", True, lambda v, c: _bool(v),
          lambda c: cur(c, "VERIFY_TLS", True)),
        Q("FILTER", f"Only some devices? key=value pairs ({', '.join(FILTER_KEYS)}), e.g. site=hq, tag=lab; "
                    "'-' = all", check=_check_filter, default=lambda c: cur(c, "FILTER")),
        Q("PLATFORM_MAP", "Platform names the catalogue does not know, source=OS pairs (e.g. arista-eos=EOS); "
                          "'-' = none", check=_check_pmap, default=lambda c: cur(c, "PLATFORM_MAP")),
        Q("CRED_MAP", "Which credential tag each device logs in with, first match wins: ROLE:LEAF=FAB, "
                      "SITE:HQ=LAB_EOS, TAG:lab=LAB_EOS, PLATFORM:PANOS=LAB_PANOS, *=DEFAULT_TAG", True,
          _check_cred_map, lambda c: cur(c, "CRED_MAP")),
    ]


RECORD_QUESTIONS = {"DEVICE": _device_q, "CONTROLLER": _controller_q, "OOB": _oob_q}
SECRET_FIELDS_BY_TYPE = {
    "LOGIN": [("LOGIN_PASSWORD", True), ("ENABLE_PASSWORD", False)],
    "API_TOKEN": [("API_KEY", True)],
    "SNMP": [("COMMUNITY", False), ("AUTH_PASS", False), ("PRIV_PASS", False)],
    "CERT": [("CERT", True), ("PRIVATE_KEY", True), ("PASSPHRASE", False)],
    "SMTP": [("AUTH_PASS", True)],
}


def _ctype(c: Ctx) -> str:
    return str(_answer(c, "TYPE", _cur_or(c, "TYPE") or (c.draft.get("current_type") or "LOGIN"))).upper()


def _credential_q(new: bool) -> list[Q]:
    qs = []
    if new:
        qs += [Q("TAG", "Credential tag: a short label inventories will use for this login "
                        "(A-Z 0-9 _ -, e.g. LAB_EOS)?", True, _check_tag_new),
               Q("TYPE", "Type?", True, _check_ctype, lambda c: "LOGIN", lambda c: list(credentials.CREDENTIAL_TYPES))]
    qs += [Q("DESCRIPTION", "One-line description?", check=lambda v, c: str(v).strip(),
             default=lambda c: _cur_or(c, "DESCRIPTION")),
           Q("USERNAME", "User name? (not a secret, but stored encrypted)",
             required=lambda c, new=new: new and _ctype(c) == "LOGIN",
             check=lambda v, c: str(v), when=lambda c: _ctype(c) in ("LOGIN", "SNMP", "API_TOKEN")),
           Q("AUTH_USER", "SMTP user?", required=new, check=lambda v, c: str(v), when=lambda c: _ctype(c) == "SMTP"),
           Q("HOST", "SMTP host?", required=new, check=lambda v, c: _addr(v), when=lambda c: _ctype(c) == "SMTP",
             default=lambda c: _cur_or(c, "HOST")),
           Q("PORT", "SMTP port?", check=lambda v, c: _port(v), when=lambda c: _ctype(c) == "SMTP",
             default=lambda c: _cur_or(c, "PORT", 587)),
           Q("FROM", "From address?", check=lambda v, c: str(v).strip(), when=lambda c: _ctype(c) == "SMTP",
             default=lambda c: _cur_or(c, "FROM")),
           Q("TLS", "Use TLS? (yes/no)", check=lambda v, c: _bool(v), when=lambda c: _ctype(c) == "SMTP",
             default=lambda c: _cur_or(c, "TLS", True))]
    for t, fields in SECRET_FIELDS_BY_TYPE.items():
        for f, req in fields:
            qs.append(Q(f, f"{f} (secret: set it on the host with atlas-interview secret)",
                        required=new and req, secret=True, when=lambda c, t=t: _ctype(c) == t))
    if not new:
        qs.append(Q("IS_ENABLED", "Enabled? (yes/no)", check=lambda v, c: _bool(v),
                    default=lambda c: _cur_or(c, "IS_ENABLED", True)))
    return qs


def questions(c: Ctx) -> list[Q]:
    topic, rt = c.draft["topic"], c.target.get("record_type", "DEVICE")
    if topic == "inventory_new":
        return [Q("NAME", "Inventory name (A-Z, 0-9, _)?", True, _check_inv_new),
                Q("DESCRIPTION", "One-line description?", check=lambda v, c: str(v).strip()),
                Q("SITE", "Default SITE for new records?", check=lambda v, c: str(v).strip()),
                Q("FAB", "Default FAB for new records?", check=lambda v, c: str(v).strip()),
                Q("PRODUCTION", "Is this inventory production? (yes/no; unanswered counts as yes)", True,
                  lambda v, c: _bool(v), lambda c: True),
                Q("SOURCE", "Where is your device list? hand (record_add), netbox or nautobot (source_import: "
                            "a one-time import, then inventory_sync), or a file (bulk_import)",
                  check=_check_source_answer, default=lambda c: "hand", choices=lambda c: list(SOURCE_ANSWERS))]
    if topic == "source_import":
        return _source_q()
    if topic == "inventory_sync":
        return []
    if topic in ("record_add", "record_edit"):
        return [q for q in RECORD_QUESTIONS[rt]() if q.name not in (
            ("DEVICE_NAME", "DEVICE_ID", "NAME") if topic == "record_edit" else ())]
    if topic == "record_remove":
        return [Q("HARD", "Delete for good instead of disabling? (yes/no)", check=lambda v, c: _bool(v),
                  default=lambda c: False)]
    if topic == "bulk_import":
        return [Q("FORMAT", "yaml or csv?", True, lambda v, c: _fmt(v), lambda c: "yaml", lambda c: ["yaml", "csv"]),
                Q("DATA", "Paste the records (YAML list, or CSV with a header row)", True,
                  lambda v, c: str(v))]
    if topic == "bulk_edit":
        return [Q("MATCH", "Which records? FIELD=value pairs, comma separated; '|' means or; PLATFORM matches "
                           "any OS name of a platform (e.g. PLATFORM=EOS, or PLATFORM=VIPTELA|IOSXE, SITE=HQ)",
                  True, _check_match),
                Q("SET", "What to set? FIELD=value pairs (e.g. CREDENTIAL=LAB_EOS, PORT=443)", True, _check_set)]
    if topic == "inventory_restore":
        return [Q("VERSION", "Which version?", True, _check_version,
                  choices=lambda c: [p.stem for p in store.list_versions(c.cfg, c.inv_path())][:20])]
    if topic == "credential_new":
        return _credential_q(new=True)
    if topic == "credential_update":
        return _credential_q(new=False)
    if topic == "credential_remove":
        return [Q("HARD", "Delete for good instead of disabling? (yes/no)", check=lambda v, c: _bool(v),
                  default=lambda c: False)]
    if topic == "consumer_access":
        cur = lambda c, k: ((c.cfg.consumers or {}).get(c.target["consumer"]) or {}).get(k)  # noqa: E731
        return [Q("INVENTORIES", "Inventories it may use (names or globs, comma separated)?", True, _check_list,
                  lambda c: cur(c, "INVENTORIES")),
                Q("CREDENTIALS", "Credential tags it may use (names or globs)?", True, _check_list,
                  lambda c: cur(c, "CREDENTIALS"))]
    raise InterviewError(f"unknown topic {topic}")


def _fmt(v) -> str:
    s = str(v).strip().lower()
    if s not in ("yaml", "csv"):
        raise InterviewError("FORMAT is yaml or csv")
    return s


# --------------------------------------------------------------------------- drafts
def _drafts_dir(cfg) -> Path:
    return store.state_dir(cfg, "drafts")


def _draft_path(cfg, did: str) -> Path:
    if not re.match(r"^[a-z0-9]{8}$", did or ""):
        raise InterviewError(f"no draft {did!r}")
    return _drafts_dir(cfg) / f"{did}.yml"


def _save(cfg, d: dict) -> None:
    d["updated"] = time.time()
    d.pop("preview_code", None) if d.get("_dirty") else None
    d.pop("_dirty", None)
    store._write_600(_draft_path(cfg, d["id"]), yaml.safe_dump(d, sort_keys=False).encode())


def load_draft(cfg, did: str) -> dict:
    p = _draft_path(cfg, did)
    if not p.is_file():
        raise InterviewError(f"no draft {did!r} (it may have been committed, cancelled or expired)")
    d = yaml.safe_load(p.read_text())
    if time.time() - d.get("updated", 0) > DRAFT_TTL:
        p.unlink(missing_ok=True)
        raise InterviewError(f"draft {did} expired (drafts last 7 days)")
    return d


def list_drafts(cfg) -> list[dict]:
    out = []
    for p in sorted(_drafts_dir(cfg).glob("*.yml")):
        try:
            out.append(load_draft(cfg, p.stem))
        except InterviewError:
            continue
    return out


def _ctx(cfg, d, host) -> Ctx:
    try:
        cat = catalog_mod.Catalog(cfg.catalog_path)
    except catalog_mod.CatalogError:
        cat = None
    return Ctx(cfg, cat, d, host)


def _find_record(doc: dict, rtype: str, name: str) -> dict | None:
    for r in doc.get(inventory.RECORD_LISTS[rtype]) or []:
        if isinstance(r, dict) and name.upper() in {str(r.get(k, "")).upper()
                                                     for k in ("DEVICE_NAME", "DEVICE_ID", "NAME")}:
            return r
    return None


def _cred_entry(cfg, tag: str) -> dict | None:
    return credentials._entries(cfg.credential_file).get(tag)


def start(cfg: config.Config, topic: str, *, host: bool, inventory_name: str | None = None,
          record_type: str = "DEVICE", name: str | None = None, tag: str | None = None,
          consumer: str | None = None) -> dict:
    topic = (topic or "").strip().lower()
    if topic in LATER_TOPICS:
        raise InterviewError(f"topic {topic}: {LATER_TOPICS[topic]}")
    if topic not in TOPICS:
        raise InterviewError(f"topic must be one of {', '.join(TOPICS)}")
    if topic in HOST_ONLY_TOPICS and not host:
        raise InterviewError(f"{topic} changes who may use what, so it runs only on the host: "
                             f"atlas-interview run {topic} --consumer NAME")
    rtype = (record_type or "DEVICE").upper()
    if rtype not in inventory.RECORD_LISTS:
        raise InterviewError("record_type must be DEVICE, CONTROLLER or OOB")
    d = {"id": "".join(pysecrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(8)),
         "topic": topic, "who": "HOST:" + getpass.getuser() if host else cfg.consumer,
         "created": time.time(), "updated": time.time(), "answers": {}, "secrets": {}, "base": {},
         "target": {"record_type": rtype}}
    c = _ctx(cfg, d, host)
    if topic in ("record_add", "record_edit", "record_remove", "bulk_import", "bulk_edit", "inventory_restore",
                 *SOURCE_TOPICS):
        if topic in SOURCE_TOPICS and rtype != "DEVICE":
            raise InterviewError(f"{topic} imports devices only (record_type DEVICE)")
        if not inventory_name:
            raise InterviewError(f"{topic} needs inventory=<name>")
        path = c.inv_path(inventory_name)
        if not path.is_file():
            raise InterviewError(f"inventory {path.stem} not found")
        if not host and not consumers.allowed_inventory(cfg, path.stem):
            raise InterviewError(f"inventory {path.stem} is not available to consumer {cfg.consumer}")
        d["target"]["inventory"] = path.stem.removeprefix("INVENTORY_")
        d["base"][path.name] = store.digest(path)
        if topic in ("record_edit", "record_remove"):
            if not name:
                raise InterviewError(f"{topic} needs name=<record name>")
            rec = _find_record(c.inv_doc(), rtype, name)
            if rec is None:
                raise InterviewError(f"no {rtype.lower()} {name} in {path.stem}")
            d["target"]["name"] = inventory.record_name({**rec, "RECORD_TYPE": rtype})
            d["current"] = rec
        if topic == "inventory_sync" and not _cur_source(c):
            raise InterviewError(f"{path.stem} has no source yet: run topic source_import first")
    if topic.startswith("credential_") and topic != "credential_new":
        t = (tag or "").strip()
        entry = _cred_entry(cfg, t)
        if entry is None:
            raise InterviewError(f"credential {t!r} not found")
        if not host and not consumers.allowed_tag(cfg, t):
            raise InterviewError(f"consumer {cfg.consumer} may not use credential {t}")
        d["target"]["tag"] = t
        info = {i.tag: i for i in credentials.describe(cfg.credential_file)}[t]
        d["current"] = {k: v for k, v in entry.items() if k in vault.NON_SECRET_FIELDS}
        d["current_type"] = info.ctype
    if topic.startswith("credential_"):
        d["base"][cfg.credential_file.name] = store.digest(cfg.credential_file)
    if topic == "consumer_access":
        if not consumer:
            raise InterviewError("consumer_access needs --consumer NAME")
        d["target"]["consumer"] = consumer.strip().upper()
        d["base"][cfg.consumers_file.name] = store.digest(cfg.consumers_file)
    _save(cfg, d)
    return d


def answer(cfg: config.Config, did: str, answers: dict, *, host: bool) -> tuple[dict, list[str]]:
    """Set answers. Returns (draft, problems). Valid answers are kept even if others fail."""
    d = load_draft(cfg, did)
    c = _ctx(cfg, d, host)
    qs = _active(c)
    problems = []
    for k, v in (answers or {}).items():
        k = str(k).strip().upper()
        if k not in qs:
            if inventory.SECRET_KEY.search(k) and k not in NOT_SECRET:
                problems.append(f"{k}: secrets are never given in chat. If you typed one, change it, "
                                f"then set it on the host: atlas-interview secret {did} {k}")
            else:
                problems.append(f"{k}: not a question in this interview (ask: {', '.join(qs)})")
            continue
        q = qs[k]
        if q.secret:
            problems.append(f"{k}: secrets are never given in chat. If you typed one, change it, then "
                            f"set it on the host: atlas-interview secret {did} {k}")
            continue
        try:
            d["answers"][k] = q.check(v, c) if q.check else v
            d["_dirty"] = True
        except InterviewError as e:
            problems.append(f"{k}: {e}")
    if "HARD" in d["answers"] and d["answers"]["HARD"]:
        d["answers"]["HARD"] = False
        problems.append("HARD: a hard delete needs the out-of-band /pushok approval, which arrives with "
                        "RUDDER's approver (P4). For now the record is disabled instead.")
    _save(cfg, d)
    return d, problems


def set_secret(cfg: config.Config, did: str, field_name: str, value: str) -> dict:
    """Host only: encrypt a secret into the draft at once. Needs the vault key."""
    d = load_draft(cfg, did)
    c = _ctx(cfg, d, True)
    f = field_name.strip().upper()
    q = _active(c).get(f)
    if q is None or not (q.secret or f in NOT_SECRET):
        raise InterviewError(f"{f} is not a secret field of this interview")
    tag = d["answers"].get("TAG") or d["target"].get("tag")
    if not tag:
        raise InterviewError("answer TAG first: the secret is bound to its tag when encrypted")
    if not value:
        raise InterviewError("empty value; nothing stored")
    try:
        key = vault.load_key(cfg)
    except vault.VaultKeyError as e:
        raise InterviewError(f"{e}. Create the key with `atlas-vault init` and back it up first "
                             f"(MIGRATION.md)") from None
    d["secrets"][f] = vault.encrypt_value(key, tag, f, value)
    del key
    d["_dirty"] = True
    _save(cfg, d)
    return d


def cancel(cfg, did: str) -> None:
    _draft_path(cfg, did).unlink(missing_ok=True)


# --------------------------------------------------------------------------- status
def open_questions(c: Ctx) -> list[dict]:
    out = []
    for q in questions(c):
        if (not q.when(c) or q.name in c.draft["answers"] or q.name in c.draft["secrets"]
                or q.name in c.draft.get("skipped", [])):
            continue
        default = q.default(c) if q.default else None
        out.append({"field": q.name, "question": q.text, "required": _req(q, c), "secret": q.secret,
                    "default": default, "choices": (q.choices(c) if q.choices else None) or None})
    return out


def status(cfg, did: str, *, host: bool) -> dict:
    d = load_draft(cfg, did)
    c = _ctx(cfg, d, host)
    open_q = open_questions(c)
    need = [q for q in open_q if q["required"] and (q["default"] is None or q["secret"])]
    return {"draft": d["id"], "topic": d["topic"], "target": d["target"],
            "answers": {k: ("(set; stored encrypted)" if k in NOT_SECRET else v) for k, v in d["answers"].items()},
            "secrets_set": sorted(d["secrets"]),
            "next_questions": [q for q in open_q if q["required"]][:5] + [q for q in open_q if not q["required"]][:5],
            "ready_for_preview": not need,
            "waiting_for": [f"{q['field']} (secret: atlas-interview secret {did} {q['field']} on the host)"
                            if q["secret"] else q["field"] for q in need]}


# --------------------------------------------------------------------------- building the change
def _value(c: Ctx, q: Q):
    if q.name in c.draft["answers"]:
        return c.draft["answers"][q.name]
    return q.default(c) if q.default else None


def _record_from_answers(c: Ctx, rtype: str, base: dict | None = None) -> dict:
    rec = dict(base or {})
    tags = dict((base or {}).get("TAGS") or {})
    for q in RECORD_QUESTIONS[rtype]():
        if c.draft["topic"] == "record_edit" and q.name not in c.draft["answers"]:
            continue
        v = _value(c, q)
        if v is None or v == [] or v == {} or v == "":
            if _req(q, c) and c.draft["topic"] != "record_edit":
                raise InterviewError(f"{q.name} is not answered")
            if q.name in inventory.TAGS_FIELDS:
                tags.pop(q.name, None)
            if q.name == "ADAPTER":            # 'default' = back to the platform's own method
                rec.pop("ADAPTER", None)
            continue
        if q.name in inventory.TAGS_FIELDS:
            tags[q.name] = v
        else:
            rec[q.name] = v
    if tags:
        rec["TAGS"] = tags
    else:
        rec.pop("TAGS", None)
    return rec


def _parse_bulk(c: Ctx) -> tuple[list[dict], list[str]]:
    fmt, data = c.draft["answers"].get("FORMAT", "yaml"), c.draft["answers"].get("DATA") or ""
    rows: list[dict] = []
    if fmt == "yaml":
        raw = yaml.safe_load(data) or []
        if isinstance(raw, dict):
            raw = raw.get(inventory.RECORD_LISTS[c.target["record_type"]]) or []
        if not isinstance(raw, list):
            raise InterviewError("YAML must be a list of records")
        rows = [r for r in raw if isinstance(r, dict)]
    else:
        rows = list(csv.DictReader(io.StringIO(data)))
    out, problems = [], []
    rtype = c.target["record_type"]
    for i, row in enumerate(rows, 1):
        row = {str(k).strip().upper(): v for k, v in row.items() if k is not None and v not in (None, "")}
        for k in row:
            if inventory.SECRET_KEY.search(k):
                problems.append(f"row {i}: column {k} looks like a secret; remove it")
        sub = {"topic": "record_add", "answers": {}, "secrets": {}, "target": c.target}
        sc = Ctx(c.cfg, c.cat, sub, c.host)
        tags = row.pop("TAGS", None)
        if isinstance(tags, dict):
            row.update({k.upper(): v for k, v in tags.items()})
        for q in RECORD_QUESTIONS[rtype]():
            if q.name in row:
                try:
                    sub["answers"][q.name] = q.check(row[q.name], sc) if q.check else row[q.name]
                except InterviewError as e:
                    problems.append(f"row {i} {q.name}: {e}")
        try:
            out.append(_record_from_answers(sc, rtype))
        except InterviewError as e:
            problems.append(f"row {i}: {e}")
    names = [inventory.record_name({**r, "RECORD_TYPE": rtype}).upper() for r in out]
    for n in {x for x in names if names.count(x) > 1}:
        problems.append(f"{n} appears more than once in the import")
    return out, problems


@dataclass
class Change:
    files: dict[Path, str]                    # path -> new text (inventories, consumers file)
    cred_doc: dict | None = None              # new CREDENTIALS.yml document
    summary: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=120)


def build(cfg: config.Config, d: dict, *, host: bool) -> Change:
    c = _ctx(cfg, d, host)
    st = status(cfg, d["id"], host=host)
    if not st["ready_for_preview"]:
        raise InterviewError(f"not ready: still waiting for {', '.join(st['waiting_for'])}")
    topic, rtype, ch = d["topic"], d["target"]["record_type"], Change(files={})
    a = d["answers"]

    if topic == "inventory_new":
        name = a["NAME"]
        defaults = {k: a[k] for k in ("SITE", "FAB") if a.get(k)}
        defaults["PRODUCTION"] = a.get("PRODUCTION", True)
        doc = {"META": {"DESCRIPTION": a.get("DESCRIPTION", ""), "DEFAULTS": defaults,
                        "CREATED": time.strftime("%Y-%m-%d"), "CREATED_BY": d["who"]},
               "INVENTORY": []}
        ch.files[c.inv_path(name)] = _dump(doc)
        nxt = {"netbox": "source_import (NetBox)", "nautobot": "source_import (Nautobot)",
               "file": "bulk_import"}.get(a.get("SOURCE") or "hand", "record_add or bulk_import")
        ch.summary.append(f"new inventory INVENTORY_{name} (no records yet; next: topic {nxt} "
                          f"with inventory={name})")

    elif topic in SOURCE_TOPICS:
        path = c.inv_path()
        doc = c.inv_doc()
        meta = doc.get("META") if isinstance(doc.get("META"), dict) else {}
        doc["META"] = meta
        if topic == "source_import":
            src = {"TYPE": a["SOURCE"], "URL": a["URL"], "AUTH_CREDENTIAL": a["AUTH_CREDENTIAL"],
                   "VERIFY_TLS": a.get("VERIFY_TLS", True)}
            src.update({k: a[k] for k in ("FILTER", "PLATFORM_MAP") if a.get(k)})
            src["CRED_MAP"] = a["CRED_MAP"]
            if src != meta.get("SOURCE"):
                ch.summary.append(f"source: {src['TYPE']} {src['URL']}, token from {src['AUTH_CREDENTIAL']}, "
                                  f"credentials {src['CRED_MAP']}" + (f", filter {src['FILTER']}" if src.get("FILTER") else ""))
            meta["SOURCE"] = src
        else:
            src = dict(meta.get("SOURCE") or {})
        devs = None
        try:
            s = source.open_source(cfg, src, None if host else cfg.consumer)
            devs = s.devices(src.get("FILTER"))
        except (source.SourceError, vault.VaultError, PermissionError) as e:
            ch.errors.append(f"could not read {src.get('TYPE')} {src.get('URL')}: {e}")
        if devs is not None:
            try:
                p = source.plan(doc.get("INVENTORY") or [], devs, src, c.cat, c.cred_tags(),
                                meta.get("DEFAULTS") or {})
            except source.SourceError as e:
                ch.errors.append(str(e))
            else:
                doc["INVENTORY"] = p.records
                ch.summary += p.summary(f"{src['TYPE']} ({len(devs)} device(s) read)")
                ch.warnings += p.problems
                ch.errors += p.errors
        text = _dump(doc)
        if text != path.read_text() and path.read_text().strip():
            ch.warnings.append(f"{path.name} is written in a standard layout (its own comments are not kept)")
        ch.files[path] = text
        rep = inventory.validate_text(text, credentials.tags(cfg.credential_file), catalog=c.cat)
        ch.errors += rep.errors
        ch.warnings += [w for w in rep.warnings if "no records yet" not in w]

    elif topic in ("record_add", "record_edit", "record_remove", "bulk_import", "bulk_edit", "inventory_restore"):
        path = c.inv_path()
        if topic == "inventory_restore":
            text = store.read_version(cfg, path, a["VERSION"])
            ch.files[path] = text
            ch.summary.append(f"restore {path.stem} to version {a['VERSION']}")
            doc = yaml.safe_load(text) or {}
        else:
            orig = path.read_text()
            doc = c.inv_doc()
            key = inventory.RECORD_LISTS[rtype]
            lst = doc.get(key)
            if lst is None:
                lst = doc[key] = []
            patch = None                                   # how to write the change as a small text edit
            if topic == "record_add":
                rec = _record_from_answers(c, rtype)
                lst.append(rec)
                patch = lambda: yamlpatch.append_records(orig, key, [rec], doc)  # noqa: E731
                ch.summary.append(f"add {rtype.lower()} {inventory.record_name({**rec, 'RECORD_TYPE': rtype})}")
            elif topic == "bulk_edit":
                match, setv = a["MATCH"], a["SET"]
                text_now, work = orig, yaml.safe_load(orig) or {}
                changed_names, patched_ok = [], True
                for idx, cur in enumerate(list(lst)):
                    if not isinstance(cur, dict) or not _matches(c, cur, match):
                        continue
                    new = dict(cur)
                    tags = dict(cur.get("TAGS") or {})
                    for k, v in setv.items():
                        if k in inventory.TAGS_FIELDS:
                            tags[k] = v
                        elif v is None and k == "ADAPTER":
                            new.pop(k, None)
                        else:
                            new[k] = v
                    if tags:
                        new["TAGS"] = tags
                    if new == cur:
                        continue
                    lst[idx] = new
                    work[key][idx] = new
                    changed_names.append(inventory.record_name({**new, "RECORD_TYPE": rtype}))
                    if patched_ok:
                        t2 = yamlpatch.edit_record(text_now, key, idx, cur, new, work)
                        if t2 is None:
                            patched_ok = False
                        else:
                            text_now = t2
                if not changed_names:
                    ch.errors.append("no record matches, or they already have these values")
                ch.summary.append(f"set {', '.join(f'{k}={v}' for k, v in setv.items())} on "
                                  f"{len(changed_names)} {rtype.lower()} record(s): {', '.join(changed_names)}")
                patch = (lambda t=text_now: t) if patched_ok else None
            elif topic == "bulk_import":
                recs, probs = _parse_bulk(c)
                ch.errors += probs
                existing = {inventory.record_name({**r, "RECORD_TYPE": rtype}).upper() for r in lst if isinstance(r, dict)}
                for r in recs:
                    n = inventory.record_name({**r, "RECORD_TYPE": rtype})
                    if n.upper() in existing:
                        ch.errors.append(f"{n} already exists in {path.stem}; use record_edit")
                lst.extend(recs)
                patch = lambda: yamlpatch.append_records(orig, key, recs, doc)  # noqa: E731
                ch.summary.append(f"import {len(recs)} {rtype.lower()} record(s)")
            else:
                cur = _find_record(doc, rtype, d["target"]["name"])
                if cur is None:
                    raise InterviewError(f"{d['target']['name']} is no longer in {path.stem}")
                idx = lst.index(cur)
                if topic == "record_edit":
                    new = _record_from_answers(c, rtype, cur)
                    changed = sorted(k for k in set(new) | set(cur) if new.get(k) != cur.get(k))
                    ch.summary.append(f"edit {d['target']['name']}: {', '.join(changed) or 'no change'}")
                else:
                    new = {**cur, "IS_ENABLED": False}
                    ch.summary.append(f"disable {d['target']['name']} (soft delete)")
                lst[idx] = new
                patch = lambda: yamlpatch.edit_record(orig, key, idx, cur, new, doc)  # noqa: E731
            text = patch() if patch else None
            if text is None:
                text = _dump(doc)
                if orig.strip():
                    ch.warnings.append(f"{path.name} will be rewritten in a standard layout "
                                       f"(its own formatting and comments are not kept)")
            ch.files[path] = text
        rep = inventory.validate_text(ch.files[path], credentials.tags(cfg.credential_file), catalog=c.cat)
        ch.errors += rep.errors
        ch.warnings += rep.warnings

    elif topic.startswith("credential_"):
        path = cfg.credential_file
        raw = vault._read_doc(path) if path.is_file() else {"DATA": []}
        entries = dict(vault._entries(raw))
        if topic == "credential_new":
            tag = a["TAG"]
            entry = {"TAG": tag, "TYPE": a["TYPE"]}
            if isinstance(raw["DATA"], list):
                raw["DATA"].append(entry)
            else:
                raw["DATA"][tag] = entry
                entry.pop("TAG")
        else:
            tag = d["target"]["tag"]
            entry = entries[tag]
        if topic == "credential_remove":
            users = _tag_users(cfg, tag)
            if users:
                ch.errors.append(f"{tag} is used by {', '.join(users)}; move those records to another tag first")
            entry["IS_ENABLED"] = False
            ch.summary.append(f"disable credential {tag} (soft delete)")
        else:
            changed = []
            for q in _active(c).values():
                if q.name in ("TAG", "TYPE"):
                    continue
                if q.name in d["secrets"]:
                    entry[q.name] = d["secrets"][q.name]
                    changed.append(f"{q.name} (encrypted)")
                elif q.name in a:
                    v = a[q.name]
                    if q.name in NOT_SECRET:
                        entry[q.name] = ("__ENCRYPT__", v)          # encrypted below, with the key
                        changed.append(f"{q.name} (encrypted)")
                    else:
                        entry[q.name] = v
                        changed.append(q.name)
            fields = {k for k, v in entry.items() if k not in vault.NON_SECRET_FIELDS and v not in (None, "")}
            miss = credentials.missing_fields(str(entry.get("TYPE") or d.get("current_type") or "LOGIN"), fields)
            if miss:
                ch.errors.append(f"{tag}: missing {', '.join(miss)}")
            ch.summary.append(f"{'new' if topic == 'credential_new' else 'update'} credential {tag}: "
                              f"{', '.join(changed) or 'no change'}")
        ch.cred_doc = raw

    elif topic == "consumer_access":
        who = d["target"]["consumer"]
        cur = dict(cfg.consumers or {})
        if cfg.consumers is None:
            ch.warnings.append("today there is no CONSUMERS list, so every consumer may use everything. "
                               "After this commit only the consumers listed here get access.")
        cur[who] = {"INVENTORIES": a["INVENTORIES"], "CREDENTIALS": a["CREDENTIALS"]}
        ch.files[cfg.consumers_file] = _dump({"CONSUMERS": cur})
        ch.summary.append(f"access for {who}: inventories {a['INVENTORIES']}, credentials {a['CREDENTIALS']}")
    return ch


def _tag_users(cfg, tag: str) -> list[str]:
    users = []
    for n in inventory.list_names(cfg.data_path):
        try:
            recs = inventory.load_all(inventory.resolve(cfg.data_path, n))
        except inventory.InventoryError:
            continue
        for r in (x for v in recs.values() for x in v):
            if str(r.get("CREDENTIAL")) == tag:
                users.append(f"{n}/{inventory.record_name(r)}")
    return users


def _code(d: dict, ch: Change) -> str:
    h = hashlib.sha256()
    h.update(yaml.safe_dump({"a": d["answers"], "s": sorted(d["secrets"]), "b": d["base"]}).encode())
    for p, t in sorted(ch.files.items()):
        h.update(str(p).encode() + t.encode())
    return h.hexdigest()[:6].upper()


def preview(cfg, did: str, *, host: bool) -> dict:
    d = load_draft(cfg, did)
    ch = build(cfg, d, host=host)
    diffs = []
    for p, text in ch.files.items():
        old = p.read_text() if p.is_file() else ""
        diffs.append("".join(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                                  f"{p.name} (now)", f"{p.name} (after)", n=2)))
    code = None if ch.errors else _code(d, ch)
    d["preview_code"] = code
    _save(cfg, d)
    return {"draft": did, "summary": ch.summary, "errors": ch.errors, "warnings": ch.warnings,
            "diff": "\n".join(x for x in diffs if x) or "(no file text changes shown; credential values are never shown)",
            "commit_code": code,
            "next": (f"Show this to the user. On their yes: interview_commit(draft='{did}', code='{code}')"
                     if code else "Fix the errors (interview_answer), then preview again.")}


def commit(cfg, did: str, code: str, *, host: bool) -> list[str]:
    d = load_draft(cfg, did)
    if not d.get("preview_code"):
        raise InterviewError("run preview first and show it to the user")
    ch = build(cfg, d, host=host)
    if ch.errors:
        raise InterviewError("cannot commit: " + "; ".join(ch.errors))
    if str(code).strip().upper() != _code(d, ch) or str(code).strip().upper() != d["preview_code"]:
        raise InterviewError("the code does not match the last preview (the draft or a file changed); "
                             "preview again")
    done = []
    action = f"interview:{d['topic']}:{did}"
    for p, text in ch.files.items():
        expect = d["base"].get(p.name, None) if p.name in d["base"] else None
        store.write_versioned(cfg, p, text, d["who"], action, "; ".join(ch.summary), expect_digest=expect)
        done.append(f"wrote {p.name} (previous version kept)")
    if ch.cred_doc is not None:
        raw = ch.cred_doc
        need_key = any(isinstance(v, tuple) for _, e in vault._entries(raw) for v in e.values())
        if need_key or d["secrets"]:
            try:
                key = vault.load_key(cfg)
            except vault.VaultKeyError as e:
                raise InterviewError(f"{e}. Create the key with `atlas-vault init` and back it up first "
                                     f"(MIGRATION.md)") from None
            vault._decrypt_all(key, raw)      # existing values must open with this key: never mix keys
            for t, e in vault._entries(raw):
                for k, v in list(e.items()):
                    if isinstance(v, tuple) and v[0] == "__ENCRYPT__":
                        e[k] = vault.encrypt_value(key, t, k, str(v[1]))
            del key
        path = cfg.credential_file
        if store.digest(path) != d["base"].get(path.name):
            raise InterviewError(f"{path.name} changed since this draft was made; start a new draft")
        # The previous file may hold plain values, so it is kept next to it (like atlas-vault does),
        # not in STATE_PATH: secrets stay in one place.
        backup = vault._backup(path) if path.is_file() else None
        if path.is_file():
            vault._atomic_write(path, raw)
        else:
            store._write_600(path, vault._dump(raw))
        store.audit(cfg, d["who"], action, f"{path.name}: " + "; ".join(ch.summary))
        done.append(f"wrote {path.name} (new values encrypted; previous file kept as "
                    f"{backup.name if backup else '-'})")
    cancel(cfg, did)
    return done + ch.summary


# --------------------------------------------------------------------------- CLI
def _print(obj) -> None:
    print(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=110).rstrip())


def _interactive(cfg, d: dict) -> int:
    did = d["id"]
    tgt = d["target"]
    shown = {k: v for k, v in tgt.items() if k != "record_type" or d["topic"].startswith(("record_", "bulk_"))}
    what = ", ".join(f"{k}={v}" for k, v in shown.items())
    print(f"draft {did}: {d['topic']}{' (' + what + ')' if what else ''}  "
          f"(Ctrl-C to stop; resume with: atlas-interview run --draft {did})")
    while True:
        c = _ctx(cfg, load_draft(cfg, did), True)
        pending = [q for q in open_questions(c)]
        if not pending:
            break
        q = pending[0]
        hint = f" [{q['default']}]" if q["default"] not in (None, "", []) else ""
        ch = f" ({', '.join(map(str, q['choices']))})" if q["choices"] else ""
        if q["secret"]:
            v = getpass.getpass(f"{q['question']}{' (required)' if q['required'] else ' (Enter = skip)'}: ")
            if v:
                try:
                    set_secret(cfg, did, q["field"], v)
                except InterviewError as e:
                    print("  !", e)
                    return 1
                continue
            if q["required"]:
                print("  ! required")
                continue
            _skip(cfg, did, q["field"])
            continue
        v = input(f"{q['question']}{ch}{hint}: ").strip()
        if not v:
            if q["default"] not in (None, "", []):
                v = q["default"]
            elif q["required"]:
                print("  ! required")
                continue
            else:
                _skip(cfg, did, q["field"])
                continue
        _, probs = answer(cfg, did, {q["field"]: v}, host=True)
        for p in probs:
            print("  !", p)
    pv = preview(cfg, did, host=True)
    print("\n".join(["", "SUMMARY:"] + [f"  {s}" for s in pv["summary"]]))
    for w in pv["warnings"]:
        print("  WARNING", w)
    if pv["diff"]:
        print(pv["diff"])
    if pv["errors"]:
        print("\n".join(f"  ERROR {e}" for e in pv["errors"]))
        print(f"Fix with: atlas-interview answer {did} FIELD=VALUE, then: atlas-interview preview {did}")
        return 1
    if input("Commit this? (yes/no): ").strip().lower() in ("y", "yes"):
        for line in commit(cfg, did, pv["commit_code"], host=True):
            print("  ", line)
        return 0
    print(f"Not committed. The draft stays for 7 days: atlas-interview commit {did} {pv['commit_code']}")
    return 0


def _skip(cfg, did, fld) -> None:
    """Leave an optional question unanswered (keeps the current value when editing)."""
    d = load_draft(cfg, did)
    d.setdefault("skipped", []).append(fld)
    _save(cfg, d)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas-interview", description="Create, update and remove ATLAS "
                                 "inventories, records, credentials and access. Secrets are asked with getpass.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="interactive interview")
    r.add_argument("topic", nargs="?", choices=TOPICS)
    r.add_argument("--draft")
    for p in (r, s := sub.add_parser("start")):
        p.add_argument("--inventory")
        p.add_argument("--type", default="DEVICE")
        p.add_argument("--name")
        p.add_argument("--tag")
        p.add_argument("--consumer")
    s.add_argument("topic", choices=TOPICS)
    a_ = sub.add_parser("answer")
    a_.add_argument("draft")
    a_.add_argument("pairs", nargs="+", help="FIELD=VALUE")
    se = sub.add_parser("secret", help="set a secret field (asked with getpass, or --file)")
    se.add_argument("draft")
    se.add_argument("field")
    se.add_argument("--file", help="read the value from a file (certificates, keys)")
    for name in ("status", "preview", "cancel"):
        sub.add_parser(name).add_argument("draft")
    co = sub.add_parser("commit")
    co.add_argument("draft")
    co.add_argument("code")
    sub.add_parser("list")
    h = sub.add_parser("history", help="versions of an inventory")
    h.add_argument("inventory")
    args = ap.parse_args(argv)
    cfg = config.load()
    try:
        if args.cmd == "run":
            if args.draft:
                return _interactive(cfg, load_draft(cfg, args.draft))
            if not args.topic:
                ap.error("give a topic or --draft")
            return _interactive(cfg, start(cfg, args.topic, host=True, inventory_name=args.inventory,
                                           record_type=args.type, name=args.name, tag=args.tag,
                                           consumer=args.consumer))
        if args.cmd == "start":
            d = start(cfg, args.topic, host=True, inventory_name=args.inventory, record_type=args.type,
                      name=args.name, tag=args.tag, consumer=args.consumer)
            _print(status(cfg, d["id"], host=True))
        elif args.cmd == "answer":
            pairs = dict(p.split("=", 1) for p in args.pairs if "=" in p)
            _, probs = answer(cfg, args.draft, pairs, host=True)
            _print({"problems": probs, **status(cfg, args.draft, host=True)})
        elif args.cmd == "secret":
            v = Path(args.file).read_text() if args.file else getpass.getpass(f"{args.field.upper()}: ")
            set_secret(cfg, args.draft, args.field, v)
            print(f"{args.field.upper()} stored encrypted in draft {args.draft}")
        elif args.cmd == "status":
            _print(status(cfg, args.draft, host=True))
        elif args.cmd == "preview":
            pv = preview(cfg, args.draft, host=True)
            print("\n".join(["SUMMARY:"] + [f"  {s}" for s in pv["summary"]]
                            + [f"  WARNING {w}" for w in pv["warnings"]] + [f"  ERROR {e}" for e in pv["errors"]]))
            print(pv["diff"])
            print(f"commit with: atlas-interview commit {args.draft} {pv['commit_code']}" if pv["commit_code"]
                  else "fix the errors first")
        elif args.cmd == "commit":
            for line in commit(cfg, args.draft, args.code, host=True):
                print(line)
        elif args.cmd == "cancel":
            cancel(cfg, args.draft)
            print(f"draft {args.draft} cancelled; nothing was written")
        elif args.cmd == "list":
            for d in list_drafts(cfg):
                print(f"{d['id']}  {d['topic']}  {d['target']}  by {d['who']}  "
                      f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(d['updated']))}")
        elif args.cmd == "history":
            path = inventory.resolve(cfg.data_path, args.inventory)
            for p in store.list_versions(cfg, path):
                print(p.stem)
        return 0
    except (InterviewError, store.StoreError, inventory.InventoryError, vault.VaultError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped; the draft is kept for 7 days")
        return 130


if __name__ == "__main__":
    sys.exit(main())
