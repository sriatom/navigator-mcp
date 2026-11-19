"""navigator-guard: a Hermes plugin that keeps NAVIGATOR's private data where it belongs.

Active only in a profile that has $HERMES_HOME/navigator-guard.json (`navigator-setup connect hermes` writes it):
    {"always_local": false}    a project's mode decides (LOCAL / CLOUD, NAVIGATOR's .navigator_mode)
    {"always_local": true}     every session counts as LOCAL
    optional: "private_paths": [...], "local_hosts": [...], "alert": "telegram" (or slack: a Hermes `send` target),
              "navigator_bootstrap" / "atlas_bootstrap" / "rudder_bootstrap": the settings files (connect fills these)

Everything else comes from NAVIGATOR's own settings files (no paths in this file):
    navigator.yml   $NAVIGATOR_BOOTSTRAP or ~/.config/navigator/navigator.yml: PROJECT_ROOT, ADHOC,
                    MODELS.LOCAL.URL and MODELS.LOCAL.ALLOW_HOSTS (the local model counts as local)
    ATLAS           $ATLAS_BOOTSTRAP or ~/.config/atlas/bootstrap.yml: DATA_PATH, STATE_PATH, KEY_FILE
    RUDDER          ~/.rudder and the approval folder (APPROVAL.APPROVAL_DIR, default ~/.hermes/push-approvals)

Hooks
  pre_api_request / pre_auxiliary_call   remember each session's model endpoint; a LOCAL session that talks to
                                         a non-local model is logged (and alerted, if "alert" is set)
  pre_tool_call (blocks)
    1. a tool call naming a private path: the credential store, ATLAS / RUDDER state, the approval folder
    2. a tool call naming a project's mode or state file (.navigator_mode, ...): only NAVIGATOR's mode tool
    3. a tool call that touches a LOCAL project while the session is on a cloud model (or an unknown one):
       "/model local". NAVIGATOR's own tools (mcp__navigator__*) pass: they keep LOCAL data on the host.
    4. in a LOCAL session, web_search / web_extract / browser_*: no IPs, MACs, AS numbers, config or log
       lines, or the project's own names
  pre_gateway_dispatch (rewrites)        a message that is only a formatted /model command (`/model local`,
                                         *\\/model local*) becomes the real command
Unreadable settings fail closed: the guard then blocks every tool call and says why.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

log = logging.getLogger("navigator-guard")

NAV_TOOL = "mcp__navigator__"
MODE_FILES = (".navigator_mode", ".navigator_inventory", "NAVIGATOR_STATE.json", ".navi_mode", ".navi_inventory")
WEB_TOOLS = ("web_search", "web_extract")
FILE_TOOLS = ("terminal", "execute_code", "process", "read_file", "search_files", "write_file", "patch")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "policy": None}


# ------------------------------------------------------------------ settings
def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # noqa: BLE001
        return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _profile() -> str:
    return os.path.basename(_home().rstrip("/")) or "default"


def _cfg() -> dict | None:
    """navigator-guard.json of the profile serving this turn; None = guard off. Unreadable = strictest."""
    for name in ("navigator-guard.json", "navi-guard.json"):
        path = os.path.join(_home(), name)
        try:
            with open(path) as fh:
                d = json.load(fh) or {}
            d["always_local"] = bool(d.get("always_local", d.get("always_locked", False)))
            return d
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            return {"always_local": True}
    return None


def _yaml(path: str) -> dict:
    import yaml
    try:
        with open(os.path.expanduser(path)) as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def _exp(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


class Policy:
    def __init__(self, cfg: dict):
        # The files navigator-setup connect found for this install win; then the env, then the defaults.
        nav = _yaml(cfg.get("navigator_bootstrap") or os.environ.get("NAVIGATOR_BOOTSTRAP")
                    or "~/.config/navigator/navigator.yml")
        atlas = _yaml(cfg.get("atlas_bootstrap") or os.environ.get("ATLAS_BOOTSTRAP") or "~/.config/atlas/bootstrap.yml")
        rudder = _yaml(cfg.get("rudder_bootstrap") or os.environ.get("RUDDER_BOOTSTRAP") or "~/.config/rudder/rudder.yml")
        rlocal = _yaml("~/.config/rudder/rudder.local.yml")
        self.root = _exp(nav.get("PROJECT_ROOT") or nav.get("WORKSPACE") or "~/workspace")
        self.adhoc = os.path.join(self.root, str(nav.get("ADHOC") or "ADHOC_WORK"))
        appr = ((rlocal.get("APPROVAL") or {}).get("APPROVAL_DIR") or (rudder.get("APPROVAL") or {}).get("APPROVAL_DIR")
                or (nav.get("APPROVAL") or {}).get("APPROVAL_DIR") or "~/.hermes/push-approvals")
        paths = [atlas.get("DATA_PATH") or "~/.atlas/data", atlas.get("STATE_PATH") or "~/.atlas",
                 os.path.dirname(str(atlas.get("KEY_FILE") or "~/.config/atlas/vault.key")),
                 "~/.config/atlas", "~/.rudder", "~/.config/rudder", appr] + list(cfg.get("private_paths") or [])
        self.private = sorted({_exp(p) for p in paths if p}, key=len, reverse=True)
        self.private_raw = sorted({str(p).rstrip("/") for p in paths if p}, key=len, reverse=True)
        local = ((nav.get("MODELS") or {}).get("LOCAL") or {})
        self.local_hosts = {h.lower() for h in LOOPBACK | set(map(str, local.get("ALLOW_HOSTS") or []))
                            | set(map(str, cfg.get("local_hosts") or []))}
        url_host = (urlparse(str(local.get("URL") or "")).hostname or "").lower()
        if url_host:
            self.local_hosts.add(url_host)

    # paths
    def private_hit(self, text: str) -> str | None:
        for raw, real in zip(self.private_raw, self.private):
            for form in {raw, real, real.replace(os.path.expanduser("~"), "~", 1),
                         real.replace(os.path.expanduser("~"), "$HOME", 1)}:
                if form and len(form) > 3 and form in text:
                    return raw
        return None

    def projects_in(self, text: str) -> set[str]:
        out = set()
        for base in (self.adhoc, self.root):
            for form in {base, base.replace(os.path.expanduser("~"), "~", 1),
                         base.replace(os.path.expanduser("~"), "$HOME", 1)}:
                for m in re.finditer(re.escape(form.rstrip("/")) + r"/([A-Za-z0-9][A-Za-z0-9._-]*)", text):
                    d = os.path.join(base, m.group(1))
                    if os.path.isdir(d) and d != self.adhoc:
                        out.add(d)
        return out

    def project_dir(self, name) -> str | None:
        if not isinstance(name, str) or not NAME.match(name):
            return None
        for base in (self.root, self.adhoc):
            d = os.path.join(base, name)
            if os.path.isdir(d):
                return d
        return None

    @staticmethod
    def mode(pdir: str) -> str:
        """LOCAL unless the project says CLOUD (or the older OPEN). Fails closed."""
        for f in (".navigator_mode", ".navi_mode"):
            try:
                with open(os.path.join(pdir, f)) as fh:
                    return "CLOUD" if fh.read().strip().upper() in ("CLOUD", "OPEN") else "LOCAL"
            except FileNotFoundError:
                continue
            except OSError:
                return "LOCAL"
        return "LOCAL"

    def is_local(self, base_url: str) -> bool:
        try:
            host = (urlparse(base_url or "").hostname or "").lower()
        except ValueError:
            return False
        return bool(host) and host in self.local_hosts


def _policy(cfg: dict) -> Policy:
    if _cache["policy"] is None or time.time() - _cache["at"] > 30:
        _cache.update(policy=Policy(cfg), at=time.time())
    return _cache["policy"]


# ------------------------------------------------------------------ the query check (same rules as research.py)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
_MAC = re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b|\b(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}\b", re.I)
_ASN = re.compile(r"\b(?:AS|ASN|remote-as|local-as|router bgp)\s*-?\s*(\d{1,10})\b", re.I)
_CONFIG = re.compile(r"^\s*(?:interface\s+\S+\d|ip address\s|ipv6 address\s|neighbor\s+\S+\s+\S+|"
                     r"router\s+(?:bgp|ospf|isis)\s+\d|hostname\s+\S+|description\s+\S+|snmp-server\s|"
                     r"username\s|set\s+\S+\s+\S+\s+\S+|%[A-Z0-9_]+-\d-[A-Z0-9_]+)", re.I | re.M)
_LOGLINE = re.compile(r"\b\d{1,2}:\d\d:\d\d(?:\.\d+)?\b.*%[A-Z0-9_]+-\d-", re.I)
_HOSTLIKE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*\d[A-Za-z0-9-]*")


def check_generic(query: str, private: list[str]) -> list[str]:
    why = []
    for m in _IPV4.findall(query):
        try:
            ipaddress.ip_network(m, strict=False)
            why.append(f"an IP address ({m})")
        except ValueError:
            pass
    for tok in re.findall(r"[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(?:/\d{1,3})?", query):
        try:
            if ":" in tok and tok.count(":") >= 2 and ipaddress.ip_network(tok, strict=False).version == 6:
                why.append(f"an IPv6 address ({tok})")
        except ValueError:
            pass
    if _MAC.search(query):
        why.append("a MAC address")
    for m in _ASN.finditer(query):
        why.append(f"an AS number ({m.group(1)})")
    if _CONFIG.search(query) or _LOGLINE.search(query) or query.count("\n") >= 2:
        why.append("a config or log line (describe the problem instead)")
    low = query.lower()
    for v in private:
        if re.search(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?![a-z0-9])", low):
            why.append(f"a real name from this project ({v[:2]}…)")
    return list(dict.fromkeys(why))


def project_names(pdir: str) -> list[str]:
    """The project's own names: its folder name, its inventory, and host-like names in its file names."""
    vals = [os.path.basename(pdir)]
    for f in (".navigator_inventory", ".navi_inventory"):
        try:
            with open(os.path.join(pdir, f)) as fh:
                inv = fh.read().strip()
                vals += [inv, re.sub(r"^INVENTORY_", "", inv)]
        except OSError:
            pass
    try:
        for i, (_d, _s, files) in enumerate(os.walk(pdir)):
            for name in files:
                vals += [t for t in _HOSTLIKE.findall(os.path.splitext(name)[0]) if len(t) >= 4]
            if i > 50:
                break
    except OSError:
        pass
    return [v for v in dict.fromkeys(vals) if len(v) >= 3]


