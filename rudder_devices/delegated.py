"""Delegated adapters: RUDDER as an MCP *client* of existing open-source MCP servers.

The launcher starts such a server as a child process over stdio for one device:
  - a private work folder (mode 700) holds the inventory / testbed RUDDER writes for that device
    (names, addresses, platform, and a credential *reference* only);
  - the credential is put only into the child's environment, under the names that server reads;
  - the child gets a minimal environment (no inherited secrets), and the folder is removed after.
Results come back in RUDDER's record format, so callers cannot tell native from delegated.
RUDDER's own rules still apply first: the command allowlist, and (for writes, later) /pushok.

Servers (installed by scripts/install_delegated.sh under DELEGATED_HOME, default ~/.rudder/delegated):
  pyats        upstream pyATS MCP server   -> pyats_run_show_command   (Genie-parsed where possible)
  multivendor  multivendor-cli-mcp         -> run_command              (netmiko, raw text)
  fortinet     fortinet-mcp                -> fgt_* tools              (FortiGate REST, JSON)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from .adapters import DeviceError, _Base, _result
from .config import Settings

SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
DEBUG_LOG_CHARS = 12000      # how much of a delegated server's own log debug=true returns (the end of it)


def server_specs(s: Settings) -> dict[str, dict]:
    raw = s.delegated or {}
    home = Path(os.path.expanduser(str(raw.get("HOME", "~/.rudder/delegated"))))
    nc = home / str(raw.get("SOURCE_DIR", "src")) / "mcp-servers"
    specs = {
        "multivendor": {"command": [str(home / "venvs/multivendor/bin/python"), "-u",
                                    str(nc / "multivendor-cli-mcp/server.py")],
                        "cwd": str(nc / "multivendor-cli-mcp")},
        "fortinet": {"command": [str(home / "venvs/fortinet/bin/python"), "-u", str(nc / "fortinet-mcp/server.py")],
                     "cwd": str(nc / "fortinet-mcp")},
        # upstream pyATS MCP has no stdio: HTTP on 127.0.0.1 and a free port, for one call only
        "pyats": {"command": [str(home / "venvs/pyats/bin/python"), "-u", str(home / "pyATS_MCP/pyats_mcp_server.py")],
                  "cwd": str(home / "pyATS_MCP"), "transport": "http", "path": "/mcp",
                  "host_env": "PYATS_MCP_HTTP_HOST", "port_env": "PYATS_MCP_HTTP_PORT"},
    }
    for name, over in (raw.get("SERVERS") or {}).items():
        specs[str(name).lower()] = {**specs.get(str(name).lower(), {}), **{k.lower(): v for k, v in over.items()}}
    return specs


def _die_with_parent() -> None:
    """Linux: the delegated server gets SIGTERM if RUDDER dies, so a killed read leaves no server behind."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGTERM))   # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001  (not Linux: nothing to do)
        pass


def installed(spec: dict) -> bool:
    cmd = spec.get("command") or []
    return bool(cmd) and all(Path(p).exists() for p in cmd if p.startswith("/"))


class Launcher:
    """Start one delegated server, call tools, stop it. Override `call` in tests."""

    def __init__(self, spec: dict, timeout: int):
        self.spec, self.timeout = spec, timeout

    def call(self, env: dict, calls: list[tuple[str, dict]]) -> list:
        if self.spec.get("transport") == "http":
            return self._call_http(env, calls)
        return asyncio.run(self._call(env, calls))

    def _call_http(self, env: dict, calls):
        """For servers without stdio (upstream pyATS MCP is HTTP-only): bind to 127.0.0.1 on a free
        port, wait until it listens, call, then always stop it. Never 0.0.0.0."""
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        env = {**env, self.spec["host_env"]: "127.0.0.1", self.spec["port_env"]: str(port)}
        log = open(Path(env.get("HOME", tempfile.gettempdir())) / "server.log", "wb")
        proc = subprocess.Popen(self.spec["command"], env=env, cwd=self.spec.get("cwd"),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, preexec_fn=_die_with_parent)
        try:
            deadline = time.time() + int(self.spec.get("start_timeout", 90))
            while True:
                if proc.poll() is not None:
                    raise DeviceError("error", f"delegated server exited during start (code {proc.returncode})")
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    if time.time() > deadline:
                        raise DeviceError("error", "delegated server did not start in time") from None
                    time.sleep(0.5)
            url = f"http://127.0.0.1:{port}{self.spec.get('path', '/mcp')}"
            return asyncio.run(self._call(env, calls, url=url))
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            log.close()

    async def _call(self, env: dict, calls, url: str | None = None):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport
        if url:
            t = url
        else:
            cmd = self.spec["command"]
            t = StdioTransport(command=cmd[0], args=cmd[1:], env=env, cwd=self.spec.get("cwd"))
        out = []
        async with Client(t, timeout=self.timeout + 30) as c:
            for tool, args in calls:
                try:
                    r = await c.call_tool(tool, args, raise_on_error=False)
                except Exception as e:  # noqa: BLE001
                    out.append({"_error": f"{type(e).__name__}: {str(e)[:200]}"})
                    continue
                if r.is_error:
                    text = " ".join(getattr(x, "text", "") for x in r.content)[:300]
                    out.append({"_error": text or "tool error"})
                    continue
                sc = r.structured_content
                if isinstance(sc, dict) and set(sc) == {"result"}:
                    sc = sc["result"]
                if sc is None:
                    sc = "\n".join(getattr(x, "text", "") for x in r.content)
                if isinstance(sc, str) and sc.lstrip()[:1] in ("{", "["):   # tools that return json.dumps(...)
                    try:
                        sc = json.loads(sc)
                    except ValueError:
                        pass
                out.append(sc)
        return out


