"""ATLAS inventories: load, select and validate.

An inventory is DATA_PATH/INVENTORY_<NAME>.yml. It holds up to three record lists:
    INVENTORY:    devices       (switches, routers, firewalls, hosts). Same rules as NAVI's
                                DATA_COLLECTOR.py, so existing files stay valid.
    CONTROLLERS:  controllers   (vManage, Meraki, Panorama, FortiManager, APIC, ...)
    OOB:          out-of-band   (console servers, BMCs)
Inventories hold names, addresses and tags plus a CREDENTIAL tag; never secrets.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

RECORD_LISTS = {"DEVICE": "INVENTORY", "CONTROLLER": "CONTROLLERS", "OOB": "OOB"}
MANDATORY = {
    "DEVICE": ("DEVICE_ID", "DEVICE_NAME", "DEVICE_ADDR", "CREDENTIAL", "VENDOR",
               "OS", "CONN_TYPE", "PORT", "IS_ENABLED"),
    "CONTROLLER": ("NAME", "URL", "VENDOR", "PRODUCT", "CREDENTIAL", "IS_ENABLED"),
    "OOB": ("NAME", "ADDR", "TYPE", "VENDOR", "CREDENTIAL", "IS_ENABLED"),
}
MANDATORY_DEVICE_KEYS = MANDATORY["DEVICE"]
NAME_KEYS = {"DEVICE": ("DEVICE_NAME", "DEVICE_ID"), "CONTROLLER": ("NAME",), "OOB": ("NAME",)}
ADDR_KEY = {"DEVICE": "DEVICE_ADDR", "CONTROLLER": "URL", "OOB": "ADDR"}
CASE_INSENSITIVE_FIELDS = {"DEVICE_NAME", "DEVICE_ID", "NAME", "OS", "VENDOR", "CONN_TYPE",
                           "PRODUCT", "TYPE", "PLATFORM"}
TAGS_FIELDS = {"SITE", "FAB", "ROLE"}
# Optional per-record connection method for RUDDER instead of the platform default (same names as RUDDER's via=).
ADAPTER_CHOICES = ("eapi", "ssh", "netmiko", "pyats", "multivendor", "fortinet", "vmanage")
# Used only when no platform catalogue is available.
KNOWN_OS = {"EOS", "ARISTAEOS", "IOS", "IOSXE", "NXOS", "PANOS", "VIPTELA", "SDWAN", "LINUX"}
KNOWN_CONN = {"SSH", "EAPI", "HTTPS", "API", "NETCONF", "RESTCONF", "NXAPI", "GNMI", "XML_API",
              "VMANAGE_API", "TELNET", "REDFISH"}

# Any key that looks like it holds a secret makes the whole file invalid.
SECRET_KEY = re.compile(r"PASS|SECRET|TOKEN|PRIVATE|API_?KEY|PSK|COMMUNITY|ENABLE_?PW|^ENABLE$", re.I)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")


class InventoryError(Exception):
    pass


# --------------------------------------------------------------------------- names and files
def resolve(data_path: Path, name: str) -> Path:
    """Inventory NAME -> file in data_path. Names only, never paths."""
    name = re.sub(r"\.ya?ml$", "", (name or "").strip())
    if not _NAME.match(name) or ".." in name:
        raise InventoryError(f"invalid inventory name {name!r}")
    for cand in (name, "INVENTORY_" + name):
        if not re.match(r"^INVENTORY[_-].+", cand):   # never CREDENTIALS.yml or other vault files
            continue
        for ext in (".yml", ".yaml"):
            p = data_path / (cand + ext)
            if p.is_file():
                return p
    raise InventoryError(f"inventory {name!r} not found. Available: "
                         f"{', '.join(list_names(data_path)) or 'none'}")


def list_names(data_path: Path) -> list[str]:
    try:
        return sorted(re.sub(r"\.ya?ml$", "", p.name) for p in data_path.iterdir()
                      if re.match(r"^INVENTORY[_-].+\.ya?ml$", p.name) and p.is_file())
    except OSError:
        return []


def _raw(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise InventoryError(f"{path.name}: top level must be a mapping")
    return raw


def load_all(path: Path) -> dict[str, list[dict]]:
    """{'DEVICE': [...], 'CONTROLLER': [...], 'OOB': [...]}. Each record gets RECORD_TYPE."""
    raw = _raw(path)
    out: dict[str, list[dict]] = {}
    for rtype, key in RECORD_LISTS.items():
        items = raw.get(key) or []
        if not isinstance(items, list):
            raise InventoryError(f"{path.name}: {key}: must be a list")
        out[rtype] = [{**d, "RECORD_TYPE": rtype} for d in items if isinstance(d, dict)]
    if not any(out.values()) and not raw.get("META"):
        raise InventoryError(f"{path.name}: no INVENTORY:, CONTROLLERS: or OOB: list")
    return out


def load(path: Path) -> list[dict]:
    """Devices only (the INVENTORY: list), as NAVI's scripts expect. May be empty when the file
    holds only controllers or OOB records."""
    raw = _raw(path)
    devices = raw.get("INVENTORY")
    if not devices and (raw.get("CONTROLLERS") or raw.get("OOB") or raw.get("META")):
        return []
    if not isinstance(devices, list):
        raise InventoryError(f"{path.name}: no INVENTORY: list")
    return [d for d in devices if isinstance(d, dict)]


def record_name(rec: dict) -> str:
    rtype = rec.get("RECORD_TYPE", "DEVICE")
    for k in NAME_KEYS.get(rtype, ("DEVICE_NAME", "NAME")):
        if rec.get(k):
            return str(rec[k])
    return "?"


# --------------------------------------------------------------------------- selection
def _values(dev: dict, fld: str) -> list[str]:
    fld = fld.upper()
    val = (dev.get("TAGS") or {}).get(fld) if fld in TAGS_FIELDS else dev.get(fld)
    if val is None:
        return []
    return [str(v) for v in val] if isinstance(val, (list, tuple)) else [str(val)]


def _groups(dev: dict) -> set[str]:
    return {v for f in TAGS_FIELDS for v in _values(dev, f)}


def select(devices: list[dict], names: list[str] | None = None, groups: list[str] | None = None,
           filters: dict[str, str] | None = None, include_disabled: bool = False) -> list[dict]:
    """Names: case-insensitive on DEVICE_NAME/DEVICE_ID/NAME. Groups: any TAGS value, case-sensitive.
    Filters: FIELD=value; case-insensitive only for the CASE_INSENSITIVE_FIELDS."""
    wanted = {n.upper() for n in names or []}
    out = []
    for d in devices:
        if not include_disabled and d.get("IS_ENABLED") is False:
            continue
        if wanted and not ({str(d.get(k, "")).upper() for k in ("DEVICE_NAME", "DEVICE_ID", "NAME")}
                           & wanted):
            continue
        if groups and not (_groups(d) & set(groups)):
            continue
        ok = True
        for f, want in (filters or {}).items():
            vals = _values(d, f)
            if f.upper() in CASE_INSENSITIVE_FIELDS:
                ok = str(want).upper() in {v.upper() for v in vals}
            else:
                ok = str(want) in vals
            if not ok:
                break
        if ok:
            out.append(d)
    return out


def _tag_text(rec: dict) -> str:
    tags = rec.get("TAGS") or {}
    if not isinstance(tags, dict):
        return ""
    return " ".join(f"{k}={','.join(map(str, v)) if isinstance(v, list) else v}" for k, v in tags.items())


def describe(dev: dict) -> str:
    """One line per record. Only safe fields (no secret can be in a valid inventory anyway)."""
    rtype = dev.get("RECORD_TYPE", "DEVICE")
    state = "" if dev.get("IS_ENABLED") is not False else " DISABLED"
    tag_txt = _tag_text(dev)
    tail = f"  cred={dev.get('CREDENTIAL')}{state}{'  ' + tag_txt if tag_txt else ''}"
    if rtype == "CONTROLLER":
        return f"[controller] {dev.get('NAME')}  {dev.get('URL')}  {dev.get('VENDOR')}/{dev.get('PRODUCT')}" + tail
    if rtype == "OOB":
        port = f":{dev.get('PORT')}" if dev.get("PORT") else ""
        return f"[oob] {dev.get('NAME')}  {dev.get('ADDR')}{port}  {dev.get('VENDOR')}/{dev.get('TYPE')}" + tail
    return (f"{dev.get('DEVICE_NAME')}  {dev.get('DEVICE_ADDR')}  {dev.get('VENDOR')}/{dev.get('OS')}"
            f"  {dev.get('CONN_TYPE')}:{dev.get('PORT')}" + tail)


# --------------------------------------------------------------------------- validation
@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    controllers: list[dict] = field(default_factory=list)
    oob: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self, title: str) -> str:
        counts = f"{len(self.devices)} device(s)"
        if self.controllers:
            counts += f", {len(self.controllers)} controller(s)"
        if self.oob:
            counts += f", {len(self.oob)} OOB"
        head = f"{title}: {'VALID' if self.ok else 'INVALID'}, {counts}, " \
               f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        lines = [head] + [f"  ERROR   {e}" for e in self.errors] + [f"  WARNING {w}" for w in self.warnings]
        return "\n".join(lines)


def _secret_keys(text: str) -> list[str]:
    """Every mapping key that looks like a secret, with its line number (values never shown)."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and SECRET_KEY.search(str(k.value)):
                    found.append(f"line {k.start_mark.line + 1}: key '{k.value}'")
                walk(v)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    for doc in yaml.compose_all(text):
        walk(doc)
    return found


