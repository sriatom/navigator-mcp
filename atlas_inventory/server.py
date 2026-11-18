"""ATLAS: layer 1 (inventory and credentials) as an MCP server.

Read-only tools for inventories (devices, controllers, out-of-band), the platform catalogue,
the read command list and credential metadata. No tool in this server ever returns a
credential value; RUDDER (layer 2) gets credentials server-to-server, never through a model.

Run:  atlas-mcp                              (stdio: Hermes, Claude, any MCP client)
      atlas-mcp --http 127.0.0.1:19310       (streamable HTTP: OpenClaw)
"""
from __future__ import annotations

import argparse

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from navigator_engineer import branding
from . import catalog as catalog_mod
from . import config, consumers, credentials, interview, inventory, netbox, store, vault

INSTRUCTIONS = """ATLAS: network inventory (devices, controllers, out-of-band), platform catalogue and
credential metadata. Layer 1 of ATLAS / RUDDER / NAVIGATOR.
- Inventories are named (e.g. EVPN_LAB). Use inventory_list first.
- inventory_query returns chosen fields as structured data; inventory_show is one line per record.
- platform_list / platform_show tell you which adapter (native, or a delegated MCP server) serves a
  platform; command_list gives the allowed read commands per platform.
- Credentials are referenced by TAG only. You can never see a password, and you must never
  ask the user to type one in chat.
- Changes go through the interview: interview_start -> ask the questions -> interview_answer ->
  interview_preview -> show it to the user -> on their yes, interview_commit with the code.
  netbox_export works the same way: dry run, user's yes, then the code.
- A device list in NetBox or Nautobot: interview topic source_import (once), then inventory_sync on
  demand; the preview lists what is added, changed and disabled before anything is written.
- inventory_render writes pyATS / Ansible / ANTA / multivendor files with credential references only.
- Report tool errors to the user as they are; do not guess device details."""

mcp = FastMCP("atlas", instructions=INSTRUCTIONS, icons=branding.icons(), website_url=branding.WEBSITE_URL)
MAX_LINES = 400
MAX_RECORDS = 500


def _cfg() -> config.Config:
    return config.load()


def _catalog(cfg: config.Config) -> catalog_mod.Catalog | None:
    try:
        return catalog_mod.Catalog(cfg.catalog_path)
    except catalog_mod.CatalogError:
        return None


def _need_catalog(cfg: config.Config) -> catalog_mod.Catalog:
    cat = _catalog(cfg)
    if cat is None:
        raise ToolError(f"platform catalogue not found in {cfg.catalog_path} "
                        f"(run scripts/gen_platforms.py)")
    return cat


def _clip(lines: list[str]) -> str:
    if len(lines) > MAX_LINES:
        return "\n".join(lines[:MAX_LINES] + [f"... {len(lines) - MAX_LINES} more lines; narrow the filter"])
    return "\n".join(lines)


def _names(cfg: config.Config) -> list[str]:
    """Inventory names this consumer may use."""
    return [n for n in inventory.list_names(cfg.data_path) if consumers.allowed_inventory(cfg, n)]


def _path(cfg: config.Config, name: str):
    try:
        path = inventory.resolve(cfg.data_path, name)
    except inventory.InventoryError as e:
        if "not found" in str(e):   # list only what this consumer may see
            raise ToolError(f"inventory {name!r} not found. Available: "
                            f"{', '.join(_names(cfg)) or 'none'}") from None
        raise ToolError(str(e)) from None
    if not consumers.allowed_inventory(cfg, path.stem):
        raise ToolError(f"inventory {name!r} is not available to consumer {cfg.consumer}")
    return path


def _load(name: str):
    """Devices only (kept for NAVI-era callers)."""
    cfg = _cfg()
    path = _path(cfg, name)
    try:
        return cfg, path, inventory.load(path)
    except inventory.InventoryError as e:
        raise ToolError(str(e)) from None


def _load_all(name: str):
    cfg = _cfg()
    path = _path(cfg, name)
    try:
        return cfg, path, inventory.load_all(path)
    except inventory.InventoryError as e:
        raise ToolError(str(e)) from None


def _shown(cfg: config.Config, dev: dict) -> dict:
    """Record as shown to this consumer: a credential tag it may not use is masked."""
    if dev.get("CREDENTIAL") and not consumers.allowed_tag(cfg, str(dev["CREDENTIAL"])):
        return {**dev, "CREDENTIAL": "(not allowed)"}
    return dev


