"""Change validation (decision #158): checked by RUDDER itself, before the approval and after the commit.

The chat agent never carries a validation result, so it cannot skip the check or fake a pass.

VALIDATION (rudder.local.yml, set with navigator-setup; shell only, like APPROVAL):
  MODE            none (default) | batfish | netclaw
                    batfish  a Batfish service at HOST:PORT (pip install navigator-mcp[batfish]; Docker image
                             batfish/allinone). HOST must be a local or private address.
                    netclaw  the Batfish service NetClaw's batfish-mcp uses (its BATFISH_HOST / BATFISH_PORT)
  HOST, PORT      batfish / netclaw: the service (default 127.0.0.1, pybatfish's default port)
  ON_UNREACHABLE  block (default) | warn: what happens when the validator cannot be reached
  POST_PUSH       true (default) | false: check the live configs again after the commit (a fail rolls back)
  TIMEOUT         seconds (default 300)

A result is {"validator", "stage" (before | after), "status" (pass | warn | fail | skipped), "summary",
"details": [...]}. Before the push a fail blocks the change; after the commit a fail rolls it back.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path

MODES = ("none", "batfish", "netclaw")   # "command" (own script) left out for now (user, 2026-11-17)
STATUSES = ("pass", "warn", "fail", "skipped")
MAX_DETAILS = 30
# Batfish picks the parser from the file; this first line makes it certain (RANCID header).
RANCID = {"ARISTA_EOS": "arista", "EOS": "arista", "CISCO_IOS": "cisco", "CISCO_XE": "cisco",
          "CISCO_IOSXE": "cisco", "IOSXE": "cisco", "CISCO_NXOS": "cisco-nx", "NXOS": "cisco-nx"}
NETCLAW_ENV_FILES = ("~/netclaw/.env", "~/.openclaw/.env", "~/.netclaw/.env")


class ValidationError(Exception):
    """The settings are wrong (bad MODE, public HOST...)."""


class Unreachable(Exception):
    """The validator could not be reached or did not answer."""


def _truthy(v, default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def settings(s) -> dict:
    """The VALIDATION section with defaults, checked."""
    raw = dict(getattr(s, "validation", None) or {})
    mode = str(raw.get("MODE") or "none").strip().lower()
    if mode not in MODES:
        raise ValidationError(f"VALIDATION.MODE must be one of {', '.join(MODES)}")
    unr = str(raw.get("ON_UNREACHABLE") or "block").strip().lower()
    if unr not in ("block", "warn"):
        raise ValidationError("VALIDATION.ON_UNREACHABLE must be block or warn")
    out = {"MODE": mode, "ON_UNREACHABLE": unr, "POST_PUSH": _truthy(raw.get("POST_PUSH"), True),
           "TIMEOUT": int(raw.get("TIMEOUT") or 300), "HOST": str(raw.get("HOST") or "").strip(),
           "PORT": int(raw["PORT"]) if str(raw.get("PORT") or "").strip() else None}
    if mode == "netclaw" and not out["HOST"]:
        found = netclaw_service()
        if found:
            out["HOST"], out["PORT"] = found
    if mode in ("batfish", "netclaw"):
        out["HOST"] = out["HOST"] or "127.0.0.1"
    return out


def enabled(s) -> bool:
    try:
        return settings(s)["MODE"] != "none"
    except ValidationError:
        return True                    # a broken setting must not turn validation off silently


def netclaw_service(files=NETCLAW_ENV_FILES) -> tuple[str, int | None] | None:
    """NetClaw's Batfish service: BATFISH_HOST / BATFISH_PORT from its environment or env files."""
    host, port = os.environ.get("BATFISH_HOST"), os.environ.get("BATFISH_PORT")
    for f in files:
        p = Path(f).expanduser()
        if host or not p.is_file():
            continue
        for ln in p.read_text(errors="replace").splitlines():
            m = re.match(r"^\s*(?:export\s+)?(BATFISH_HOST|BATFISH_PORT)\s*=\s*['\"]?([^'\"\s#]+)", ln)
            if m and m.group(1) == "BATFISH_HOST":
                host = m.group(2)
            elif m:
                port = m.group(2)
    if not host:
        return None
    return host, (int(port) if port and port.isdigit() else None)


def check_private(host: str) -> str:
    """Configs go to the validator: only a local or private address is allowed."""
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise ValidationError(f"VALIDATION.HOST {host}: cannot resolve it ({e})") from None
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            raise ValidationError(f"VALIDATION.HOST {host} is a public address ({a}); configs are only sent "
                                  f"to a local or private validator")
    return host


def _result(name, stage, status, summary, details=None) -> dict:
    return {"validator": name, "stage": stage, "status": status, "summary": str(summary)[:500],
            "details": [str(x)[:300] for x in (details or [])][:MAX_DETAILS]}


