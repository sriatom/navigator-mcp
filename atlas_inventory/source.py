"""Import an ATLAS inventory from NetBox or Nautobot, and keep it in sync (decision #160, task 17).

The other direction of netbox.py (export). ATLAS owns the inventory file; NetBox / Nautobot stays the
source of truth for the fields it owns, and only for records it brought in.

    interview topic source_import   once: where the list is (netbox | nautobot), URL, the API token's
                                    credential tag, filters, platform names, and the credential map;
                                    saved in the inventory's META.SOURCE; then the first import.
    interview topic inventory_sync  later, on demand: the same source again.
Both go through the interview's preview (the summary of what is added, changed and disabled, plus the
file diff) and commit with its code, so nothing is written before the user saw it. A source that changed
between the preview and the commit changes the code: preview again.

Fields the source owns (overwritten on sync): DEVICE_NAME, DEVICE_ADDR (primary IP), OS (platform), VENDOR,
SITE, ROLE, IS_ENABLED (status active). Fields that stay ours: CREDENTIAL (set from the map when a record
is added, or when it has none), CONN_TYPE, PORT, ADAPTER, FAB, PRODUCTION. Each imported record carries
SOURCE_ID (e.g. netbox:42). A device that left the source (or the filter) is disabled, never deleted.
Records added by hand (no SOURCE_ID) are never touched, except that a hand record with the same name is
adopted (it gets SOURCE_ID and the source's fields).

Credentials: NetBox has no credential store of its own and Nautobot's Secrets point elsewhere, so the
credential tags are asked when the inventory is built and mapped by role, tag, site or platform:
    CRED_MAP  "ROLE:LEAF=FAB, SITE:HQ=LAB_EOS, TAG:lab=LAB_EOS, PLATFORM:PANOS=LAB_PANOS, *=DEFAULT"
first match wins. The API token is an ATLAS credential (TYPE API_TOKEN) named by META.SOURCE.AUTH_CREDENTIAL,
read server-side and never returned.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

import httpx

from . import config, inventory, vault

KINDS = ("netbox", "nautobot")
OWNED = ("DEVICE_NAME", "DEVICE_ADDR", "OS", "VENDOR", "IS_ENABLED")
OWNED_TAGS = ("SITE", "ROLE")
MATCH_KEYS = ("ROLE", "SITE", "TAG", "PLATFORM")
DEFAULT_PORTS = {"SSH": 22, "TELNET": 23, "EAPI": 443, "HTTPS": 443, "API": 443, "XML_API": 443,
                 "NXAPI": 443, "RESTCONF": 443, "VMANAGE_API": 443, "NETCONF": 830, "GNMI": 6030}
PAGE = 500
MAX_DEVICES = 20000
TRANSPORT: httpx.BaseTransport | None = None      # tests put an httpx.MockTransport here
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class SourceError(Exception):
    pass


@dataclass
class Device:
    """One device as the source has it, in the shape both sources share."""
    sid: str                    # "netbox:42"
    name: str
    addr: str | None
    platform: list[str]         # names to try against the catalogue (slug, name, driver)
    vendor: str
    site: str
    role: str
    tags: list[str] = field(default_factory=list)
    active: bool = True


# --------------------------------------------------------------------------- the two sources
def _get(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


def _ip(v) -> str | None:
    a = _get(v, "address") if isinstance(v, dict) else v
    if not a:
        return None
    try:
        return str(ipaddress.ip_interface(str(a)).ip)
    except ValueError:
        return None


class Source:
    kind = "netbox"
    params: dict = {}

    def __init__(self, url: str, token: str, verify: bool = True, transport: httpx.BaseTransport | None = None):
        u = str(url or "").rstrip("/")
        if not re.match(r"^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?$", u):
            raise SourceError("URL: give https://host[:port][/path], without a user name or password")
        self.http = httpx.Client(base_url=u + "/api/", timeout=30, verify=verify,
                                 transport=transport if transport is not None else TRANSPORT,
                                 headers={"Authorization": f"Token {token}", "Accept": "application/json"})

    def _rows(self, ep: str, params: dict) -> list[dict]:
        out, url, q = [], ep, {**self.params, **params, "limit": PAGE}
        while url:
            try:
                r = self.http.get(url, params=q)
            except httpx.HTTPError as e:
                raise SourceError(f"{self.kind}: cannot reach {self.http.base_url} ({type(e).__name__})") from None
            if r.status_code in (401, 403):
                raise SourceError(f"{self.kind}: HTTP {r.status_code}: the API token was refused")
            if r.status_code >= 400:
                raise SourceError(f"{self.kind} GET {r.request.url.path}: HTTP {r.status_code}")
            body = r.json()
            out += body.get("results") or []
            if len(out) > MAX_DEVICES:
                raise SourceError(f"more than {MAX_DEVICES} devices: narrow it with FILTER")
            url, q = body.get("next"), None        # 'next' already carries the query
        return out

    def device(self, row: dict) -> Device:
        tags = [str(_get(t, "slug") or _get(t, "name") or t) for t in row.get("tags") or []]
        status = _get(row, "status", "value") or _get(row, "status", "name") or "active"
        return Device(sid=f"{self.kind}:{row.get('id')}", name=str(row.get("name") or "").strip(),
                      addr=_ip(row.get("primary_ip4")) or _ip(row.get("primary_ip6")) or _ip(row.get("primary_ip")),
                      platform=list(dict.fromkeys(str(x) for x in (
                          _get(row, "platform", "slug"), _get(row, "platform", "name"),
                          _get(row, "platform", "network_driver"), _get(row, "platform", "napalm_driver")) if x)),
                      vendor=str(_get(row, "device_type", "manufacturer", "name") or ""),
                      site=str(_get(row, "site", "slug") or _get(row, "site", "name") or ""),
                      role=str(_get(row, "role", "slug") or _get(row, "device_role", "slug")
                               or _get(row, "role", "name") or ""),
                      tags=tags, active=str(status).lower() == "active")

    def devices(self, flt: dict | None = None) -> list[Device]:
        return [self.device(r) for r in self._rows("dcim/devices/", dict(flt or {}))]


class NetBox(Source):
    kind = "netbox"
    params = {"brief": "false"}


class Nautobot(Source):
    """Nautobot 2.x: location instead of site, names instead of slugs, depth for nested objects."""
    kind = "nautobot"
    params = {"depth": 2}

    def device(self, row: dict) -> Device:
        d = super().device(row)
        d.site = str(_get(row, "location", "name") or d.site)
        d.role = str(_get(row, "role", "name") or d.role)
        return d


def client(kind: str, url: str, token: str, verify: bool = True, transport=None) -> Source:
    k = str(kind or "").lower()
    if k not in KINDS:
        raise SourceError(f"SOURCE must be one of {', '.join(KINDS)}")
    return (NetBox if k == "netbox" else Nautobot)(url, token, verify, transport)


def open_source(cfg: config.Config, src: dict, consumer: str | None = None, transport=None) -> Source:
    """A client for META.SOURCE, with the token read from the credential store (never returned)."""
    tag = str(src.get("AUTH_CREDENTIAL") or "")
    if not tag:
        raise SourceError("META.SOURCE.AUTH_CREDENTIAL is not set (an API_TOKEN credential tag)")
    cred = vault.get_credential(cfg, tag, consumer)
    token = cred.get("API_KEY") or cred.get("TOKEN")
    if not token:
        raise SourceError(f"credential {tag} has no API_KEY")
    try:
        return client(src.get("TYPE"), src.get("URL"), token, bool(src.get("VERIFY_TLS", True)), transport)
    finally:
        del token, cred


# --------------------------------------------------------------------------- mapping
def parse_cred_map(v) -> list[tuple[str, str, str]]:
    """'ROLE:LEAF=FAB, *=DEFAULT' -> [(ROLE, LEAF, FAB), (*, *, DEFAULT)]."""
    if isinstance(v, list) and all(isinstance(x, (list, tuple)) for x in v):
        return [tuple(str(y) for y in x) for x in v]
    rules = []
    for part in [p.strip() for p in str(v or "").split(",") if p.strip()]:
        if "=" not in part:
            raise SourceError(f"CRED_MAP {part!r}: use KIND:VALUE=CREDENTIAL (ROLE, SITE, TAG, PLATFORM) or *=CREDENTIAL")
        lhs, tag = (x.strip() for x in part.split("=", 1))
        if lhs == "*":
            rules.append(("*", "*", tag))
            continue
        if ":" not in lhs or lhs.split(":", 1)[0].upper() not in MATCH_KEYS:
            raise SourceError(f"CRED_MAP {part!r}: match on ROLE:, SITE:, TAG: or PLATFORM:")
        k, val = lhs.split(":", 1)
        rules.append((k.upper(), val.strip(), tag))
    if not rules:
        raise SourceError("CRED_MAP: give at least one rule, e.g. *=LAB_EOS")
    return rules


def cred_for(rules: list[tuple[str, str, str]], dev: Device, pid: str | None) -> str | None:
    for k, val, tag in rules:
        v = val.upper()
        if k == "*" or (k == "ROLE" and dev.role.upper() == v) or (k == "SITE" and dev.site.upper() == v) \
                or (k == "TAG" and v in {t.upper() for t in dev.tags}) or (k == "PLATFORM" and (pid or "") == v):
            return tag
    return None


def _pid(cat, dev: Device, pmap: dict) -> str | None:
    for name in dev.platform:
        if str(name) in pmap or str(name).lower() in pmap:
            return str(pmap.get(str(name)) or pmap.get(str(name).lower()))
    if cat is None:
        return str(dev.platform[0]).upper() if dev.platform else None
    for name in dev.platform:
        for cand in (name, str(name).replace("-", "_"), str(name).split("-")[-1], str(name).replace("-", "")):
            pid = cat.resolve(cand)
            if pid:
                return pid
    return None


@dataclass
class Plan:
    records: list[dict]                       # the new INVENTORY list
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)     # devices left out, with why (warnings)
    errors: list[str] = field(default_factory=list)       # block the commit

    def summary(self, where: str) -> list[str]:
        def few(xs):
            return ", ".join(xs[:15]) + (f" (+{len(xs) - 15} more)" if len(xs) > 15 else "")
        out = [f"from {where}: add {len(self.added)}, change {len(self.changed)}, "
               f"disable {len(self.disabled)} (gone from the source)"]
        out += [f"add: {few(self.added)}"] if self.added else []
        out += [f"change: {x}" for x in self.changed[:30]]
        out += [f"disable: {few(self.disabled)}"] if self.disabled else []
        return out


def plan(current: list[dict], devices: list[Device], src: dict, cat, cred_tags: set[str],
         defaults: dict | None = None) -> Plan:
    """What the inventory looks like after this import / sync. Pure: no I/O."""
    kind = str(src.get("TYPE") or "netbox").lower()
    rules = parse_cred_map(src.get("CRED_MAP"))
    for _, _, tag in rules:
        if tag not in cred_tags:
            raise SourceError(f"CRED_MAP: credential tag {tag} does not exist; create it first (credential_new)")
    pmap = {str(k): str(v).upper() for k, v in (src.get("PLATFORM_MAP") or {}).items()}
    defaults = defaults or {}
    out = [dict(r) for r in current if isinstance(r, dict)]
    by_sid = {str(r["SOURCE_ID"]): i for i, r in enumerate(out) if r.get("SOURCE_ID")}
    by_name = {str(r.get("DEVICE_NAME") or r.get("DEVICE_ID") or "").upper(): i for i, r in enumerate(out)}
    p = Plan(records=out)
    seen, names = set(), set()
    for dev in devices:
        seen.add(dev.sid)       # still in the source: a record is never disabled for a data problem here
        if not dev.name or not _NAME.match(dev.name):
            p.problems.append(f"{dev.sid} {dev.name!r}: not a usable device name; left out")
            continue
        if dev.name.upper() in names:
            p.problems.append(f"{dev.name}: the source has it twice; the second one is left out")
            continue
        names.add(dev.name.upper())
        if not dev.addr:
            p.problems.append(f"{dev.name}: no primary IP in {kind}; left out")
            continue
        pid = _pid(cat, dev, pmap)
        if not pid:
            p.problems.append(f"{dev.name}: platform {', '.join(dev.platform) or '(none)'} is not known; "
                              f"left out (add it to PLATFORM_MAP, e.g. {(dev.platform or ['x'])[0]}=EOS)")
            continue
        plat = cat.platforms.get(pid, {}) if cat else {}
        owned = {"DEVICE_NAME": dev.name, "DEVICE_ADDR": dev.addr, "OS": pid,
                 "VENDOR": str(plat.get("vendor") or dev.vendor or "UNKNOWN").upper(), "IS_ENABLED": dev.active}
        tags = {k: v for k, v in (("SITE", dev.site.upper()), ("ROLE", [dev.role.upper()] if dev.role else None)) if v}
        idx = by_sid.get(dev.sid)
        if idx is None:
            idx = by_name.get(dev.name.upper())
            if idx is not None and out[idx].get("SOURCE_ID") and out[idx]["SOURCE_ID"] != dev.sid:
                p.problems.append(f"{dev.name}: already in the inventory from {out[idx]['SOURCE_ID']}; left out")
                continue
        if idx is None:
            conn = (plat.get("conn") or ["SSH"])[0]
            cred = cred_for(rules, dev, pid)
            if not cred:
                p.errors.append(f"{dev.name}: no CRED_MAP rule matches (role {dev.role}, site {dev.site}); "
                                f"add one or a *=TAG default")
                continue
            rec = {"DEVICE_ID": dev.name, **owned, "CREDENTIAL": cred, "CONN_TYPE": conn,
                   "PORT": DEFAULT_PORTS.get(conn, 22),
                   "PRODUCTION": bool(defaults.get("PRODUCTION", True)),    # unanswered = production (safe)
                   "SOURCE_ID": dev.sid}
            t = {**({"FAB": defaults["FAB"]} if defaults.get("FAB") else {}), **tags}
            if t:
                rec["TAGS"] = t
            out.append(rec)
            p.added.append(dev.name)
            continue
        cur = out[idx]
        new = {**cur, **owned, "SOURCE_ID": dev.sid}
        if not new.get("CREDENTIAL"):
            new["CREDENTIAL"] = cred_for(rules, dev, pid)
        nt = {**(cur.get("TAGS") or {}), **tags}
        if nt:
            new["TAGS"] = nt
        if new != cur:
            diff = []
            for k in sorted(set(new) | set(cur)):
                if k == "TAGS":
                    for tk in sorted(set(nt) | set(cur.get("TAGS") or {})):
                        if nt.get(tk) != (cur.get("TAGS") or {}).get(tk):
                            diff.append(f"{tk} {(cur.get('TAGS') or {}).get(tk)} -> {nt.get(tk)}")
                elif new.get(k) != cur.get(k):
                    diff.append("adopted" if k == "SOURCE_ID" and not cur.get(k) else f"{k} {cur.get(k)} -> {new.get(k)}")
            p.changed.append(f"{dev.name} ({', '.join(diff)})")
            out[idx] = new
    prefix = f"{kind}:"
    for i, r in enumerate(out):
        sid = str(r.get("SOURCE_ID") or "")
        if sid.startswith(prefix) and sid not in seen and r.get("IS_ENABLED") is not False:
            out[i] ={**r, "IS_ENABLED": False}
            p.disabled.append(str(r.get("DEVICE_NAME") or r.get("DEVICE_ID")))
    addrs: dict[str, str] = {}
    for r in out:
        if r.get("IS_ENABLED") is False:
            continue
        a, n = str(r.get("DEVICE_ADDR") or ""), str(r.get("DEVICE_NAME") or "")
        if a and a in addrs and addrs[a] != n:
            p.errors.append(f"{n} and {addrs[a]} have the same address {a}")
        addrs[a] = n
    return p
