"""RUDDER's front door logic (no MCP here): select devices via ATLAS, check commands against the
allowlist, pick an adapter, run, save records, summarise."""
from __future__ import annotations

import difflib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import adapters
from .atlas import Atlas, AtlasError
from .config import Settings, inv_key
from .records import Run


class RudderError(Exception):
    pass


def _norm(cmd: str) -> str:
    return re.sub(r"\s+", " ", str(cmd).strip()).lower()


# LAB_ANY_SHOW: a plain show command. No pipes, redirects, command chaining or shell characters.
SAFE_SHOW = re.compile(r"^show(\s+[A-Za-z0-9_./:,\-]+)+$", re.I)


def resolve_commands(allow: dict[str, list[str]], category: str | None, commands: list[str] | None,
                     platform_id: str, any_show: bool = False) -> list[str]:
    """Commands to run: a category and/or explicit commands, each of which must be on the allowlist.
    any_show (lab inventories with LAB_ANY_SHOW only): a plain 'show ...' command not on the list may run too."""
    if not allow and not (any_show and commands and not category):
        raise RudderError(f"{platform_id} has no command list yet, so nothing may be read from it "
                          f"(add one to COMMANDS.yml)")
    wanted: list[str] = []
    if category:
        match = next((c for c in allow if c.lower() == category.lower()), None)
        if not match:
            raise RudderError(f"no category {category!r} for {platform_id}; categories: {', '.join(sorted(allow))}")
        wanted += allow[match]
    if commands:
        listed = {_norm(c): c for cs in allow.values() for c in cs}
        free = {_norm(c): " ".join(str(c).split()) for c in commands
                if any_show and _norm(c) not in listed and SAFE_SHOW.match(" ".join(str(c).split()))}
        bad = [c for c in commands if _norm(c) not in listed and _norm(c) not in free]
        if bad:
            raise RudderError(f"not on the {platform_id} command list: {'; '.join(bad)}. "
                              f"Use command_list on ATLAS to see what is allowed"
                              + ("" if any_show else "; in a lab inventory the host can allow any plain "
                                                     "show command (LAB_ANY_SHOW in rudder.yml)"))
        wanted += [listed.get(_norm(c)) or free[_norm(c)] for c in commands]
    if not wanted:
        raise RudderError("give a category or a list of commands")
    seen, out = set(), []
    for c in wanted:
        if _norm(c) not in seen:
            seen.add(_norm(c))
            out.append(c)
    return out


def _scrub(lines: list | None, cred: dict) -> list | None:
    """Debug lines with every credential value of this connection replaced."""
    if lines is None:
        return None
    vals = sorted({str(v) for v in cred.values() if isinstance(v, (str, int)) and len(str(v)) >= 3},
                  key=len, reverse=True)
    out = []
    for ln in lines:
        s = str(ln)
        for v in vals:
            s = s.replace(v, "<REDACTED>")
        out.append(s)
    return out


def _as_text(o) -> str:
    if o is None:
        return ""
    return o if isinstance(o, str) else json.dumps(o, indent=1, sort_keys=True)


def _clip(obj, budget: int):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    if len(text) <= budget:
        return obj, False
    return text[:budget] + f"\n... ({len(text) - budget} more characters in the record file)", True