def _base_env(workdir: Path) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    env["HOME"] = str(workdir)          # servers that write caches/artifacts do it in the temp folder
    env["PYTHONUNBUFFERED"] = "1"
    return env


class _Delegated(_Base):
    reads, writes = True, False

    def __init__(self, s: Settings, spec: dict, launcher: Launcher | None = None):
        self.s, self.spec = s, spec
        self.launcher = launcher or Launcher(spec, s.timeout)

    def _workdir(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="rudder-dlg-"))
        os.chmod(d, 0o700)
        return d

    def _call(self, env: dict, calls: list, wd: Path) -> list:
        """Call the server and, when debugging, keep a short trace and the server's own log."""
        t0 = time.time()
        self._log(f"start {self.name} ({' '.join(Path(c).name for c in self.spec.get('command', [])[1:])}) "
                  f"for {len(calls)} call(s)")
        try:
            res = self.launcher.call(env, calls)
        except DeviceError as e:
            self._log(f"{self.name} failed after {round(time.time() - t0, 1)} s: {e.kind}: {e}")
            raise
        finally:
            log = wd / "server.log"
            if self.debug is not None and log.is_file():
                text = log.read_text("utf-8", "replace")
                if text.strip():
                    self.debug.append(f"--- {self.name} server log (last {DEBUG_LOG_CHARS} characters) ---\n"
                                      + text[-DEBUG_LOG_CHARS:])
        for (tool, args), r in zip(calls, res):
            err = r.get("_error") if isinstance(r, dict) else None
            self._log(f"{tool} {json.dumps({k: v for k, v in args.items() if k != 'timeout'})[:200]} -> "
                      + (f"error: {str(err)[:300]}" if err else "returned"))
        self._log(f"{self.name} done in {round(time.time() - t0, 1)} s")
        return res


# unicon connect() defaults for RUDDER's pyATS reads (measured on the SD-WAN cEdge EDGE1, 2026-11-13, #121):
#  - init_config_commands []: unicon otherwise enters config mode at every login (no logging console,
#    line console 0 / exec-timeout 0). A read must never change the configuration, and on a
#    controller-managed cEdge 'configure terminal' is refused, so the login hangs.
#  - iosxe operating_mode true: unicon otherwise runs 'show version | include operating mode' and, on a
#    Controller-Managed device, sends 'pnpa service discovery stop', waits for the console to settle and
#    logs in a second time (~25 s). With it: one login, ~3 s, same parsed output.
PYATS_DEFAULT_ARGS = {"init_config_commands": []}
PYATS_DEFAULT_ARGS_BY_OS = {"iosxe": {"operating_mode": True}}



def _pyats_defaults() -> tuple[dict, dict]:
    """The Cisco pack's delegated.pyats defaults (pack.yml), else the constants above."""
    try:
        import navigator_packs
        d = (navigator_packs.pack("cisco").get("delegated") or {}).get("pyats") or {}
    except Exception:  # noqa: BLE001
        d = {}
    return (dict(d.get("default_args") or PYATS_DEFAULT_ARGS),
            {k: dict(v) for k, v in (d.get("default_args_by_os") or PYATS_DEFAULT_ARGS_BY_OS).items()})