def _record_types(record_type: str) -> list[str]:
    rt = (record_type or "DEVICE").upper()
    if rt == "ALL":
        return ["DEVICE", "CONTROLLER", "OOB"]
    if rt not in inventory.RECORD_LISTS:
        raise ToolError("record_type must be DEVICE, CONTROLLER, OOB or ALL")
    return [rt]


# --------------------------------------------------------------------------- inventory tools
@mcp.tool()
def inventory_list() -> str:
    """List inventories: devices (enabled/total), controllers, OOB records, credential tags used."""
    cfg = _cfg()
    names = _names(cfg)
    if not names:
        return "No inventories found."
    lines = []
    for n in names:
        try:
            recs = inventory.load_all(inventory.resolve(cfg.data_path, n))
        except Exception as e:  # noqa: BLE001
            lines.append(f"{n}: UNREADABLE ({type(e).__name__})")
            continue
        devs = recs["DEVICE"]
        everything = [r for v in recs.values() for r in v]
        enabled = sum(1 for d in devs if d.get("IS_ENABLED") is not False)
        creds = sorted({str(_shown(cfg, d).get("CREDENTIAL")) for d in everything if d.get("CREDENTIAL")})
        prod = sum(1 for d in everything if inventory.is_production(d))
        extra = "".join(f", {len(recs[k])} {label}" for k, label in
                        (("CONTROLLER", "controller(s)"), ("OOB", "OOB")) if recs[k])
        lines.append(f"{n}: {enabled}/{len(devs)} devices enabled{extra}, credentials "
                     f"{', '.join(creds) or '-'}" + (f", {prod} PRODUCTION" if prod else ""))
    return "\n".join(lines)


@mcp.tool()
def inventory_show(name: str, devices: list[str] | None = None, groups: list[str] | None = None,
                   filters: dict[str, str] | None = None, include_disabled: bool = False,
                   record_type: str = "ALL") -> str:
    """Show records of one inventory, one line each (name, address, vendor/OS or product,
    connection, credential tag, tags).

    devices: record names or IDs (case-insensitive). groups: any SITE/FAB/ROLE tag value
    (case-sensitive). filters: FIELD=value, e.g. {"OS": "PANOS"} or {"SITE": "HQ"}.
    record_type: DEVICE, CONTROLLER, OOB or ALL (default)."""
    cfg, _, recs = _load_all(name)
    pool = [r for rt in _record_types(record_type) for r in recs[rt]]
    sel = inventory.select(pool, names=devices, groups=groups, filters=filters,
                           include_disabled=include_disabled)
    if not sel:
        raise ToolError("no records matched. Try inventory_show with no filters to see what exists.")
    lines = [f"{len(sel)} record(s) in {name}:"] + [
        inventory.describe(_shown(cfg, d)) + ("  PRODUCTION" if inventory.is_production(d) else "")
        for d in sel]
    missing = sorted({n.upper() for n in devices or []} -
                     {str(d.get(k, "")).upper() for d in sel for k in ("DEVICE_NAME", "DEVICE_ID", "NAME")})
    if missing:
        lines.append(f"NOT FOUND: {', '.join(missing)}")
    return _clip(lines)


@mcp.tool()
def inventory_query(name: str, fields: list[str] | None = None, devices: list[str] | None = None,
                    groups: list[str] | None = None, filters: dict[str, str] | None = None,
                    include_disabled: bool = False, record_type: str = "DEVICE") -> list[dict]:
    """Records of one inventory as structured data, with only the fields you ask for.

    fields: e.g. ["DEVICE_NAME", "DEVICE_ADDR", "PLATFORM", "READS"]. Default: name, address,
    vendor, OS/product, credential tag. Computed fields: PLATFORM (catalogue id for the OS),
    READS / WRITES (the RUDDER adapter), PRODUCTION (true/false), RECORD_TYPE.
    Selection as in inventory_show. record_type: DEVICE (default), CONTROLLER, OOB or ALL."""
    cfg, _, recs = _load_all(name)
    cat = _catalog(cfg)
    pool = [r for rt in _record_types(record_type) for r in recs[rt]]
    sel = inventory.select(pool, names=devices, groups=groups, filters=filters,
                           include_disabled=include_disabled)
    if len(sel) > MAX_RECORDS:
        raise ToolError(f"{len(sel)} records match; narrow the selection (limit {MAX_RECORDS})")
    defaults = {"DEVICE": ["DEVICE_NAME", "DEVICE_ADDR", "VENDOR", "OS", "CREDENTIAL"],
                "CONTROLLER": ["NAME", "URL", "VENDOR", "PRODUCT", "CREDENTIAL"],
                "OOB": ["NAME", "ADDR", "VENDOR", "TYPE", "CREDENTIAL"]}
    out = []
    for r in sel:
        r = _shown(cfg, r)
        rt = r.get("RECORD_TYPE", "DEVICE")
        info = {}
        if cat is not None:
            if rt == "DEVICE":
                pid = cat.resolve(r.get("OS"))
                p = cat.platforms.get(pid, {}) if pid else {}
            elif rt == "CONTROLLER":
                pid, p = None, cat.controller(r.get("PRODUCT")) or {}
            else:
                pid, p = None, cat.oob_type(r.get("TYPE")) or {}
            info = {"PLATFORM": pid, "READS": p.get("reads"), "WRITES": p.get("writes")}
        computed = {**info, "PRODUCTION": inventory.is_production(r), "RECORD_TYPE": rt}
        want = [f.upper() for f in fields] if fields else defaults[rt]
        row = {}
        for f in want:
            if f in computed:
                row[f] = computed[f]
            elif f in inventory.TAGS_FIELDS:
                row[f] = (r.get("TAGS") or {}).get(f)
            else:
                row[f] = r.get(f)
        out.append(row)
    return out