def _check_addr(rep: Report, label: str, addr: str) -> None:
    try:
        ipaddress.ip_address(addr)
    except ValueError:
        if not _HOST.match(addr):
            rep.errors.append(f"{label}: address {addr!r} is not an IP address or host name")


def _check_common(rep: Report, label: str, d: dict, rtype: str, credential_tags) -> None:
    for k in MANDATORY[rtype]:
        if k not in d:
            rep.errors.append(f"{label}: missing {k}")
        elif d[k] is None or (isinstance(d[k], str) and not d[k].strip()):
            rep.errors.append(f"{label}: {k} is blank")
    port = d.get("PORT")
    if port is not None and not (isinstance(port, int) and 1 <= port <= 65535):
        rep.errors.append(f"{label}: PORT must be a number 1-65535")
    if "IS_ENABLED" in d and not isinstance(d["IS_ENABLED"], bool):
        rep.errors.append(f"{label}: IS_ENABLED must be true or false")
    tags = d.get("TAGS")
    if tags is not None and not isinstance(tags, dict):
        rep.errors.append(f"{label}: TAGS must be a mapping (SITE, FAB, ROLE)")
    elif tags:
        extra = set(map(str.upper, tags)) - TAGS_FIELDS - {"ENV", "PRODUCTION"}
        if extra:
            rep.warnings.append(f"{label}: TAGS keys {sorted(extra)} are not used for selection")
    cred = d.get("CREDENTIAL")
    if credential_tags is not None and cred and str(cred) not in credential_tags:
        rep.errors.append(f"{label}: credential tag {cred} does not exist "
                          f"(create it with credential_request)")
    conn = str(d.get("CONN_TYPE", "")).upper()
    if conn and conn not in KNOWN_CONN:
        rep.warnings.append(f"{label}: CONN_TYPE {d.get('CONN_TYPE')!r} is unusual")
    ad = d.get("ADAPTER")
    if ad not in (None, "") and str(ad).strip().lower() not in ADAPTER_CHOICES:
        rep.errors.append(f"{label}: ADAPTER {ad!r} is not one of {', '.join(ADAPTER_CHOICES)}")


