"""Export an ATLAS inventory to NetBox (decision #54: ATLAS first, then push to NetBox).

- Dry run first: every object is looked up and reported as create / update / ok / skip.
  The dry run prints a code; the commit needs that code and re-checks that nothing changed.
- Creates what is missing: tag `atlas`, site, manufacturer, device type, role, platform, device,
  a `mgmt0` interface, the management IP, and sets it as the device's primary IP.
- Updates only devices that carry the `atlas` tag (ATLAS made them). Other objects are never
  changed. Nothing is ever deleted.
- Exports enabled DEVICE records. Controllers and OOB records are reported, not exported (v1).
- The NetBox token is an ATLAS credential (TYPE API_TOKEN), read server-side; never returned.

bootstrap.yml:
    NETBOX:
      URL: https://netbox.example.net
      TOKEN_CREDENTIAL: NETBOX_API        # an API_TOKEN credential tag
      VERIFY_TLS: true
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field

import httpx

from . import catalog as catalog_mod
from . import config, inventory, store, vault

TAG_SLUG = "atlas"


class NetBoxError(Exception):
    pass


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:100] or "unnamed"


class Client:
    def __init__(self, url: str, token: str, verify: bool = True, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=url.rstrip("/") + "/api/", timeout=20, verify=verify,
                                 transport=transport,
                                 headers={"Authorization": f"Token {token}", "Accept": "application/json",
                                          "Content-Type": "application/json"})

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise NetBoxError(f"NetBox {r.request.method} {r.request.url.path}: HTTP {r.status_code} "
                              f"{r.text[:300]}")
        return r.json() if r.content else {}

    def find(self, ep: str, **params) -> dict | None:
        res = self._check(self.http.get(ep, params=params)).get("results") or []
        if len(res) > 1:
            raise NetBoxError(f"{ep} {params}: {len(res)} matches; expected one")
        return res[0] if res else None

    def create(self, ep: str, body: dict) -> dict:
        return self._check(self.http.post(ep, json=body))

    def update(self, ep: str, oid: int, body: dict) -> dict:
        return self._check(self.http.patch(f"{ep}{oid}/", json=body))


@dataclass
class Plan:
    actions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, action: str, kind: str, key: str, detail: str = "") -> None:
        self.actions.append({"action": action, "kind": kind, "key": key, "detail": detail})

    @property
    def code(self) -> str:
        return hashlib.sha256(json.dumps(self.actions, sort_keys=True).encode()).hexdigest()[:8].upper()

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a["action"]] = out.get(a["action"], 0) + 1
        return out


def _slug_of(obj) -> str | None:
    return obj.get("slug") if isinstance(obj, dict) else None


def _ensure(nb: Client, plan: Plan, apply: bool, ep: str, kind: str, slug: str, body: dict,
            cache: dict) -> int | None:
    """Shared objects (site, manufacturer, ...): create if missing, never update."""
    k = (ep, slug)
    if k in cache:
        return cache[k]
    found = nb.find(ep, slug=slug)
    if found:
        cache[k] = found["id"]
        return found["id"]
    plan.add("create", kind, slug)
    cache[k] = nb.create(ep, {**body, "slug": slug})["id"] if apply else None
    return cache[k]


def sync(nb: Client, records: list[dict], cat: catalog_mod.Catalog | None, apply: bool) -> Plan:
    plan, cache = Plan(), {}
    tag_id = _ensure(nb, plan, apply, "extras/tags/", "tag", TAG_SLUG,
                     {"name": TAG_SLUG, "color": "2196f3", "description": "managed by ATLAS"}, cache)
    for rec in records:
        rt = rec.get("RECORD_TYPE", "DEVICE")
        name = inventory.record_name(rec)
        if rt != "DEVICE":
            plan.notes.append(f"{name}: {rt.lower()} records are not exported yet")
            continue
        if rec.get("IS_ENABLED") is False:
            plan.notes.append(f"{name}: disabled, not exported")
            continue
        tags = rec.get("TAGS") or {}
        site = str(tags.get("SITE") or "atlas-unassigned")
        role = tags.get("ROLE")
        role = str(role[0] if isinstance(role, list) and role else role or "network")
        pid = (cat.resolve(rec.get("OS")) if cat else None) or str(rec.get("OS") or "unknown").upper()
        vendor = str(rec.get("VENDOR") or "unknown")
        model = str(rec.get("MODEL") or f"{pid} (generic)")

        site_id = _ensure(nb, plan, apply, "dcim/sites/", "site", slugify(site), {"name": site, "status": "active"}, cache)
        man_id = _ensure(nb, plan, apply, "dcim/manufacturers/", "manufacturer", slugify(vendor), {"name": vendor}, cache)
        dt_slug = slugify(f"{vendor}-{model}")
        dt_id = _ensure(nb, plan, apply, "dcim/device-types/", "device type", dt_slug,
                        {"model": model, "manufacturer": man_id}, cache)
        role_id = _ensure(nb, plan, apply, "dcim/device-roles/", "role", slugify(role),
                          {"name": role, "color": "9e9e9e"}, cache)
        napalm = (cat.platforms.get(pid, {}).get("napalm") if cat else None) or ""
        plat_id = _ensure(nb, plan, apply, "dcim/platforms/", "platform", slugify(pid),
                          {"name": pid, "manufacturer": man_id, "description": f"napalm: {napalm}" if napalm else ""},
                          cache)

        want = {"site": slugify(site), "role": slugify(role), "device_type": dt_slug, "platform": slugify(pid)}
        body = {"name": name, "site": site_id, "role": role_id, "device_type": dt_id, "platform": plat_id,
                "status": "active", "tags": [tag_id] if tag_id else []}
        dev = nb.find("dcim/devices/", name=name)
        if dev is None:
            plan.add("create", "device", name, f"site {site}, role {role}, platform {pid}")
            dev_id = nb.create("dcim/devices/", body)["id"] if apply else None
        else:
            dev_id = dev["id"]
            managed = TAG_SLUG in {_slug_of(t) for t in dev.get("tags") or []}
            have = {"site": _slug_of(dev.get("site")), "role": _slug_of(dev.get("role") or dev.get("device_role")),
                    "device_type": _slug_of(dev.get("device_type")), "platform": _slug_of(dev.get("platform"))}
            diff = sorted(k for k in want if have.get(k) != want[k])
            if not managed:
                plan.add("skip", "device", name, "exists in NetBox but was not made by ATLAS (no 'atlas' tag)")
                continue
            if diff:
                plan.add("update", "device", name, ", ".join(f"{k} {have[k]} -> {want[k]}" for k in diff))
                if apply:
                    nb.update("dcim/devices/", dev_id, {k: body[k] for k in diff})
            else:
                plan.add("ok", "device", name)

        addr = str(rec.get("DEVICE_ADDR") or "")
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            plan.notes.append(f"{name}: address {addr} is a name, not an IP; no IP object made")
            continue
        cidr = f"{ip}/{32 if ip.version == 4 else 128}"
        intf = nb.find("dcim/interfaces/", device_id=dev_id, name="mgmt0") if dev_id else None
        if intf is None:
            plan.add("create", "interface", f"{name}/mgmt0")
            intf_id = nb.create("dcim/interfaces/", {"device": dev_id, "name": "mgmt0", "type": "virtual",
                                                     "mgmt_only": True})["id"] if apply else None
        else:
            intf_id = intf["id"]
        ipo = nb.find("ipam/ip-addresses/", address=cidr)
        if ipo is None:
            plan.add("create", "ip address", cidr, f"on {name}/mgmt0")
            ip_id = nb.create("ipam/ip-addresses/", {"address": cidr, "assigned_object_type": "dcim.interface",
                                                     "assigned_object_id": intf_id, "status": "active",
                                                     "tags": [tag_id] if tag_id else []})["id"] if apply else None
        else:
            ip_id = ipo["id"]
            if ipo.get("assigned_object_id") not in (None, intf_id):
                plan.add("skip", "ip address", cidr, "already assigned to another interface in NetBox")
                continue
            if ipo.get("assigned_object_id") is None:
                plan.add("update", "ip address", cidr, f"assign to {name}/mgmt0")
                if apply:
                    nb.update("ipam/ip-addresses/", ip_id, {"assigned_object_type": "dcim.interface",
                                                           "assigned_object_id": intf_id})
        prim = (dev or {}).get("primary_ip4" if ip.version == 4 else "primary_ip6")
        if not prim or prim.get("id") != ip_id or ip_id is None:
            plan.add("update", "primary ip", name, cidr)
            if apply:
                nb.update("dcim/devices/", dev_id, {"primary_ip4" if ip.version == 4 else "primary_ip6": ip_id})
    return plan


def _client(cfg: config.Config, transport=None) -> Client:
    nbc = cfg.netbox
    if not nbc:
        raise NetBoxError("NetBox is not configured: add NETBOX: {URL, TOKEN_CREDENTIAL} to bootstrap.yml")
    tag = nbc.get("TOKEN_CREDENTIAL")
    if not tag:
        raise NetBoxError("NETBOX.TOKEN_CREDENTIAL is not set (an API_TOKEN credential tag)")
    cred = vault.get_credential(cfg, str(tag))
    token = cred.get("API_KEY") or cred.get("TOKEN")
    if not token:
        raise NetBoxError(f"credential {tag} has no API_KEY or TOKEN")
    try:
        return Client(nbc["URL"], token, verify=bool(nbc.get("VERIFY_TLS", True)), transport=transport)
    finally:
        del token, cred


def export(cfg: config.Config, name: str, code: str | None = None, transport=None,
           who: str | None = None) -> dict:
    path = inventory.resolve(cfg.data_path, name)
    recs = [r for v in inventory.load_all(path).values() for r in v]
    try:
        cat = catalog_mod.Catalog(cfg.catalog_path)
    except catalog_mod.CatalogError:
        cat = None
    nb = _client(cfg, transport)
    plan = sync(nb, recs, cat, apply=False)
    base = {"inventory": path.stem, "netbox": cfg.netbox["URL"], "counts": plan.counts(),
            "changes": [f"{a['action']:6} {a['kind']} {a['key']}" + (f" ({a['detail']})" if a["detail"] else "")
                        for a in plan.actions if a["action"] != "ok"],
            "notes": plan.notes}
    pending = [a for a in plan.actions if a["action"] in ("create", "update")]
    if code is None:
        return {**base, "dry_run": True, "commit_code": plan.code if pending else None,
                "next": (f"Show this to the user. On their yes: netbox_export(name='{name}', code='{plan.code}')"
                         if pending else "NetBox already matches; nothing to do.")}
    if str(code).strip().upper() != plan.code:
        raise NetBoxError("the code does not match: NetBox or the inventory changed since the dry run. "
                          "Run the dry run again.")
    done = sync(nb, recs, cat, apply=True)
    store.audit(cfg, who or cfg.consumer, "netbox_export",
                f"{path.stem} -> {cfg.netbox['URL']}: {done.counts()}")
    return {**base, "dry_run": False, "applied": done.counts()}
