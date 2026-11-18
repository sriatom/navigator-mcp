"""P3: the ATLAS link, RUDDER's read path, native EOS adapters (fake eAPI / fake SSH), records."""
from __future__ import annotations

import base64
import json
import os
import socket
import stat

import httpx
import pytest
from fastmcp import Client

from atlas_inventory import link
from conftest import SECRET, write_boot
from rudder_devices import adapters
from rudder_devices.atlas import Atlas, AtlasError
from rudder_devices.config import Settings
from rudder_devices.core import Rudder, RudderError, resolve_commands

COMMANDS = """\
ARISTA_EOS:
  system: [show version, show hostname, show running-config]
  bgp: [show ip bgp summary]
PA_PANOS:
  system: [show system info]
"""

EOS_JSON = {
    "show version": {"modelName": "cEOSLab", "version": "4.32.1F", "serialNumber": "SN123",
                     "systemMacAddress": "00:1c:73:00:00:01", "uptime": 1234.5},
    "show hostname": {"hostname": "qaleaf1a", "fqdn": "qaleaf1a.lab"},
    "show ip bgp summary": {"vrfs": {"default": {"peers": {"10.0.0.1": {"peerState": "Established"}}}}},
}


class FakeEapi:
    """Arista eAPI JSON-RPC: right credential -> results; running-config has no JSON form."""

    def __init__(self, password=SECRET):
        self.password = password
        self.calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        user, pw = base64.b64decode(req.headers["Authorization"].split()[1]).decode().split(":", 1)
        if pw != self.password:
            return httpx.Response(401)
        body = json.loads(req.content)
        cmds, fmt = body["params"]["cmds"], body["params"]["format"]
        res = []
        for c in cmds:
            if isinstance(c, dict) or c == "enable":
                res.append({})
            elif c == "show running-config":
                if fmt == "json":
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "rudder", "error": {
                        "code": 1003, "message": "CLI command 1 of 1 'show running-config' failed: "
                                                 "could not convert to JSON"}})
                res.append({"output": "hostname qaleaf1a\nusername admin secret sha512 $6$x\n"})
            elif c in EOS_JSON:
                res.append(EOS_JSON[c] if fmt == "json" else {"output": "text"})
            else:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": "rudder",
                                                 "error": {"code": 1002, "message": "invalid command"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "rudder", "result": res})


class FakeConn:
    def __init__(self, replies):
        self.replies, self.sent, self.closed = replies, [], False

    def send_command(self, cmd, read_timeout=None):
        self.sent.append(cmd)
        return self.replies.get(cmd, "% Invalid input detected")

    def disconnect(self):
        self.closed = True


@pytest.fixture()
def rig(vault, tmp_path):
    tok = tmp_path / "keys" / "rudder.token"
    sock = tmp_path / "s" / "link.sock"
    write_boot(tmp_path, f"""\
        LINK:
          SOCKET: {sock}
          CLIENTS:
            RUDDER: {{TOKEN_FILE: {tok}, CONSUMERS: [NAVI]}}
        """)
    (tmp_path / "catalog" / "COMMANDS.yml").write_text(COMMANDS)
    link.make_token(tok)
    srv = link.serve_in_thread(sock)
    s = Settings(atlas_socket=sock, token_file=tok, consumer="NAVI", records_path=tmp_path / "records",
                 eapi_verify_tls=False, ssh_strict=False, known_hosts=None, timeout=5, max_workers=4,
                 max_inline_chars=60000, delegated={"HOME": str(tmp_path / "no-delegated")})
    eapi = FakeEapi()
    conns = []

    def ssh_connect(dev, cred, device_type, st):
        assert cred.get("PASSWORD") == SECRET
        c = FakeConn({"show version | json": json.dumps(EOS_JSON["show version"]),
                      "show hostname | json": "% This is an unconverted command",
                      "show hostname": "Hostname: core1",
                      "show system info": "hostname: PA-EDGE\nsw-version: 11.1.0"})
        conns.append((device_type, c))
        return c

    factories = {"eapi": lambda: adapters.EosEapi(s, client=lambda **kw: httpx.Client(
                     transport=httpx.MockTransport(eapi), **kw)),
                 "ssh-json": lambda: adapters.EosSshJson(s, connect=ssh_connect),
                 "netmiko-text": lambda: adapters.NetmikoText(s, "paloalto_panos", connect=ssh_connect)}
    yield {"s": s, "rudder": Rudder(s, factories=factories), "eapi": eapi, "conns": conns, "sock": sock,
           "tok": tok, "vault": vault, "tmp": tmp_path}
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- ATLAS link
def test_link_auth_and_consumers(rig):
    a = Atlas(rig["s"])
    assert a.call("hello") == {"ok": True, "client": "RUDDER", "consumers": ["NAVI"]}
    assert stat.S_IMODE(os.stat(rig["sock"]).st_mode) == 0o600
    assert a.credential("FAB") == {"USERNAME": "labuser", "PASSWORD": SECRET}
    other = Settings(**{**rig["s"].__dict__, "consumer": "LAB_BOT"})
    with pytest.raises(AtlasError, match="may not act for consumer LAB_BOT"):
        Atlas(other).credential("FAB")
    bad = rig["tmp"] / "bad.token"
    bad.write_text("not-the-token\n")
    with pytest.raises(AtlasError, match="unknown token"):
        Atlas(Settings(**{**rig["s"].__dict__, "token_file": bad})).call("hello")