@mcp.tool()
def inventory_validate(name: str, probe: bool = False) -> str:
    """Check an inventory: required fields, duplicate names/addresses, unknown credential tags,
    secret-looking fields, OS / controller product / OOB type against the platform catalogue.
    probe=True also tests TCP reachability of each enabled record."""
    cfg = _cfg()
    path = _path(cfg, name)
    rep = inventory.validate_text(path.read_text(), credentials.tags(cfg.credential_file),
                                  catalog=_catalog(cfg))
    out = rep.text(name)
    recs = [{**d, "RECORD_TYPE": rt} for rt, lst in
            (("DEVICE", rep.devices), ("CONTROLLER", rep.controllers), ("OOB", rep.oob)) for d in lst]
    if probe and recs:
        out += "\nReachability:\n" + "\n".join("  " + r for r in inventory.probe(
            [d for d in recs if d.get("IS_ENABLED") is not False]))
    return out


# --------------------------------------------------------------------------- catalogue tools
@mcp.tool()
def platform_list(vendor: str | None = None, search: str | None = None,
                  adapter: str | None = None) -> str:
    """List platforms in the catalogue: id, vendor, which RUDDER adapter serves reads and writes,
    connection types, and the name each tool uses (netmiko, NAPALM, pyATS, Ansible, multivendor).

    vendor: e.g. CISCO. search: text in id/alias/netmiko name. adapter: e.g. "pyats",
    "native", "multivendor", "generic-netmiko"."""
    cat = _need_catalog(_cfg())
    found = cat.search(vendor=vendor, text=search, adapter=adapter)
    if not found:
        raise ToolError("no platform matched. Try platform_list with no arguments.")
    head = f"{len(found)} platform(s) (catalogue from netmiko {cat.netmiko_version}):"
    return _clip([head] + [catalog_mod.describe_platform(p) for p in found])


@mcp.tool()
def platform_show(platform: str) -> dict:
    """Everything the catalogue knows about one platform (any name: EOS, arista_eos, CISCO_NXOS...),
    or one controller PRODUCT (VMANAGE, PANORAMA, ...) or OOB TYPE (CONSOLE_SERVER, BMC)."""
    cat = _need_catalog(_cfg())
    if cat.resolve(platform):
        p = cat.platform(platform)
        p["COMMAND_CATEGORIES"] = sorted(cat.commands_for(p["ID"]))
        return p
    other = cat.controller(platform) or cat.oob_type(platform)
    if other:
        return other
    raise ToolError(f"unknown platform {platform!r}. Use platform_list to see the names")


@mcp.tool()
def command_list(platform: str, category: str | None = None) -> str:
    """Allowed read (show) commands for a platform, by category. These are the commands RUDDER's
    device_read accepts. Without category: the categories and how many commands each has."""
    cat = _need_catalog(_cfg())
    try:
        pid = cat.platform(platform)["ID"]
        cmds = cat.commands_for(pid)
    except catalog_mod.CatalogError as e:
        raise ToolError(str(e)) from None
    if not cmds:
        have = ", ".join(sorted(cat.commands())) or "none"
        raise ToolError(f"no command list for {pid} yet (platforms with one: {have})")
    if not category:
        total = sum(len(v) for v in cmds.values())
        return "\n".join([f"{pid}: {total} commands in {len(cmds)} categories"] +
                         [f"  {c}: {len(v)}" for c, v in sorted(cmds.items())])
    match = next((c for c in cmds if c.lower() == category.lower()), None)
    if not match:
        raise ToolError(f"no category {category!r} for {pid}. Categories: {', '.join(sorted(cmds))}")
    return "\n".join([f"{pid} / {match}:"] + [f"  {c}" for c in cmds[match]])