def validate_text(text: str, credential_tags: set[str] | None = None, catalog=None) -> Report:
    """catalog: an atlas_inventory.catalog.Catalog; when given, OS / PRODUCT / TYPE are checked
    against it, otherwise against the short built-in list."""
    rep = Report()
    try:
        secrets = _secret_keys(text)
    except yaml.YAMLError as e:
        rep.errors.append(f"not valid YAML: {str(e).splitlines()[0]}")
        return rep
    if secrets:
        rep.errors += [f"{s} looks like a secret; inventories may hold only a CREDENTIAL tag. "
                       f"Store passwords with credential_request." for s in secrets]
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        rep.errors.append("top level must be a mapping with INVENTORY:, CONTROLLERS: or OOB:")
        return rep
    lists = {rt: raw.get(key) for rt, key in RECORD_LISTS.items()}
    if not any(isinstance(v, list) and v for v in lists.values()):
        if isinstance(raw.get("META"), dict):          # made by the interview, records still to come
            rep.warnings.append("no records yet (add them with record_add or bulk_import)")
        else:
            rep.errors.append("needs a non-empty top-level INVENTORY: list (or CONTROLLERS: / OOB:)")
        return rep
    for rt, key in RECORD_LISTS.items():
        if lists[rt] is not None and not isinstance(lists[rt], list):
            rep.errors.append(f"{key}: must be a list")
            lists[rt] = None

    names: dict[str, str] = {}             # every record name in the file, any type
    seen: dict[str, dict[str, str]] = {"DEVICE_ID": {}, "ADDR": {}}

    def unique(label: str, kind: str, value: str, table: dict[str, str]) -> None:
        key = value.upper() if kind not in ADDR_KEY.values() else value
        if key in table:
            rep.errors.append(f"{label}: duplicate {kind} {value} (also {table[key]})")
        else:
            table[key] = label

    for rt in ("DEVICE", "CONTROLLER", "OOB"):
        for i, d in enumerate(lists[rt] or [], 1):
            if not isinstance(d, dict):
                rep.errors.append(f"{RECORD_LISTS[rt]} entry #{i} is not a mapping")
                continue
            {"DEVICE": rep.devices, "CONTROLLER": rep.controllers, "OOB": rep.oob}[rt].append(d)
            name_key = NAME_KEYS[rt][0]
            label = str(d.get(name_key) or d.get("DEVICE_ID") or f"{RECORD_LISTS[rt]} entry #{i}")
            _check_common(rep, label, d, rt, credential_tags)
            if d.get(name_key):
                nm = str(d[name_key]).strip()
                if not _NAME.match(nm):
                    rep.errors.append(f"{label}: {name_key} may use only letters, digits, . _ -")
                unique(label, name_key, nm, names)
            if rt == "DEVICE" and str(d.get("DEVICE_ID", "")).strip():
                unique(label, "DEVICE_ID", str(d["DEVICE_ID"]).strip(), seen["DEVICE_ID"])

            addr = str(d.get(ADDR_KEY[rt], "")).strip()
            if addr and rt == "CONTROLLER":
                u = urlparse(addr)
                if u.scheme not in ("http", "https") or not u.hostname:
                    rep.errors.append(f"{label}: URL must look like https://host[:port][/path]")
                elif u.username or u.password:
                    rep.errors.append(f"{label}: URL must not contain a user name or password")
                elif u.scheme == "http":
                    rep.warnings.append(f"{label}: URL uses plain http")
            elif addr:
                _check_addr(rep, label, addr)
                unique(label, ADDR_KEY[rt], addr, seen["ADDR"])

            if rt == "DEVICE":
                os_val = d.get("OS")
                if os_val and catalog is not None:
                    pid = catalog.resolve(os_val)
                    if not pid:
                        rep.warnings.append(f"{label}: OS {os_val!r} is not in the platform catalogue "
                                            f"(platform_list shows the known names)")
                    else:
                        conns = catalog.platforms[pid].get("conn") or []
                        conn = str(d.get("CONN_TYPE", "")).upper()
                        if conn and conns and conn not in conns and not (conn == "HTTPS" and "EAPI" in conns):
                            rep.warnings.append(f"{label}: CONN_TYPE {conn} is not listed for {pid} "
                                                f"({', '.join(conns)})")
                elif os_val:
                    if re.sub(r"[-_ ]", "", str(os_val)).upper() not in KNOWN_OS:
                        rep.warnings.append(f"{label}: OS {os_val!r} is not one NAVI's collectors know")
            elif rt == "CONTROLLER" and d.get("PRODUCT") and catalog is not None:
                if not catalog.controller(d["PRODUCT"]):
                    rep.warnings.append(f"{label}: PRODUCT {d['PRODUCT']!r} is not a known controller "
                                        f"({', '.join(catalog.controllers)})")
            elif rt == "OOB" and d.get("TYPE") and catalog is not None:
                if not catalog.oob_type(d["TYPE"]):
                    rep.errors.append(f"{label}: TYPE must be one of {', '.join(catalog.oob_types)}")

            if rt in ("CONTROLLER", "OOB"):
                links = d.get("MANAGES") if rt == "CONTROLLER" else d.get("PORTS")
                if links is not None and not isinstance(links, (list, dict)):
                    rep.errors.append(f"{label}: {'MANAGES' if rt == 'CONTROLLER' else 'PORTS'} "
                                      f"must be a list (or a mapping of port -> device)")

    # links to devices must name devices in this file
    dev_names = {str(d.get(k, "")).upper() for d in rep.devices for k in ("DEVICE_NAME", "DEVICE_ID")}
    for rec, key in [(c, "MANAGES") for c in rep.controllers] + [(o, "PORTS") for o in rep.oob]:
        links = rec.get(key)
        targets = list(links.values()) if isinstance(links, dict) else list(links or []) \
            if isinstance(links, list) else []
        for t in targets:
            if str(t).upper() not in dev_names:
                rep.warnings.append(f"{rec.get('NAME')}: {key} names {t}, which is not a device in this file")
    return rep