def test_link_records_and_catalogue(rig):
    a = Atlas(rig["s"])
    recs = a.records("FAB")
    assert {r["DEVICE_NAME"] for r in recs} == {"QALEAF1A", "PDLEAF1A"}
    assert [r["PRODUCTION"] for r in recs] == [False, True]
    assert a.platform("arista_eos")["ID"] == "EOS"
    assert a.commands("EOS")["bgp"] == ["show ip bgp summary"]


def test_link_masks_tags_the_consumer_may_not_use(rig):
    write_boot(rig["tmp"], f"""\
        LINK:
          SOCKET: {rig['sock']}
          CLIENTS:
            RUDDER: {{TOKEN_FILE: {rig['tok']}, CONSUMERS: [NAVI]}}
        CONSUMERS:
          NAVI: {{INVENTORIES: ["*"], CREDENTIALS: [LAB_PANOS]}}
        """)
    a = Atlas(rig["s"])
    assert {r["CREDENTIAL"] for r in a.records("FAB")} == {None}
    with pytest.raises(AtlasError, match="may not use credential FAB"):
        a.credential("FAB")


# --------------------------------------------------------------------------- allowlist
def test_allowlist():
    allow = {"system": ["show version", "show hostname"], "bgp": ["show ip bgp summary"]}
    assert resolve_commands(allow, "BGP", ["show  VERSION"], "EOS") == ["show ip bgp summary", "show version"]
    with pytest.raises(RudderError, match="not on the EOS command list: conf t"):
        resolve_commands(allow, None, ["conf t"], "EOS")
    with pytest.raises(RudderError, match="no category"):
        resolve_commands(allow, "nope", None, "EOS")
    with pytest.raises(RudderError, match="no command list"):
        resolve_commands({}, "system", None, "PANOS")


# --------------------------------------------------------------------------- reads
def test_eapi_read_json_text_fallback_and_records(rig):
    r = rig["rudder"].read("FAB", devices=["qaleaf1a"],
                           commands=["show version", "show running-config", "show ip bgp summary"])
    assert r["counts"] == {"ok": 1}
    d = r["devices"][0]
    assert d["adapter"] == "native:eapi" and d["platform"] == "EOS"
    by = {x["command"]: x for x in d["results"]}
    assert by["show version"]["format"] == "json" and by["show version"]["output"]["version"] == "4.32.1F"
    assert by["show running-config"]["format"] == "text" and "hostname qaleaf1a" in by["show running-config"]["output"]
    for x in d["results"]:
        p = x["record"]
        assert os.path.isfile(p) and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    run = json.loads(open(r["run_file"]).read())
    assert run["devices"]["QALEAF1A"]["status"] == "ok" and run["consumer"] == "NAVI"
    blob = json.dumps(r) + "".join(open(x["record"]).read() for x in d["results"])
    assert SECRET not in blob


def test_category_on_a_group(rig):
    r = rig["rudder"].read("FAB", groups=["LEAF"], category="bgp")
    assert r["counts"] == {"ok": 2}
    assert all(x["results"][0]["output"]["vrfs"] for x in r["devices"])


def test_auth_failure_is_reported_not_leaked(rig):
    rig["eapi"].password = "something-else"
    r = rig["rudder"].read("FAB", devices=["QALEAF1A"], category="system")
    d = r["devices"][0]
    assert d["status"] == "auth_failed" and "401" in d["error"] and SECRET not in json.dumps(r)


