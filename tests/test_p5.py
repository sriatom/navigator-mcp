"""P5: delegated adapters (RUDDER as an MCP client of other MCP servers)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from conftest import SECRET
from rudder_devices import adapters
from rudder_devices.config import Settings
from rudder_devices.core import Rudder
from rudder_devices.delegated import (FortinetAdapter, Launcher, MultivendorAdapter, PyatsAdapter,
                                      choose_delegated, installed, server_specs)
from test_p3 import rig  # noqa: F401


class FakeLauncher:
    """Records what the child would get (env, files in the work folder), returns canned replies."""

    def __init__(self, replies):
        self.replies, self.env, self.calls, self.files = replies, None, None, {}

    def call(self, env, calls):
        self.env, self.calls = dict(env), list(calls)
        for k in ("MULTIVENDOR_INVENTORY_PATH", "PYATS_TESTBED_PATH"):
            if k in env:
                self.files[k] = Path(env[k]).read_text()
        self.workdir = Path(env["HOME"])
        return [self.replies(tool, args) for tool, args in calls]


def _s(tmp_path) -> Settings:
    return Settings(atlas_socket=tmp_path / "x.sock", token_file=tmp_path / "t", consumer="NAVI",
                    records_path=tmp_path / "rec", eapi_verify_tls=False, ssh_strict=False, known_hosts=None,
                    timeout=7, max_workers=2, max_inline_chars=60000, delegated={"HOME": str(tmp_path / "dlg")})


DEV = {"DEVICE_NAME": "R1", "DEVICE_ADDR": "10.0.0.1", "PORT": 22, "OS": "VYOS"}
CRED = {"USERNAME": "labuser", "PASSWORD": SECRET}


def test_multivendor_adapter(tmp_path):
    fl = FakeLauncher(lambda t, a: {"status": "ok", "output": f"out of {a['command']}"})
    ad = MultivendorAdapter(_s(tmp_path), {}, "vyos", launcher=fl)
    res = ad.run(DEV, CRED, ["show version", "show interfaces"])
    assert [r["output"] for r in res] == ["out of show version", "out of show interfaces"]
    assert fl.calls[0] == ("run_command", {"device": "R1", "command": "show version", "timeout_s": 7})
    inv = yaml.safe_load(fl.files["MULTIVENDOR_INVENTORY_PATH"])
    assert inv == {"R1": {"hostname": "10.0.0.1", "platform": "vyos", "credential_ref": "rudder"}}
    assert SECRET not in fl.files["MULTIVENDOR_INVENTORY_PATH"]               # file holds a reference only
    assert fl.env["MULTIVENDOR_RUDDER_PASSWORD"] == SECRET                   # the secret is env-only
    assert "MULTIVENDOR_WRITE_ENABLED" not in fl.env                          # read-only child
    assert not fl.workdir.exists()                                            # work folder removed


def test_multivendor_errors(tmp_path):
    ad = MultivendorAdapter(_s(tmp_path), {}, "vyos",
                            launcher=FakeLauncher(lambda t, a: {"status": "auth_failed"}))
    with pytest.raises(adapters.DeviceError) as e:
        ad.run(DEV, CRED, ["show version"])
    assert e.value.kind == "auth_failed"
    ad = MultivendorAdapter(_s(tmp_path), {}, "vyos",
                            launcher=FakeLauncher(lambda t, a: {"status": "refused", "reason": "denied by policy"}))
    r = ad.run(DEV, CRED, ["show version"])[0]
    assert not r["ok"] and "denied by policy" in r["error"]


def test_pyats_adapter(tmp_path):
    parsed = {"version": {"version": "17.9.4", "platform": "Catalyst L3 Switch"}}
    fl = FakeLauncher(lambda t, a: {"status": "completed", "output": parsed, "parsed": True}
                      if a["command"] == "show version" else {"status": "completed", "output": "raw text", "parsed": False})
    ad = PyatsAdapter(_s(tmp_path), {}, "iosxe", launcher=fl)
    res = ad.run({**DEV, "OS": "IOSXE"}, {**CRED, "ENABLE_PASSWORD": "en"}, ["show version", "show clock"])
    assert res[0]["format"] == "json" and res[0]["output"] == parsed
    assert res[1]["format"] == "text" and res[1]["output"] == "raw text"
    tb = fl.files["PYATS_TESTBED_PATH"]
    assert "%ENV{RUDDER_PASSWORD}" in tb and SECRET not in tb
    d = yaml.safe_load(tb)["devices"]["R1"]
    assert d["os"] == "iosxe" and d["connections"]["cli"] == {"protocol": "ssh", "ip": "10.0.0.1", "port": 22}
    assert fl.env["RUDDER_ENABLE"] == "en" and fl.env["PYATS_MCP_KEEP_ARTIFACTS"] == "0"
    # #121: one login per run, no config-mode init, no controller-mode probe on IOS-XE, own timeout
    assert d["custom"]["connection_args"] == {"init_config_commands": [], "operating_mode": True}
    assert fl.env["PYATS_MCP_CONN_CACHE_TTL"] == "600"
    assert fl.calls[0][1]["timeout"] == 90


def test_pyats_settings_defaults_and_overrides(tmp_path):
    from rudder_devices.delegated import pyats_settings
    s = _s(tmp_path)
    assert pyats_settings(s, "nxos")["args"] == {"init_config_commands": []}            # no iosxe extra
    s2 = Settings(**{**s.__dict__, "delegated": {"PYATS": {
        "TIMEOUT": 45, "CONN_CACHE_TTL": 0, "CONNECTION_ARGS": {"learn_hostname": False},
        "CONNECTION_ARGS_BY_OS": {"iosxe": {"operating_mode": False}}}}})
    c = pyats_settings(s2, "iosxe")
    assert c == {"timeout": 45, "cache_ttl": 0, "args": {"init_config_commands": [], "operating_mode": False,
                                                          "learn_hostname": False}}


def test_fortinet_adapter(tmp_path):
    fl = FakeLauncher(lambda t, a: {"tool": t, "args": a})
    ad = FortinetAdapter(_s(tmp_path), {}, launcher=fl)
    dev = {"DEVICE_NAME": "FW1", "DEVICE_ADDR": "10.0.0.9", "PORT": 8443}
    res = ad.run(dev, {"API_KEY": "tok-xyz"}, ["fgt_system_status", "fgt_get_routes dmz"])
    assert res[1]["output"] == {"tool": "fgt_get_routes", "args": {"vdom": "dmz"}}
    assert fl.env["FORTIGATE_HOST"] == "https://10.0.0.9:8443" and fl.env["FORTIGATE_API_TOKEN"] == "tok-xyz"
    with pytest.raises(adapters.DeviceError, match="API_TOKEN"):
        ad.run(dev, CRED, ["fgt_system_status"])
    assert not ad.run(dev, {"API_KEY": "t"}, ["fmg_install_package"])[0]["ok"]   # write tools never


def test_choose_delegated_reports_missing_install(tmp_path):
    s = _s(tmp_path)
    ad, why = choose_delegated("pyats", {"ID": "NXOS", "pyats": "nxos"}, s)
    assert ad is None and "install_delegated.sh pyats" in why
    assert not installed(server_specs(s)["multivendor"])
    ad, why = choose_delegated("aci", {"ID": "X"}, s)
    assert ad is None


def test_rudder_reads_cisco_through_pyats(rig):  # noqa: F811
    (rig["tmp"] / "catalog" / "COMMANDS.yml").write_text("CISCO_NXOS:\n  system: [show version]\n")
    (rig["vault"] / "INVENTORY_DC.yml").write_text("""\