def probe(devices: list[dict], timeout: float = 2.0) -> list[str]:
    """TCP connect to each record's address:port. Returns 'NAME addr:port open|closed'."""
    def one(d):
        rtype = d.get("RECORD_TYPE", "DEVICE")
        if rtype == "CONTROLLER":
            u = urlparse(str(d.get("URL")))
            addr, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        else:
            addr = str(d.get(ADDR_KEY.get(rtype, "DEVICE_ADDR")))
            port = int(d.get("PORT") or (443 if str(d.get("CONN_TYPE", "")).upper() in
                                         ("EAPI", "HTTPS", "API", "XML_API", "REDFISH") else 22))
        try:
            with socket.create_connection((addr, port), timeout=timeout):
                return f"{record_name(d)} {addr}:{port} open"
        except OSError as e:
            return f"{record_name(d)} {addr}:{port} NOT reachable ({type(e).__name__})"
    with ThreadPoolExecutor(max_workers=32) as ex:
        return list(ex.map(one, devices))


def is_production(dev: dict) -> bool:
    """True if PRODUCTION: true, or any TAGS value (SITE, FAB, ROLE, ENV, ...) is PRODUCTION."""
    if dev.get("PRODUCTION") is True:
        return True
    tags = dev.get("TAGS") or {}
    if not isinstance(tags, dict):
        return False
    vals = set()
    for v in tags.values():
        vals |= {str(x).upper() for x in (v if isinstance(v, (list, tuple)) else [v])}
    return "PRODUCTION" in vals