# --------------------------------------------------------------------------- credentials
@mcp.tool()
def credential_list() -> str:
    """List credential tags: type, which fields are set (and which required ones are missing),
    whether they are stored encrypted, and which inventories use each tag. Values are never shown."""
    cfg = _cfg()
    used: dict[str, list[str]] = {}
    for n in _names(cfg):
        try:
            recs = inventory.load_all(inventory.resolve(cfg.data_path, n))
        except Exception:  # noqa: BLE001
            continue
        for d in (r for v in recs.values() for r in v):
            if d.get("CREDENTIAL"):
                used.setdefault(str(d["CREDENTIAL"]), [])
                if n not in used[str(d["CREDENTIAL"])]:
                    used[str(d["CREDENTIAL"])].append(n)
    allinfo = credentials.describe(cfg.credential_file)
    infos = [i for i in allinfo if consumers.allowed_tag(cfg, i.tag)]
    known = {i.tag for i in allinfo}
    lines = [f"{i.tag}: type {i.ctype}{' (inferred)' if i.inferred else ''}; "
             f"fields {', '.join(i.fields_set) or 'none'}"
             + (f"; MISSING {', '.join(i.missing)}" if i.missing else "")
             + f"; used by {', '.join(used.get(i.tag, [])) or 'no inventory'}; stored {i.storage}"
             + ("" if i.enabled else "; DISABLED") for i in infos]
    for tag in sorted(t for t in set(used) - known if consumers.allowed_tag(cfg, t)):
        lines.append(f"{tag}: MISSING (used by {', '.join(used[tag])}, not stored)")
    return "\n".join(lines) or "No credentials stored."