def _write_snapshot(root: Path, configs: dict[str, str], platforms: dict[str, str]) -> Path:
    d = root / "configs"
    d.mkdir(parents=True, mode=0o700)
    for name, text in configs.items():
        head = RANCID.get(str(platforms.get(name) or "").upper())
        body = text if not head or text.startswith("!RANCID-CONTENT-TYPE") else f"!RANCID-CONTENT-TYPE: {head}\n{text}"
        fd = os.open(d / f"{re.sub(r'[^A-Za-z0-9._-]', '_', name)}.cfg", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
    return root


class BatfishValidator:
    """Before vs after on one Batfish network per consumer; the snapshots are deleted afterwards.

    fail  a config Batfish cannot parse; a new undefined reference (route-map, prefix-list, ACL...);
          a BGP session that was compatible before and is not after
    warn  new unrecognized lines on a changed device; a BGP session that is gone
    info  how many routes changed on the changed devices (in the details)
    """
    name = "batfish"
    OK_BGP = ("UNIQUE_MATCH", "DYNAMIC_MATCH")

    def __init__(self, cfg: dict, consumer: str = "NAVI", session_factory=None):
        self.host = check_private(cfg["HOST"])
        self.port = cfg.get("PORT")
        self.network = f"rudder-{consumer}".lower()
        self.factory = session_factory

    def _session(self):
        if self.factory:
            return self.factory(self.host, self.port)
        try:
            from pybatfish.client.session import Session
        except ImportError:
            raise Unreachable("pybatfish is not installed (pip install navigator-mcp[batfish])") from None
        try:
            return Session(host=self.host, **({"port": self.port} if self.port else {}))
        except Exception as e:  # noqa: BLE001 - any connection problem
            raise Unreachable(f"Batfish at {self.host}: {type(e).__name__}: {str(e)[:200]}") from None

    def check(self, stage, before, after, platforms, changed, tag) -> dict:
        root = Path(tempfile.mkdtemp(prefix="rudder-val-"))
        bf = self._session()
        snaps = (f"{tag}-before".lower(), f"{tag}-after".lower())
        try:
            _write_snapshot(root / "before", before, platforms)
            _write_snapshot(root / "after", after, platforms)
            try:
                bf.set_network(self.network)
                bf.init_snapshot(str(root / "before"), name=snaps[0], overwrite=True)
                bf.init_snapshot(str(root / "after"), name=snaps[1], overwrite=True)
                frames = {q: {sn: getattr(bf.q, q)().answer(snapshot=sn).frame() for sn in snaps}
                          for q in ("fileParseStatus", "initIssues", "undefinedReferences", "bgpSessionCompatibility")}
                routes = bf.q.routes(nodes=",".join(changed)).answer(snapshot=snaps[1],
                                                                       reference_snapshot=snaps[0]).frame()
            except Unreachable:
                raise
            except Exception as e:  # noqa: BLE001
                raise Unreachable(f"Batfish at {self.host}: {type(e).__name__}: {str(e)[:200]}") from None
            return self._judge(stage, frames, routes, snaps, {c.lower() for c in changed})
        finally:
            for sn in snaps:
                try:
                    bf.delete_snapshot(sn)
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _rows(frame) -> list[dict]:
        return frame.to_dict("records") if frame is not None and len(frame) else []

    def _judge(self, stage, frames, routes, snaps, changed) -> dict:
        b, a = snaps
        fails, warns, info = [], [], []
        for r in self._rows(frames["fileParseStatus"][a]):
            if str(r.get("Status", "")).upper() not in ("PASSED", "PARTIALLY_UNRECOGNIZED", "EMPTY"):
                fails.append(f"{r.get('File_Name')}: Batfish could not parse it ({r.get('Status')})")

        def key_ref(r):
            return (str(r.get("File_Name")), str(r.get("Struct_Type")), str(r.get("Ref_Name")), str(r.get("Context")))
        old = {key_ref(r) for r in self._rows(frames["undefinedReferences"][b])}
        for r in self._rows(frames["undefinedReferences"][a]):
            if key_ref(r) not in old:
                fails.append(f"{Path(str(r.get('File_Name'))).stem}: {r.get('Struct_Type')} {r.get('Ref_Name')} "
                             f"is used ({r.get('Context')}) but not defined")

        def issue_key(r):
            return (str(r.get("Type")), str(r.get("Line_Text")).strip(), str(r.get("Details")))
        old_issues = {issue_key(r) for r in self._rows(frames["initIssues"][b])}
        for r in self._rows(frames["initIssues"][a]):
            if issue_key(r) in old_issues:
                continue
            where = ",".join(Path(str(x).split(":")[0]).stem for x in (r.get("Source_Lines") or [])) or str(r.get("Nodes"))
            text = f"{where}: {r.get('Type')}: {str(r.get('Line_Text') or r.get('Details')).strip()[:160]}"
            (fails if "error" in str(r.get("Type", "")).lower() else warns).append(text)

        def bgp_key(r):
            return (str(r.get("Node")), str(r.get("VRF")), str(r.get("Remote_IP") or r.get("Remote_Interface")))
        before_bgp = {bgp_key(r): str(r.get("Configured_Status")) for r in self._rows(frames["bgpSessionCompatibility"][b])}
        after_bgp = {bgp_key(r): str(r.get("Configured_Status")) for r in self._rows(frames["bgpSessionCompatibility"][a])}
        for k, st in before_bgp.items():
            if k not in after_bgp:
                if k[0].lower() in changed:
                    warns.append(f"{k[0]}: BGP session to {k[2]} (vrf {k[1]}) is gone")
            elif st in self.OK_BGP and after_bgp[k] not in self.OK_BGP:
                fails.append(f"{k[0]}: BGP session to {k[2]} (vrf {k[1]}) breaks: {st} -> {after_bgp[k]}")
        n = len(routes) if routes is not None else 0
        if n:
            info.append(f"{n} route(s) differ on {', '.join(sorted(changed))}")
        status = "fail" if fails else ("warn" if warns else "pass")
        summary = {"fail": f"{len(fails)} problem(s): " + "; ".join(fails[:2]),
                   "warn": f"{len(warns)} warning(s): " + "; ".join(warns[:2]),
                   "pass": "no new parse errors, undefined references or broken BGP sessions"}[status]
        if n:
            summary += f" ({info[0]})"
        return _result(self.name, stage, status, summary, fails + warns + info)


def make(cfg: dict, consumer: str = "NAVI", session_factory=None):
    if cfg["MODE"] in ("batfish", "netclaw"):
        v = BatfishValidator(cfg, consumer, session_factory)
        v.name = cfg["MODE"]
        return v
    return None


def run(s, stage: str, before: dict[str, str], after: dict[str, str | None], platforms: dict[str, str],
        changed: list[str], tag: str, session_factory=None) -> dict | None:
    """Validate one change. None when validation is off. Devices without a candidate config (after[d] None)
    are left out and named; with none left the result is 'skipped'. Never raises for the validator itself:
    an unreachable validator is a fail (ON_UNREACHABLE block) or a warn."""
    try:
        cfg = settings(s)
    except ValidationError as e:
        return _result("settings", stage, "fail", f"validation settings are wrong: {e}")
    if cfg["MODE"] == "none":
        return None
    left = sorted(d for d in changed if after.get(d) is None)
    use = [d for d in changed if after.get(d) is not None]
    note = [f"{d}: not checked (no candidate config for platform {platforms.get(d) or '?'})" for d in left]
    if not use:
        return _result(cfg["MODE"], stage, "skipped", "no device in this change can be validated", note)
    bef = {d: c for d, c in before.items() if c is not None and (d in use or d not in changed)}
    aft = {d: (after[d] if d in use else c) for d, c in bef.items()}
    try:
        v = make(cfg, getattr(s, "consumer", "NAVI"), session_factory)
        r = v.check(stage, bef, aft, platforms, use, tag)
    except ValidationError as e:
        return _result(cfg["MODE"], stage, "fail", f"validation settings are wrong: {e}")
    except Unreachable as e:
        if cfg["ON_UNREACHABLE"] == "warn":
            return _result(cfg["MODE"], stage, "warn", f"NOT validated: {e}", note)
        r = _result(cfg["MODE"], stage, "fail", f"validator not reachable: {e}", note)
        r["unreachable"] = True
        return r
    r["details"] = (r["details"] + note)[:MAX_DETAILS]
    return r


def post_push(s) -> bool:
    try:
        cfg = settings(s)
    except ValidationError:
        return True
    return cfg["MODE"] != "none" and cfg["POST_PUSH"]


def line(r: dict | None) -> str:
    """One line for the approval message and the chat."""
    if not r:
        return ""
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skipped": "➖"}.get(r["status"], "")
    return f"{icon} Validation ({r['validator']}): {r['status'].upper()}: {r['summary']}"


def test(cfg: dict, consumer: str = "NAVI", session_factory=None) -> str:
    """navigator-setup: prove the validator answers before the setting is saved."""
    if cfg["MODE"] == "none":
        return "VALIDATION off"
    sample = "hostname rudder-test\n!\ninterface Loopback0\n   ip address 10.255.0.1/32\n!\nend\n"
    r = run(type("S", (), {"validation": cfg, "consumer": consumer})(), "test", {"RUDDER-TEST": sample},
            {"RUDDER-TEST": sample.replace("10.255.0.1/32", "10.255.0.2/32")}, {"RUDDER-TEST": "ARISTA_EOS"},
            ["RUDDER-TEST"], "rudder-setup-test", session_factory)
    if r.get("unreachable") or r["status"] == "fail" or "NOT validated" in r["summary"]:
        raise ValidationError(r["summary"])
    return f"VALIDATION {cfg['MODE']} answers ({r['status']} on a test snapshot)"