def pyats_settings(s: Settings, os_name: str) -> dict:
    """rudder.yml DELEGATED.PYATS (all optional; the defaults above apply first):
        TIMEOUT          seconds per command, login included (default 90)
        CONN_CACHE_TTL   keep the login for the server's life, so a run logs in once (default 600; 0 = per command)
        CONNECTION_ARGS  more unicon connect() arguments for every device
        CONNECTION_ARGS_BY_OS  {iosxe: {...}}: per pyATS os name (wins over CONNECTION_ARGS)"""
    raw = (s.delegated or {}).get("PYATS") or {}
    base, by_os = _pyats_defaults()
    args = {**base, **by_os.get(os_name, {})}
    args.update(raw.get("CONNECTION_ARGS") or {})
    args.update(((raw.get("CONNECTION_ARGS_BY_OS") or {}).get(os_name)) or {})
    return {"timeout": int(raw.get("TIMEOUT", max(s.timeout, 90))),
            "cache_ttl": int(raw.get("CONN_CACHE_TTL", 600)), "args": args}


class PyatsAdapter(_Delegated):
    name = "delegated:pyats"

    def __init__(self, s, spec, os_name: str, launcher=None, settings: dict | None = None):
        self.cfg = settings or pyats_settings(s, os_name)
        super().__init__(s, spec, launcher or Launcher(spec, self.cfg["timeout"]))
        self.os_name = os_name

    def run(self, dev, cred, commands):
        wd = self._workdir()
        try:
            name = dev["DEVICE_NAME"]
            tb = {"testbed": {"name": "rudder"},
                  "devices": {name: {
                      "os": self.os_name, "type": "router",
                      "credentials": {"default": {"username": "%ENV{RUDDER_USERNAME}",
                                                  "password": "%ENV{RUDDER_PASSWORD}"},
                                      "enable": {"password": "%ENV{RUDDER_ENABLE}"}},
                      "connections": {"cli": {"protocol": "ssh", "ip": dev["DEVICE_ADDR"],
                                              "port": int(dev.get("PORT") or 22)}}}}}
            if self.cfg["args"]:
                tb["devices"][name]["custom"] = {"connection_args": dict(self.cfg["args"])}
            self._log(f"pyATS timeout {self.cfg['timeout']} s per command (login included), "
                      f"connection cache {self.cfg['cache_ttl']} s, connect args {self.cfg['args'] or 'none'}")
            (wd / "testbed.yaml").write_text(yaml.safe_dump(tb))
            env = {**_base_env(wd), "PYATS_TESTBED_PATH": str(wd / "testbed.yaml"),
                   "PYATS_MCP_CONN_CACHE_TTL": str(self.cfg["cache_ttl"]),
                   "PYATS_MCP_ARTIFACTS_DIR": str(wd / "artifacts"), "PYATS_MCP_KEEP_ARTIFACTS": "0",
                   "RUDDER_USERNAME": cred.get("USERNAME", ""),
                   "RUDDER_PASSWORD": cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or "",
                   "RUDDER_ENABLE": cred.get("ENABLE_PASSWORD") or cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or ""}
            res = self._call(env, [("pyats_run_show_command",
                                    {"device_name": name, "command": c, "timeout": self.cfg["timeout"]})
                                   for c in commands], wd)
            env.clear()
            return [self._one(c, r) for c, r in zip(commands, res)]
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    @staticmethod
    def _one(cmd, r):
        if isinstance(r, dict) and r.get("_error"):
            return _result(cmd, False, "text", None, f"pyATS server: {r['_error']}")
        if isinstance(r, dict) and r.get("status") == "completed":
            out = r.get("output")
            return _result(cmd, True, "json" if isinstance(out, (dict, list)) else "text", out)
        err = (r.get("error") if isinstance(r, dict) else str(r)) or "error"
        low = str(err).lower()
        if "auth" in low or "password" in low:
            raise DeviceError("auth_failed", "pyATS: login refused")
        if "unable to connect" in low or "connection" in low and "refused" in low:
            raise DeviceError("unreachable", "pyATS: cannot connect")
        return _result(cmd, False, "text", None, f"pyATS: {str(err)[:300]}")