# --------------------------------------------------------------------------- interview (writes)
def _iv(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (interview.InterviewError, store.StoreError, inventory.InventoryError, vault.VaultError) as e:
        raise ToolError(str(e)) from None


@mcp.tool()
def interview_start(topic: str, inventory: str | None = None, record_type: str = "DEVICE",
                    name: str | None = None, tag: str | None = None) -> dict:
    """Start a guided change. Nothing is written until preview + the user's yes + commit.

    topic: inventory_new | record_add | record_edit | record_remove | bulk_import | bulk_edit |
           inventory_restore | credential_new | credential_update | credential_remove |
           source_import (devices from NetBox / Nautobot, once) | inventory_sync (that source again)
    inventory: the inventory (record_*, bulk_*, inventory_restore, source_import, inventory_sync). record_type: DEVICE,
    CONTROLLER or OOB. name: the record (record_edit/remove). tag: the credential (credential_update/remove).
    Returns the draft id and the next questions: ask the user, then call interview_answer.
    Secrets (passwords, keys) are NEVER asked in chat: the user sets them on the host with
    `atlas-interview secret <draft> <FIELD>`. Who-may-use-what changes are host-only."""
    cfg = _cfg()
    d = _iv(interview.start, cfg, topic, host=False, inventory_name=inventory, record_type=record_type,
            name=name, tag=tag)
    return _iv(interview.status, cfg, d["id"], host=False)


@mcp.tool()
def interview_answer(draft: str, answers: dict) -> dict:
    """Give answers to a draft, e.g. {"DEVICE_NAME": "LEAF1", "DEVICE_ADDR": "10.0.0.11", "OS": "EOS"}.
    Each answer is checked at once; problems are listed and the valid answers are kept.
    Never put a password or key here: it is refused."""
    cfg = _cfg()
    _, problems = _iv(interview.answer, cfg, draft, answers, host=False)
    return {"problems": problems, **_iv(interview.status, cfg, draft, host=False)}


@mcp.tool()
def interview_status(draft: str) -> dict:
    """The draft's answers so far, the next questions, and what it is waiting for."""
    return _iv(interview.status, _cfg(), draft, host=False)


@mcp.tool()
def interview_preview(draft: str) -> dict:
    """Show exactly what a commit would write (summary, diff, validation) and a commit code.
    Show it to the user and commit only on their yes."""
    return _iv(interview.preview, _cfg(), draft, host=False)


@mcp.tool()
def interview_commit(draft: str, code: str) -> str:
    """Write the previewed change. code = the commit_code from the last interview_preview.
    Every file written keeps its previous version. Call only after the user said yes."""
    return "\n".join(_iv(interview.commit, _cfg(), draft, code, host=False))


@mcp.tool()
def interview_cancel(draft: str) -> str:
    """Throw a draft away. Nothing is written."""
    _iv(interview.cancel, _cfg(), draft)
    return f"draft {draft} cancelled; nothing was written"


@mcp.tool()
def interview_list() -> str:
    """Open drafts (they expire after 7 days)."""
    ds = _iv(interview.list_drafts, _cfg())
    return "\n".join(f"{d['id']}  {d['topic']}  {d['target']}  by {d['who']}" for d in ds) or "No open drafts."


@mcp.tool()
def inventory_history(name: str) -> str:
    """Earlier versions of an inventory (newest first). Restore one with
    interview_start(topic="inventory_restore", inventory=name)."""
    cfg = _cfg()
    path = _path(cfg, name)
    vs = store.list_versions(cfg, path)
    return "\n".join([f"{path.stem}: {len(vs)} earlier version(s)"] + [f"  {p.stem}" for p in vs[:50]])


TEMPLATES = {"inventory": "INVENTORY_TEMPLATE.yml", "credentials": "CREDENTIALS_TEMPLATE.yml"}


@mcp.tool()
def template_get(kind: str = "inventory") -> str:
    """A ready-to-fill template with comments: kind = inventory (5 sample devices: spine, leaves, service
    leaves, with SITE / FAB / ROLE tags, plus a controller and an OOB example) or credentials (one entry
    per TYPE, placeholder values only). Give it to the user as a file to fill in. A filled inventory comes
    back through interview_start(topic="inventory_new") then topic="bulk_import". A filled credentials
    file is never pasted in chat: the user puts it on the host themselves."""
    from importlib.resources import files
    k = str(kind).strip().lower()
    if k not in TEMPLATES:
        raise ToolError(f"kind must be one of: {', '.join(TEMPLATES)}")
    return (files(__package__) / "templates" / TEMPLATES[k]).read_text()


@mcp.tool()
def inventory_render(name: str, format: str, devices: list[str] | None = None, groups: list[str] | None = None,
                     filters: dict[str, str] | None = None, save: bool = True) -> dict:
    """Render an inventory for another tool: format = pyats (testbed), ansible (YAML inventory), anta (one
    inventory per credential tag) or multivendor (the multivendor-cli MCP server). Passwords are never
    written: each file holds environment-variable references (see 'how'). Returns the file text and, with
    save, where it was written (mode 600). Devices a format cannot take are listed as skipped."""
    from . import render
    try:
        return render.render(_cfg(), name, format, devices, groups, filters, save)
    except (render.RenderError, inventory.InventoryError) as e:
        raise ToolError(str(e)) from None


# --------------------------------------------------------------------------- NetBox
@mcp.tool()
def netbox_export(name: str, code: str | None = None) -> dict:
    """Push one inventory's enabled devices to NetBox (sites, roles, platforms, devices, mgmt IPs).

    Without code: a dry run that lists what would be created or updated, plus a commit code.
    With code: applies exactly that plan. Call with the code only after the user said yes.
    Creates missing objects; updates only devices ATLAS made (tag 'atlas'); never deletes."""
    cfg = _cfg()
    _path(cfg, name)                          # consumer check
    try:
        return netbox.export(cfg, name, code, who=cfg.consumer)
    except (netbox.NetBoxError, vault.VaultError, PermissionError, inventory.InventoryError) as e:
        raise ToolError(str(e)) from None
    except httpx.HTTPError as e:
        raise ToolError(f"cannot reach NetBox: {type(e).__name__}") from None


def main() -> None:
    ap = argparse.ArgumentParser(prog="atlas-mcp")
    ap.add_argument("--serve", action="store_true",
                    help="HTTPS with logins on navigator.yml HTTP.BIND and HTTP.PORTS.ATLAS (navigator_gate)")
    ap.add_argument("--http", help="host:port: HTTPS with logins on this address instead")
    ap.add_argument("--no-tls", action="store_true", help="plain HTTP with logins, 127.0.0.1 only (tests)")
    a = ap.parse_args()
    if a.serve or a.http:
        from navigator_gate import gate
        try:
            gate.serve(mcp, "ATLAS", a.http, a.no_tls)
        except gate.GateError as e:
            raise SystemExit(f"atlas-mcp: {e}") from None
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