class Rudder:
    def __init__(self, s: Settings, atlas: Atlas | None = None, factories: dict | None = None):
        self.s = s
        self.atlas = atlas or Atlas(s)
        self.factories = factories or {}

    def _devices(self, inventory: str, devices, groups, filters) -> list[dict]:
        try:
            recs = self.atlas.records(inventory, names=devices, groups=groups, filters=filters)
        except AtlasError as e:
            raise RudderError(str(e)) from None
        if not recs and devices:
            raise RudderError(f"not found (or disabled) in {inventory}: {', '.join(devices)}")
        if not recs:
            raise RudderError("no devices matched")
        if devices:
            found = {str(r.get(k, "")).upper() for r in recs for k in ("DEVICE_NAME", "DEVICE_ID")}
            missing = [d for d in devices if d.upper() not in found]
            if missing:
                raise RudderError(f"not found (or disabled) in {inventory}: {', '.join(missing)}")
        return recs

    def is_lab(self, inventory: str) -> bool:
        return inv_key(inventory) in self.s.lab_inventories

    def _plan(self, dev: dict, category, commands, via=None, raw=False, debug=False, any_show=False):
        """(record as used, platform, adapter, commands) or a reason it cannot be read."""
        try:
            plat = self.atlas.platform(dev.get("OS"))
        except AtlasError as e:
            return dev, None, None, None, str(e)
        ad, why = adapters.choose(plat, dev, self.s, self.factories, via=via)
        if ad is None:
            return dev, plat, None, None, why
        ad.raw, ad.debug = bool(raw), ([] if debug else None)
        try:
            v = adapters.via_name(via) or adapters.via_name(dev.get("ADAPTER"))
        except ValueError:
            v = None
        dev = adapters.effective_dev(dev, v)
        try:
            cmds = resolve_commands(self.atlas.commands(plat["ID"]), category, commands, plat["ID"], any_show)
        except (RudderError, AtlasError) as e:
            return dev, plat, ad, None, str(e)
        if not dev.get("CREDENTIAL"):
            return dev, plat, ad, None, "no credential tag this consumer may use"
        return dev, plat, ad, cmds, None

    def _one(self, dev, plat, ad, cmds) -> tuple[str, list[dict] | None, str | None, float, list | None]:
        t0 = time.time()
        try:
            cred = self.atlas.credential(dev["CREDENTIAL"])
        except AtlasError as e:
            return "error", None, f"credential: {e}", 0.0, None
        try:
            return "ok", ad.run(dev, cred, cmds), None, round(time.time() - t0, 2), _scrub(ad.debug, cred)
        except adapters.DeviceError as e:
            return e.kind, None, str(e), round(time.time() - t0, 2), _scrub(ad.debug, cred)
        except Exception as e:  # noqa: BLE001
            return "error", None, f"{type(e).__name__}", round(time.time() - t0, 2), _scrub(ad.debug, cred)
        finally:
            cred.clear()

    def read(self, inventory: str, devices=None, groups=None, filters=None, category=None,
             commands=None, via: str | None = None, raw: bool = False, debug: bool = False) -> dict:
        """via: connection method for this call (adapters.VIAS); raw: the device's own text;
        debug: return the adapter's connection log (scrubbed)."""
        if via:
            try:
                adapters.via_name(via)
            except ValueError as e:
                raise RudderError(str(e)) from None
        recs = self._devices(inventory, devices, groups, filters)
        any_show = self.s.lab_any_show and self.is_lab(inventory)
        run = Run(self.s.records_path, {"consumer": self.s.consumer, "inventory": inventory,
                                        "category": category, "commands": commands, "via": via, "raw": raw})
        plans = [self._plan(d, category, commands, via, raw, debug, any_show) for d in recs]
        todo = [(d, p, a, c) for d, p, a, c, why in plans if why is None]
        started = time.time()
        with ThreadPoolExecutor(max_workers=max(1, min(self.s.max_workers, len(todo) or 1))) as ex:
            done = dict(zip([d["DEVICE_NAME"] for d, *_ in todo],
                            ex.map(lambda t: self._one(*t), todo)))
        budget = max(2000, self.s.max_inline_chars // max(1, len(todo)))
        out, status = [], {}
        for d, plat, ad, cmds, why in plans:
            name = d["DEVICE_NAME"]
            entry = {"device": name, "platform": plat.get("ID") if plat else None,
                     "adapter": getattr(ad, "name", None)}
            if why:
                entry.update(status="skipped", reason=why)
            else:
                st, results, err, secs, dbg = done[name]
                entry["status"] = st
                entry["seconds"] = secs
                if err:
                    entry["error"] = err
                if dbg is not None:
                    entry["debug"] = dbg
                if results is not None:
                    paths = run.save(name, results)
                    each = max(500, budget // max(1, len(results)))
                    entry["results"] = []
                    for r, p in zip(results, paths):
                        shown, cut = _clip(r["output"], each) if r["output"] is not None else (None, False)
                        entry["results"].append({"command": r["command"], "ok": r["ok"], "format": r["format"],
                                                 "output": shown, "truncated": cut, "error": r["error"],
                                                 "record": p})
            status[name] = {k: entry.get(k) for k in ("status", "platform", "adapter", "reason", "error")}
            out.append(entry)
        run_file = run.finish(status)
        counts: dict[str, int] = {}
        for e in out:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        return {"run": run.id, "records": str(run.path), "run_file": str(run_file), "counts": counts,
                "seconds": round(time.time() - started, 1), "devices": out}

    def credential_test(self, inventory: str, device: str, via: str | None = None, debug: bool = False) -> dict:
        dev = self._devices(inventory, [device], None, None)[0]
        dev, plat, ad, _, why = self._plan(dev, "system", None, via, False, debug)   # only the adapter is used
        if ad is None:
            return {"device": device, "status": "skipped", "reason": why}
        if not dev.get("CREDENTIAL"):
            return {"device": device, "status": "skipped", "reason": "no credential tag this consumer may use"}
        allow = self.atlas.commands(plat["ID"])
        probe = (next((c for cs in allow.values() for c in cs if _norm(c) == "show version"), None)
                 or next(iter(allow.get("system") or []), None))     # PAN-OS: show system info
        if not probe:
            return {"device": device, "status": "skipped",
                    "reason": f"no 'show version' and no system command on the {plat['ID']} list"}
        st, results, err, secs, dbg = self._one(dev, plat, ad, [probe])
        ok = st == "ok" and results and results[0]["ok"]
        return {"device": device, "platform": plat["ID"], "adapter": ad.name, "credential": dev["CREDENTIAL"],
                "status": "ok" if ok else (st if st != "ok" else "command_failed"), "seconds": secs,
                **({"error": err} if err else {}), **({"debug": dbg} if dbg is not None else {})}

    def compare(self, inventory: str, device: str, vias: list[str], commands=None, category=None,
                raw: bool = False, debug: bool = False) -> dict:
        """The same reads on one device through two or more connection methods, side by side: status,
        adapter, time and errors per method, and per command whether the outputs match (with a diff)."""
        vias = [v.strip() for v in (vias or []) if str(v).strip()]
        if len(vias) < 2:
            raise RudderError("compare needs at least two connection methods, e.g. via=['ssh', 'pyats']")
        for v in vias:
            try:
                adapters.via_name(v)
            except ValueError as e:
                raise RudderError(str(e)) from None
        runs = []
        for v in vias:
            r = self.read(inventory, devices=[device], category=category, commands=commands, via=v,
                          raw=raw, debug=debug)
            d = r["devices"][0]
            runs.append({"via": v, "adapter": d.get("adapter"), "status": d["status"], "seconds": d.get("seconds"),
                         "error": d.get("error") or d.get("reason"), "results": d.get("results"),
                         **({"debug": d["debug"]} if "debug" in d else {}), "records": r["records"]})
        base = runs[0]
        by_cmd = {x["command"]: x for x in base.get("results") or []}
        diffs = []
        for other in runs[1:]:
            for x in other.get("results") or []:
                b = by_cmd.get(x["command"])
                if b is None:
                    continue
                if not (b["ok"] and x["ok"]):
                    failed = [f"{v['via']}: {r['error']}" for v, r in ((base, b), (other, x)) if not r["ok"]]
                    diffs.append({"command": x["command"], "between": [base["via"], other["via"]], "same": False,
                                  "note": "failed on " + "; ".join(failed)})
                    continue
                if b["format"] != x["format"]:
                    diffs.append({"command": x["command"], "between": [base["via"], other["via"]], "same": False,
                                  "note": f"formats differ ({b['format']} vs {x['format']}); compare with raw=true"})
                    continue
                ta, tb = _as_text(b["output"]), _as_text(x["output"])
                d = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(), base["via"], other["via"], lineterm="", n=1))
                diffs.append({"command": x["command"], "between": [base["via"], other["via"]], "same": ta == tb,
                              **({"diff": "\n".join(d[:200]) + ("\n..." if len(d) > 200 else "")} if d else {})})
        return {"device": device, "inventory": inventory, "methods": runs, "compare": diffs}

    def facts(self, inventory: str, device: str) -> dict:
        dev = self._devices(inventory, [device], None, None)[0]
        dev, plat, ad, _, why = self._plan(dev, "system", None)
        if ad is None or plat is None:
            raise RudderError(why or "no adapter")
        if plat["ID"] != "EOS":
            raise RudderError(f"device_facts is EOS-only in P3 ({plat['ID']}: use device_read category system)")
        res = self.read(inventory, devices=[device], commands=["show version", "show hostname"])
        d = res["devices"][0]
        if d["status"] != "ok":
            return {"device": device, "status": d["status"], "error": d.get("error") or d.get("reason")}
        by = {r["command"]: r["output"] for r in d["results"] if r["ok"] and r["format"] == "json"}
        v, h = by.get("show version") or {}, by.get("show hostname") or {}
        return {"device": device, "status": "ok", "hostname": h.get("hostname"), "fqdn": h.get("fqdn"),
                "model": v.get("modelName"), "version": v.get("version"), "serial": v.get("serialNumber"),
                "system_mac": v.get("systemMacAddress"), "uptime_s": v.get("uptime"),
                "records": res["records"]}