def test_ssh_json_and_netmiko_text_and_missing_credential(rig):
    # PALAB: PA-EDGE (PANOS over SSH, text) and CORE1 (EOS over SSH; its tag LAB_EOS does not exist)
    (rig["vault"] / "CREDENTIALS.yml").write_text(
        (rig["vault"] / "CREDENTIALS.yml").read_text() + f"- {{TAG: LAB_EOS, USERNAME: x, PASSWORD: {SECRET}}}\n")
    r = rig["rudder"].read("PALAB", category="system")
    by = {d["device"]: d for d in r["devices"]}
    core = {x["command"]: x for x in by["CORE1"]["results"]}
    assert by["CORE1"]["adapter"] == "native:ssh-json"
    assert core["show version"]["format"] == "json" and core["show version"]["output"]["modelName"] == "cEOSLab"
    assert core["show hostname"]["format"] == "text" and core["show hostname"]["output"] == "Hostname: core1"
    assert not core["show running-config"]["ok"]                     # fake says "% Invalid input"
    pa = by["PA-EDGE"]
    assert pa["adapter"] == "native:netmiko-text" and pa["results"][0]["output"].startswith("hostname: PA-EDGE")
    assert all(c.closed for _, c in rig["conns"])


def test_delegated_and_unknown_are_skipped_with_reason(rig):
    (rig["vault"] / "INVENTORY_MIXED.yml").write_text("""\
INVENTORY:
- {DEVICE_ID: N1, DEVICE_NAME: N1, DEVICE_ADDR: 10.9.9.1, CREDENTIAL: FAB, VENDOR: CISCO, OS: NXOS,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
- {DEVICE_ID: V1, DEVICE_NAME: V1, DEVICE_ADDR: 10.9.9.2, CREDENTIAL: FAB, VENDOR: HP, OS: HP_COMWARE,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
""")
    r = rig["rudder"].read("MIXED", category="system")
    by = {d["device"]: d for d in r["devices"]}
    assert by["N1"]["status"] == "skipped" and "install_delegated.sh pyats" in by["N1"]["reason"]
    assert by["V1"]["status"] == "skipped" and "no command list" in by["V1"]["reason"]
    assert rig["eapi"].calls == 0


def test_unknown_device_and_command_refused(rig):
    with pytest.raises(RudderError, match="not found"):
        rig["rudder"].read("FAB", devices=["GHOST"], category="system")
    r = rig["rudder"].read("FAB", devices=["QALEAF1A"], commands=["configure terminal"])
    assert r["devices"][0]["status"] == "skipped" and "not on the EOS command list" in r["devices"][0]["reason"]
    assert rig["eapi"].calls == 0


def test_credential_test_and_facts(rig):
    assert rig["rudder"].credential_test("FAB", "QALEAF1A")["status"] == "ok"
    f = rig["rudder"].facts("FAB", "QALEAF1A")
    assert (f["hostname"], f["model"], f["version"], f["serial"]) == ("qaleaf1a", "cEOSLab", "4.32.1F", "SN123")
    rig["eapi"].password = "nope"
    t = rig["rudder"].credential_test("FAB", "PDLEAF1A")
    assert t["status"] == "auth_failed" and SECRET not in json.dumps(t)


def test_link_down_is_clear(rig, tmp_path):
    s = Settings(**{**rig["s"].__dict__, "atlas_socket": tmp_path / "nothing.sock"})
    with pytest.raises(RudderError, match="ATLAS link is not running"):
        Rudder(s).read("FAB", category="system")


# --------------------------------------------------------------------------- MCP surface
async def test_rudder_mcp_tools(rig, monkeypatch, tmp_path):
    from rudder_devices.server import mcp
    boot = tmp_path / "rudder.yml"
    boot.write_text(f"ATLAS_SOCKET: {rig['sock']}\nTOKEN_FILE: {rig['tok']}\nCONSUMER: NAVI\n"
                    f"RECORDS_PATH: {tmp_path / 'rec'}\nDELEGATED: {{HOME: {tmp_path / 'nodlg'}}}\n")
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(boot))
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"device_read", "credential_test", "device_facts", "adapter_list"} <= names
        out = (await c.call_tool("adapter_list", {})).content[0].text
        assert "EOS" in out and "ready (eAPI" in out and "NXOS" in out and "install_delegated.sh pyats" in out