class MultivendorAdapter(_Delegated):
    name = "delegated:multivendor"
    REF = "rudder"

    def __init__(self, s, spec, platform: str, launcher=None):
        super().__init__(s, spec, launcher)
        self.platform = platform

    def run(self, dev, cred, commands):
        wd = self._workdir()
        try:
            name = dev["DEVICE_NAME"]
            (wd / "inventory.yaml").write_text(yaml.safe_dump({name: {
                "hostname": dev["DEVICE_ADDR"], "platform": self.platform, "credential_ref": self.REF}}))
            env = {**_base_env(wd), "MULTIVENDOR_INVENTORY_SOURCE": "operator",
                   "MULTIVENDOR_INVENTORY_PATH": str(wd / "inventory.yaml"),
                   "MULTIVENDOR_SSH_STRICT": "1" if self.s.ssh_strict else "0",
                   "MULTIVENDOR_TIMEOUT_S": str(self.s.timeout),
                   "MULTIVENDOR_RUDDER_USERNAME": cred.get("USERNAME", ""),
                   "MULTIVENDOR_RUDDER_PASSWORD": cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or ""}
            if cred.get("ENABLE_PASSWORD"):
                env["MULTIVENDOR_RUDDER_ENABLE"] = cred["ENABLE_PASSWORD"]
            if self.s.known_hosts:
                env["MULTIVENDOR_KNOWN_HOSTS"] = str(self.s.known_hosts)
            res = self._call(env, [("run_command", {"device": name, "command": c, "timeout_s": self.s.timeout})
                                   for c in commands], wd)
            env.clear()
            return [self._one(c, r) for c, r in zip(commands, res)]
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    @staticmethod
    def _one(cmd, r):
        if isinstance(r, dict) and r.get("_error"):
            return _result(cmd, False, "text", None, f"multivendor server: {r['_error']}")
        st = r.get("status") if isinstance(r, dict) else None
        if st == "ok":
            return _result(cmd, True, "text", r.get("output", ""))
        if st in ("auth_failed", "unreachable", "timeout"):
            raise DeviceError(st, f"multivendor: {st}")
        detail = (r.get("error") or r.get("reason") or st) if isinstance(r, dict) else str(r)
        return _result(cmd, False, "text", None, f"multivendor: {str(detail)[:300]}")


class FortinetAdapter(_Delegated):
    """FortiGate REST through the fortinet-mcp server. 'Commands' are its read tool names
    (fgt_system_status, fgt_list_interfaces, ...), listed per category in COMMANDS.yml."""
    name = "delegated:fortinet"
    READ_TOOLS = {"fgt_system_status", "fgt_list_interfaces", "fgt_get_routes", "fgt_vpn_tunnels",
                  "fgt_get_policies", "fortinet_posture"}

    def run(self, dev, cred, commands):
        bad = [c for c in commands if c.split()[0] not in self.READ_TOOLS]
        if bad:
            return [_result(c, False, "text", None, "not a FortiGate read tool") for c in commands]
        token = cred.get("API_KEY") or cred.get("TOKEN")
        if not token:
            raise DeviceError("auth_failed", "FortiGate needs an API_TOKEN credential (API_KEY)")
        wd = self._workdir()
        try:
            port = dev.get("PORT") or 443
            env = {**_base_env(wd), "FORTIGATE_HOST": f"https://{dev['DEVICE_ADDR']}:{port}",
                   "FORTIGATE_API_TOKEN": token, "FORTINET_VERIFY_SSL": "1" if self.s.eapi_verify_tls else "0",
                   "FORTINET_AUDIT_LOG": str(wd / "audit.log")}
            calls = []
            for c in commands:
                parts = c.split()
                args = {"vdom": parts[1]} if len(parts) > 1 else {}
                calls.append((parts[0], args))
            res = self._call(env, calls, wd)
            env.clear()
            out = []
            for c, r in zip(commands, res):
                if isinstance(r, dict) and r.get("_error"):
                    out.append(_result(c, False, "text", None, f"fortinet server: {r['_error']}"))
                else:
                    out.append(_result(c, True, "json" if isinstance(r, (dict, list)) else "text", r))
            return out
        finally:
            del token
            shutil.rmtree(wd, ignore_errors=True)


def choose_delegated(server: str, platform: dict, s: Settings, factories: dict | None = None):
    """(adapter, None) or (None, reason) for a 'delegated:<server>' platform."""
    f = factories or {}
    if f.get(server):
        return f[server](), None
    spec = server_specs(s).get(server)
    pid = platform.get("ID")
    if spec is None:
        return None, f"{pid} reads go through the {server} MCP server, which RUDDER cannot run yet"
    if not installed(spec):
        return None, (f"{pid} reads need the {server} MCP server; install it on this host: "
                      f"scripts/install_delegated.sh {server}")
    if server == "pyats":
        if not platform.get("pyats"):
            return None, f"{pid} has no pyATS os name in the catalogue"
        return PyatsAdapter(s, spec, platform["pyats"]), None
    if server == "multivendor":
        if not platform.get("multivendor"):
            return None, f"{pid} has no multivendor platform name in the catalogue"
        return MultivendorAdapter(s, spec, platform["multivendor"]), None
    if server == "fortinet":
        return FortinetAdapter(s, spec), None
    return None, f"{pid}: the {server} adapter is not built yet"