INVENTORY:
- {DEVICE_ID: N1, DEVICE_NAME: N1, DEVICE_ADDR: 10.9.9.1, CREDENTIAL: FAB, VENDOR: CISCO, OS: NXOS,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
""")
    fl = FakeLauncher(lambda t, a: {"status": "completed", "output": {"platform": {"os": "NX-OS"}}, "parsed": True})
    r = Rudder(rig["s"], factories={"pyats": lambda: PyatsAdapter(rig["s"], {}, "nxos", launcher=fl)})
    out = r.read("DC", category="system")
    d = out["devices"][0]
    assert d["status"] == "ok" and d["adapter"] == "delegated:pyats"
    assert d["results"][0]["output"] == {"platform": {"os": "NX-OS"}}
    assert SECRET not in json.dumps(out)


def test_real_launcher_http_mode_localhost_and_stopped(tmp_path):
    import os
    script = Path(__file__).with_name("fake_delegated_server.py")
    spec = {"command": [sys.executable, "-u", str(script)], "cwd": str(script.parent), "transport": "http",
            "path": "/mcp", "host_env": "FAKE_HOST", "port_env": "FAKE_PORT"}
    home = tmp_path / "wd"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    out = Launcher(spec, 30).call(env, [("where", {})])[0]
    assert out["host"] == "127.0.0.1"
    try:
        os.kill(out["pid"], 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive                                            # the server was stopped after the call


def test_real_launcher_stdio_and_minimal_env(tmp_path, monkeypatch):
    """The real launcher against a stand-in MCP server: stdio works, the child sees its own
    inventory and credential, and nothing else from RUDDER's environment."""
    monkeypatch.setenv("RUDDER_TEST_PARENT_SECRET", "must-not-pass")
    script = Path(__file__).with_name("fake_delegated_server.py")
    spec = {"command": [sys.executable, "-u", str(script)], "cwd": str(script.parent)}
    ad = MultivendorAdapter(_s(tmp_path), spec, "vyos", launcher=Launcher(spec, 30))
    r = ad.run(DEV, CRED, ["show version"])[0]
    assert r["ok"], r
    assert r["output"] == ("inventory_exists=True user_set=True leaked_parent_var=False home_is_workdir=True")
