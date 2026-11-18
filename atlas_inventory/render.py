"""Render an ATLAS inventory for other tools: pyATS testbed, Ansible, ANTA, multivendor-cli.

SPEC_RENDERING: no format gets a password. Each credential is a *reference* the tool resolves when it runs:
  pyATS        %ENV{NAVI_CRED_<TAG>_USERNAME} / _PASSWORD / _ENABLE in the testbed
  Ansible      {{ lookup('env', 'NAVI_CRED_<TAG>_PASSWORD') }} in group vars, one group per credential tag
  ANTA         no credentials in the file at all; one file per credential tag, run with ANTA_USERNAME / ANTA_PASSWORD
  multivendor  credential_ref: <TAG>; the server reads MULTIVENDOR_<TAG>_USERNAME / _PASSWORD
Only records and tags this consumer may use are rendered. Devices a format cannot take are listed as skipped.
Files go to <STATE_PATH>/rendered/<consumer>/ (mode 600). A re-render overwrites only files it wrote itself.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import yaml

from . import catalog as catalog_mod
from . import config, consumers, inventory

FORMATS = ("pyats", "ansible", "anta", "multivendor")
HEADER = "# Rendered by ATLAS from {inv} at {when} for {consumer}. Do not edit: re-render instead.\n" \
         "# No passwords in this file: {how}\n"
HOW = {
    "pyats": "set NAVI_CRED_<TAG>_USERNAME / _PASSWORD / _ENABLE in the environment (pyATS reads %ENV{...}).",
    "ansible": "set NAVI_CRED_<TAG>_USERNAME / _PASSWORD in the environment (lookup('env', ...)).",
    "anta": "one file per credential tag; run anta with ANTA_USERNAME / ANTA_PASSWORD for that tag.",
    "multivendor": "credential_ref is the tag; the server reads MULTIVENDOR_<TAG>_USERNAME / _PASSWORD.",
}
MARK = "# Rendered by ATLAS from "
# platforms the multivendor server hands to other servers: leave them out
MULTIVENDOR_SKIP = {"IOSXE", "IOS", "NXOS", "IOSXR", "JUNOS"}


class RenderError(Exception):
    pass


def env_tag(tag: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(tag).upper())


def _records(cfg, name, devices, groups, filters):
    path = inventory.resolve(cfg.data_path, name)
    if not consumers.allowed_inventory(cfg, path.stem):
        raise RenderError(f"inventory {name!r} is not available to consumer {cfg.consumer}")
    recs = inventory.load_all(path)["DEVICE"]
    return path, inventory.select(recs, names=devices, groups=groups, filters=filters)


def render(cfg: config.Config, name: str, fmt: str, devices=None, groups=None, filters=None,
           save: bool = True) -> dict:
    fmt = str(fmt or "").strip().lower()
    if fmt not in FORMATS:
        raise RenderError(f"format must be one of {', '.join(FORMATS)}")
    try:
        cat = catalog_mod.Catalog(cfg.catalog_path)
    except catalog_mod.CatalogError as e:
        raise RenderError(str(e)) from None
    path, recs = _records(cfg, name, devices, groups, filters)
    if not recs:
        raise RenderError("no enabled devices matched")
    skipped, use = [], []
    for r in recs:
        n = r.get("DEVICE_NAME") or r.get("DEVICE_ID")
        tag = r.get("CREDENTIAL")
        if not tag or not consumers.allowed_tag(cfg, str(tag)):
            skipped.append(f"{n}: no credential tag this consumer may use")
            continue
        pid = cat.resolve(r.get("OS"))
        if not pid:
            skipped.append(f"{n}: OS {r.get('OS')!r} is not in the platform catalogue")
            continue
        use.append((n, r, pid, cat.platforms[pid]))
    files: dict[str, str] = {}
    head = lambda: HEADER.format(inv=path.stem, when=time.strftime("%Y-%m-%d %H:%M:%S"),  # noqa: E731
                                 consumer=cfg.consumer, how=HOW[fmt])
    base = path.stem.removeprefix("INVENTORY_")
    if fmt == "pyats":
        devs = {}
        for n, r, pid, p in use:
            if not p.get("pyats"):
                skipped.append(f"{n}: {pid} has no pyATS os name")
                continue
            t = env_tag(r["CREDENTIAL"])
            devs[n] = {"os": p["pyats"], "type": "router", "alias": n,
                       "credentials": {"default": {"username": f"%ENV{{NAVI_CRED_{t}_USERNAME}}",
                                                   "password": f"%ENV{{NAVI_CRED_{t}_PASSWORD}}"},
                                       "enable": {"password": f"%ENV{{NAVI_CRED_{t}_ENABLE}}"}},
                       "connections": {"cli": {"protocol": "ssh", "ip": r["DEVICE_ADDR"],
                                               "port": int(r.get("SSH_PORT") or (r.get("PORT") if str(
                                                   r.get("CONN_TYPE", "SSH")).upper() == "SSH" else 22) or 22)}},
                       "custom": {"atlas_credential": r["CREDENTIAL"]}}
        if devs:
            files[f"{base}-pyats-testbed.yaml"] = head() + yaml.safe_dump(
                {"testbed": {"name": base}, "devices": devs}, sort_keys=False)
    elif fmt == "ansible":
        by_os: dict[str, dict] = {}
        by_cred: dict[str, dict] = {}
        hosts = {}
        for n, r, pid, p in use:
            if not p.get("ansible"):
                skipped.append(f"{n}: {pid} has no Ansible network_os name")
                continue
            eapi = str(r.get("CONN_TYPE", "")).upper() == "EAPI"
            hosts[n] = {"ansible_host": r["DEVICE_ADDR"], **({"ansible_port": int(r["PORT"])} if r.get("PORT") else {})}
            g = by_os.setdefault(f"os_{pid.lower()}", {"vars": {"ansible_network_os": p["ansible"],
                                                                "ansible_connection": "ansible.netcommon.httpapi"
                                                                if eapi else "ansible.netcommon.network_cli"},
                                                       "hosts": {}})
            g["hosts"][n] = None
            t = env_tag(r["CREDENTIAL"])
            cg = by_cred.setdefault(f"cred_{t.lower()}", {"vars": {
                "ansible_user": f"{{{{ lookup('env', 'NAVI_CRED_{t}_USERNAME') }}}}",
                "ansible_password": f"{{{{ lookup('env', 'NAVI_CRED_{t}_PASSWORD') }}}}",
                "ansible_become_password": f"{{{{ lookup('env', 'NAVI_CRED_{t}_ENABLE') }}}}"}, "hosts": {}})
            cg["hosts"][n] = None
        if hosts:
            files[f"{base}-ansible-inventory.yaml"] = head() + yaml.safe_dump(
                {"all": {"hosts": hosts, "children": {**by_os, **by_cred}}}, sort_keys=False)
    elif fmt == "anta":
        per_tag: dict[str, list] = {}
        for n, r, pid, p in use:
            if not p.get("anta"):
                skipped.append(f"{n}: {pid} is not an ANTA platform (Arista EOS only)")
                continue
            tags = r.get("TAGS") or {}
            ttags = [str(v).lower() for k in ("SITE", "FAB", "ROLE") for v in
                     (tags.get(k) if isinstance(tags.get(k), list) else [tags.get(k)]) if v]
            per_tag.setdefault(r["CREDENTIAL"], []).append(
                {"host": r["DEVICE_ADDR"], "name": n, "port": int(r.get("EAPI_PORT") or (
                    r.get("PORT") if str(r.get("CONN_TYPE", "EAPI")).upper() == "EAPI" else 443) or 443),
                 **({"tags": ttags} if ttags else {})})
        for tag, hs in per_tag.items():
            files[f"{base}-anta-{env_tag(tag).lower()}.yaml"] = head() + \
                f"# Credential tag {tag}: export ANTA_USERNAME / ANTA_PASSWORD for it before running anta.\n" + \
                yaml.safe_dump({"anta_inventory": {"hosts": hs}}, sort_keys=False)
    else:   # multivendor
        out = {}
        for n, r, pid, p in use:
            if pid in MULTIVENDOR_SKIP:
                skipped.append(f"{n}: {pid} is served by another server (pyATS / Junos), not multivendor")
                continue
            if not p.get("multivendor") and not p.get("netmiko"):
                skipped.append(f"{n}: {pid} has no multivendor platform name")
                continue
            tags = r.get("TAGS") or {}
            groups_ = [str(v) for k in ("SITE", "FAB", "ROLE") for v in
                       (tags.get(k) if isinstance(tags.get(k), list) else [tags.get(k)]) if v]
            out[n] = {"hostname": r["DEVICE_ADDR"], "platform": p.get("multivendor") or p["netmiko"],
                      **({"port": int(r["PORT"])} if r.get("PORT") and str(r.get("CONN_TYPE", "SSH")).upper() == "SSH"
                         else {}),
                      **({"groups": groups_} if groups_ else {}), "credential_ref": str(r["CREDENTIAL"])}
        if out:
            files[f"{base}-multivendor-inventory.yaml"] = head() + yaml.safe_dump(out, sort_keys=False)
    written = _save(cfg, files) if save and files else []
    names = {n for n, *_ in use} - {s.split(":", 1)[0] for s in skipped}
    return {"inventory": path.stem, "format": fmt, "files": files, "written": written,
            "devices": sorted(names), "skipped": skipped, "how": HOW[fmt]}


def _save(cfg, files: dict[str, str]) -> list[str]:
    d = cfg.state_path / "rendered" / cfg.consumer
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    out = []
    for fname, text in files.items():
        p = d / fname
        if p.exists() and not p.read_text(errors="replace").startswith(MARK):
            raise RenderError(f"{p} exists and was not written by ATLAS; not overwritten")
        tmp = p.with_suffix(p.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, p)
        out.append(str(p))
    return out