# ------------------------------------------------------------------ session state, log, alert
def _state(sid: str) -> dict:
    with _lock:
        return _sessions.setdefault(sid or "?", {"local": None, "model": "", "locked": set(), "alerted": False})


def _audit(line: str) -> None:
    try:
        os.makedirs(os.path.join(_home(), "logs"), exist_ok=True)
        with open(os.path.join(_home(), "logs", "navigator-guard.log"), "a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def _alert(cfg: dict, text: str) -> None:
    target = cfg.get("alert")
    if not target:
        return
    hermes = os.environ.get("HERMES_BIN") or os.path.expanduser("~/.local/bin/hermes")

    def send():
        try:
            subprocess.run([hermes, "-p", _profile(), "send", "-t", str(target), "-f", "-"], input=text,
                           text=True, capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=send, daemon=True).start()


def _block(sid: str, tool: str, msg: str) -> dict:
    _audit(f"BLOCK session={sid} tool={tool}: {msg}")
    return {"action": "block", "message": f"navigator-guard: {msg}"}


# ------------------------------------------------------------------ hooks
def _on_request(kind: str, **kw) -> None:
    cfg = _cfg()
    if cfg is None:
        return
    try:
        pol = _policy(cfg)
    except Exception:  # noqa: BLE001
        return
    sid = kw.get("session_id") or ""
    st = _state(sid)
    local = pol.is_local(kw.get("base_url") or "")
    if kind == "main":
        st.update(local=local, model=kw.get("model") or "")
    if (cfg["always_local"] or st["locked"]) and not local:
        _audit(f"LEAK-RISK session={sid} kind={kind} provider={kw.get('provider')} url={kw.get('base_url')} "
               f"local_projects={sorted(os.path.basename(p) for p in st['locked'])}")
        if not st["alerted"]:
            st["alerted"] = True
            _alert(cfg, f"navigator-guard ({_profile()}): a LOCAL session sent a request to a non-local model "
                        f"({kw.get('provider')} {kw.get('model')}, {kind}). Session {sid}.")


def on_api_request(**kw) -> None:
    _on_request("main", **kw)


def on_aux_request(**kw) -> None:
    _on_request("aux", **kw)


def _query_text(args: dict) -> str:
    parts = [str(args.get(k)) for k in ("query", "q", "url", "urls", "text", "prompt") if args.get(k)]
    return " ".join(parts) if parts else json.dumps(args)


def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    cfg = _cfg()
    if cfg is None:
        return None
    sid = kw.get("session_id") or ""
    tool = tool_name or ""
    try:
        pol = _policy(cfg)
    except Exception as e:  # noqa: BLE001   fail closed
        return _block(sid, tool, f"cannot read NAVIGATOR's settings ({type(e).__name__}: {e}); every tool is "
                                 "blocked until that is fixed. Tell the user.")
    args = args or {}
    text = json.dumps(args, default=str)
    nav = tool.startswith(NAV_TOOL)

    if not nav:
        hit = pol.private_hit(text)
        if hit:
            return _block(sid, tool, f"{hit} is private (credentials, approvals or device state). Use NAVIGATOR's "
                                     "tools; never read or change it directly.")
        if any(m in text for m in MODE_FILES):
            return _block(sid, tool, "a project's mode and state files change only through NAVIGATOR "
                                     "(mode action=set).")

    st = _state(sid)
    if st["local"] is None and kw.get("parent_session_id") in _sessions:
        st["local"] = _sessions[kw["parent_session_id"]]["local"]
    on_cloud = st["local"] is not True                 # unknown endpoint = cloud (fail closed)

    projects = pol.projects_in(text)
    for key in ("workdir", "cwd"):
        if isinstance(args.get(key), str):
            projects |= pol.projects_in(args[key])
    if tool in ("terminal", "execute_code", "process"):
        for p in pol.projects_in(text):
            if re.search(r"\bcd\s+['\"]?" + re.escape(p), text):
                st["cwd_project"] = p
    if st.get("cwd_project") and tool in FILE_TOOLS:
        projects.add(st["cwd_project"])
    local_projects = {p for p in projects if pol.mode(p) == "LOCAL"}
    if nav:
        pd = pol.project_dir(args.get("project"))
        if pd and pol.mode(pd) == "LOCAL":
            st["locked"].add(pd)                       # remembered for the web-query check
        local_projects = set()
    if local_projects and on_cloud:
        names = ", ".join(sorted(os.path.basename(p) for p in local_projects))
        return _block(sid, tool, f"project {names} is a LOCAL project (local model only) and this session is on a "
                                 f"cloud model ({st['model'] or 'unknown'}). Nothing from it may go to the cloud. "
                                 "Ask the user to type  /model local  and retry. Do not work around this.")
    st["locked"] |= local_projects

    if (cfg["always_local"] or st["locked"]) and (tool in WEB_TOOLS or tool.startswith("browser")):
        names: list[str] = []
        for p in st["locked"]:
            names += project_names(p)
        why = check_generic(_query_text(args), names)
        if why:
            return _block(sid, tool, "LOCAL session: this would send project data to the internet (" +
                          "; ".join(why[:5]) + "). Use generic vendor / product / version / feature terms only.")
    return None


_WRAPPED_MODEL_CMD = re.compile(r"^[`*_~\s]*(/model(?:[ \t]+[^`*_~\n]*?)?)[`*_~\s.]*$", re.IGNORECASE)


def on_gateway_dispatch(event=None, **_kw):
    """A message that is only a formatted /model command (`/model local`) runs as the real command."""
    text = getattr(event, "text", None)
    if not isinstance(text, str) or text.lstrip().startswith("/"):
        return None
    m = _WRAPPED_MODEL_CMD.match(text)
    if not m:
        return None
    cmd = " ".join(m.group(1).split())
    return {"action": "rewrite", "text": "/model" + cmd[len("/model"):]}


def register(ctx) -> None:
    ctx.register_hook("pre_api_request", on_api_request)
    ctx.register_hook("pre_auxiliary_call", on_aux_request)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", on_gateway_dispatch)
