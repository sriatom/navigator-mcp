"""P7d: one front door. NAVIGATOR is the only server (15 tools); ATLAS and RUDDER run inside it.
Connection method per call (via=), side by side (compare), raw, debug (scrubbed), the lab any-show rule,
the stored ADAPTER default through the interview, render (references only), troubleshooting records,
the LOCKED inventory scope, and no secret in any output. No network, no devices."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from atlas_inventory import config as aconfig
from atlas_inventory import interview, render
from conftest import SECRET
from rudder_devices import adapters
from rudder_devices.core import Rudder, RudderError, resolve_commands
from test_p3 import EOS_JSON, FakeConn, FakeEapi, rig  # noqa: F401

TOOLS = {"inventory", "inventory_edit", "credential", "device", "adapter", "change", "troubleshoot", "project",
         "mode", "role", "qc", "lesson", "skill", "scrambler", "research", "local_model"}


# ============================================================================ RUDDER: via, raw, debug, compare
def test_via_names_and_effective_record():
    assert adapters.via_name("PyATS") == "pyats" and adapters.via_name("default") is None
    with pytest.raises(ValueError, match="unknown connection method"):
        adapters.via_name("telnet")
    eapi_rec = {"DEVICE_NAME": "Q", "CONN_TYPE": "EAPI", "PORT": 443}
    assert adapters.effective_dev(eapi_rec, "ssh")["PORT"] == 22
    assert adapters.effective_dev({**eapi_rec, "SSH_PORT": 2222}, "pyats")["PORT"] == 2222
    assert adapters.effective_dev({"CONN_TYPE": "SSH", "PORT": 22}, "eapi")["PORT"] == 443
    assert adapters.effective_dev(eapi_rec, None) is eapi_rec


def test_choose_by_via_and_record_adapter(tmp_path):
    from rudder_devices.config import Settings
    s = Settings(atlas_socket=tmp_path / "x", token_file=tmp_path / "t", consumer="NAVI", records_path=tmp_path,
                 eapi_verify_tls=False, ssh_strict=False, known_hosts=None, timeout=5, max_workers=1,
                 max_inline_chars=1000, delegated={"HOME": str(tmp_path / "none")})
    eos = {"ID": "EOS", "reads": "native:eapi", "netmiko": "arista_eos", "pyats": "eos"}
    xe = {"ID": "IOSXE", "reads": "delegated:pyats", "netmiko": "cisco_xe", "pyats": "iosxe"}
    assert isinstance(adapters.choose(eos, {"CONN_TYPE": "EAPI"}, s, via="ssh")[0], adapters.EosSshJson)
    assert isinstance(adapters.choose(eos, {"CONN_TYPE": "EAPI"}, s, via="netmiko")[0], adapters.NetmikoText)
    assert isinstance(adapters.choose(xe, {"CONN_TYPE": "SSH"}, s, via="ssh")[0], adapters.NetmikoText)
    ad, why = adapters.choose(xe, {"CONN_TYPE": "SSH"}, s)                       # default: pyATS, not installed
    assert ad is None and "install_delegated.sh pyats" in why
    assert isinstance(adapters.choose(xe, {"CONN_TYPE": "SSH", "ADAPTER": "netmiko"}, s)[0], adapters.NetmikoText)
    assert adapters.choose(xe, {}, s, via="eapi") == (None, "IOSXE has no eAPI (that is Arista only)")
    assert "unknown connection method" in adapters.choose(xe, {"ADAPTER": "carrier-pigeon"}, s)[1]


def test_lab_any_show_rule():
    allow = {"system": ["show version"]}
    with pytest.raises(RudderError, match="LAB_ANY_SHOW"):
        resolve_commands(allow, None, ["show ip route vrf MGMT"], "EOS")
    assert resolve_commands(allow, None, ["show  ip route vrf MGMT", "show version"], "EOS", any_show=True) == \
        ["show ip route vrf MGMT", "show version"]
    for bad in ["show run | bash ls", "show version; reload", "configure terminal", "show $(id)", "show log > x"]:
        with pytest.raises(RudderError):
            resolve_commands(allow, None, [bad], "EOS", any_show=True)


def test_read_via_raw_debug_and_scrub(rig):  # noqa: F811
    r = rig["rudder"]
    res = r.read("FAB", devices=["QALEAF1A"], commands=["show version"], via="ssh", debug=True)
    d = res["devices"][0]
    assert d["status"] == "ok" and d["adapter"] == "native:ssh-json" and d["seconds"] is not None
    assert any("SSH connect" in x for x in d["debug"])
    assert rig["conns"][-1][1].sent == ["show version | json"]
    raw = r.read("FAB", devices=["QALEAF1A"], commands=["show version"], raw=True)["devices"][0]
    assert raw["results"][0]["format"] == "text" and "debug" not in raw
    # a secret that ends up in a debug line is replaced before it leaves RUDDER
    from rudder_devices.core import _scrub
    assert _scrub([f"login with {SECRET} ok"], {"PASSWORD": SECRET}) == ["login with <REDACTED> ok"]
    with pytest.raises(RudderError, match="unknown connection method"):
        r.read("FAB", devices=["QALEAF1A"], commands=["show version"], via="telnet")


def test_compare_two_methods(rig):  # noqa: F811
    res = rig["rudder"].compare("FAB", "QALEAF1A", ["eapi", "ssh"], commands=["show version"])
    assert [m["via"] for m in res["methods"]] == ["eapi", "ssh"]
    assert all(m["status"] == "ok" for m in res["methods"])
    c = res["compare"][0]
    assert c["command"] == "show version" and c["same"] is True            # same JSON either way
    with pytest.raises(RudderError, match="at least two"):
        rig["rudder"].compare("FAB", "QALEAF1A", ["ssh"], commands=["show version"])


def test_credential_test_via(rig):  # noqa: F811
    t = rig["rudder"].credential_test("FAB", "QALEAF1A", via="ssh", debug=True)
    assert t["status"] == "ok" and t["adapter"] == "native:ssh-json" and "debug" in t


# ============================================================================ ATLAS: render, ADAPTER field
def test_render_all_formats_have_no_secret(vault, tmp_path):
    cfg = aconfig.load()
    tb = render.render(cfg, "PALAB", "pyats")
    text = next(iter(tb["files"].values()))
    doc = yaml.safe_load(text)
    assert doc["devices"]["CORE1"]["os"] == "eos" and "%ENV{NAVI_CRED_LAB_EOS_PASSWORD}" in text
    assert "OLD1" not in doc["devices"]                                          # disabled records stay out
    assert all(Path(p).stat().st_mode & 0o777 == 0o600 for p in tb["written"])
    ans = render.render(cfg, "PALAB", "ansible", save=False)
    a = yaml.safe_load(next(iter(ans["files"].values())))
    assert "cred_lab_panos" in a["all"]["children"] and "lookup('env', 'NAVI_CRED_LAB_PANOS_PASSWORD')" in \
        a["all"]["children"]["cred_lab_panos"]["vars"]["ansible_password"]
    anta = render.render(cfg, "FAB", "anta", save=False)
    assert list(anta["files"]) == ["FAB-anta-fab.yaml"] and "ANTA_PASSWORD" in anta["files"]["FAB-anta-fab.yaml"]
    mv = render.render(cfg, "PALAB", "multivendor", save=False)
    m = yaml.safe_load(next(iter(mv["files"].values())))
    assert m["CORE1"]["credential_ref"] == "LAB_EOS" and m["PA-EDGE"]["platform"]
    for r_ in (tb, ans, anta, mv):
        assert SECRET not in json.dumps(r_) and "labuser" not in json.dumps(r_)
    with pytest.raises(render.RenderError, match="format must be"):
        render.render(cfg, "PALAB", "excel")
    # a hand-written file with the same name is never overwritten
    p = Path(tb["written"][0])
    p.write_text("mine\n")
    with pytest.raises(render.RenderError, match="not written by ATLAS"):
        render.render(cfg, "PALAB", "pyats")


def test_adapter_field_through_bulk_edit(vault):
    cfg = aconfig.load()
    d = interview.start(cfg, "bulk_edit", host=False, inventory_name="FAB")
    _, probs = interview.answer(cfg, d["id"], {"MATCH": "DEVICE_NAME=QALEAF1A", "SET": "ADAPTER=telnet"}, host=False)
    assert probs and "ADAPTER must be one of" in " ".join(probs)
    _, probs = interview.answer(cfg, d["id"], {"MATCH": "DEVICE_NAME=QALEAF1A", "SET": "ADAPTER=ssh"}, host=False)
    assert not probs
    pv = interview.preview(cfg, d["id"], host=False)
    assert pv["errors"] == [] and pv["commit_code"]
    interview.commit(cfg, d["id"], pv["commit_code"], host=False)
    doc = yaml.safe_load((vault / "INVENTORY_FAB.yml").read_text())
    leaf = next(r for r in doc["INVENTORY"] if r["DEVICE_NAME"] == "QALEAF1A")
    assert leaf["ADAPTER"] == "ssh"
    assert "PDLEAF1A" not in [r["DEVICE_NAME"] for r in doc["INVENTORY"] if r.get("ADAPTER")]
    d = interview.start(cfg, "bulk_edit", host=False, inventory_name="FAB")         # back to the default
    interview.answer(cfg, d["id"], {"MATCH": "DEVICE_NAME=QALEAF1A", "SET": "ADAPTER=default"}, host=False)
    interview.commit(cfg, d["id"], interview.preview(cfg, d["id"], host=False)["commit_code"], host=False)
    doc = yaml.safe_load((vault / "INVENTORY_FAB.yml").read_text())
    assert "ADAPTER" not in next(r for r in doc["INVENTORY"] if r["DEVICE_NAME"] == "QALEAF1A")


# ============================================================================ the front door server
@pytest.fixture()
def front(rig, monkeypatch, tmp_path):  # noqa: F811
    ws = tmp_path / "a projects folder"
    (ws / "ADHOC_WORK").mkdir(parents=True)
    nboot = tmp_path / "navigator.yml"
    nboot.write_text(f"WORKSPACE: {ws}\nSTATE_PATH: {tmp_path / 'nst'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nboot))
    rboot = tmp_path / "rudder.yml"
    rboot.write_text(yaml.safe_dump({"ATLAS_SOCKET": str(rig["sock"]), "TOKEN_FILE": str(rig["tok"]),
                                     "CONSUMER": "NAVI", "RECORDS_PATH": str(tmp_path / "rrec"),
                                     "STATE_PATH": str(tmp_path / "rst"), "TIMEOUT": 5,
                                     "LAB_INVENTORIES": ["FAB"], "DELEGATED": {"HOME": str(tmp_path / "nd")}}))
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(rboot))
    eapi = rig["eapi"]
    real_init = Rudder.__init__

    def init(self, s, atlas=None, factories=None):          # the front door builds its own Rudder: give it fakes
        real_init(self, s, atlas, factories or {
            "eapi": lambda: adapters.EosEapi(s, client=lambda **kw: httpx.Client(transport=httpx.MockTransport(eapi), **kw)),
            "ssh-json": lambda: adapters.EosSshJson(s, connect=lambda d, c, t, st, log=None: FakeConn(
                {"show version | json": json.dumps(EOS_JSON["show version"]), "show version": "Arista cEOSLab 4.32.1F"}))})
    monkeypatch.setattr(Rudder, "__init__", init)
    from navigator_engineer.server import mcp
    return {"mcp": mcp, "ws": ws, "tmp": tmp_path}


async def _call(c, tool, **args):
    r = await c.call_tool(tool, args)
    return json.loads(r.content[0].text) if r.content and r.content[0].text[:1] in "[{" else r.content[0].text


async def test_one_server_fifteen_tools(front):
    async with Client(front["mcp"]) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == TOOLS


async def test_front_door_reads_records_and_locked_scope(front):
    async with Client(front["mcp"]) as c:
        assert "FAB" in await _call(c, "inventory", action="list")
        await _call(c, "project", action="create", mode="LOCKED", description="fab check", project="LK",
                    inventory="FAB")
        res = await _call(c, "device", action="read", project="LK", devices=["QALEAF1A"], commands=["show version"])
        assert res["devices"][0]["status"] == "ok"
        assert "/a projects folder/LK/RECORDS/DEVICES" in res["records"].replace("\\", "/")
        assert "device read in FAB" in (front["ws"] / "LK" / "RUNLOG.md").read_text()
        with pytest.raises(ToolError, match="works on inventory FAB"):
            await _call(c, "device", action="read", project="LK", inventory="PALAB", devices=["CORE1"],
                        commands=["show version"])
        with pytest.raises(ToolError, match="action must be one of"):
            await _call(c, "device", action="write", project="LK")
        cmp = await _call(c, "device", action="compare", project="LK", devices=["QALEAF1A"],
                          commands=["show version"], via="eapi,ssh")
        assert [m["via"] for m in cmp["methods"]] == ["eapi", "ssh"]


async def test_troubleshoot_session_records_calls(front):
    async with Client(front["mcp"]) as c:
        await _call(c, "project", action="create", mode="OPEN", description="ts", project="TS", inventory="FAB")
        st = await _call(c, "troubleshoot", action="start", project="TS", title="ssh reads look odd")
        res = await _call(c, "device", action="read", project="TS", devices=["QALEAF1A"],
                          commands=["show version"], via="ssh", raw=True, debug=True)
        assert res["troubleshoot"].startswith("recorded")
        await _call(c, "troubleshoot", action="note", project="TS", text="raw text came back")
        shown = await _call(c, "troubleshoot", action="show", project="TS")
        assert shown["steps"] == 2 and "show version" in shown["text"]
        done = await _call(c, "troubleshoot", action="stop", project="TS", summary="fine")
        text = Path(done["file"]).read_text()
        assert "/RECORDS/TROUBLESHOOT/" in done["file"].replace("\\", "/")
        assert "adapter native:ssh-json" in text and "Summary" in text and "Debug log" in text
        assert SECRET not in text
        with pytest.raises(ToolError, match="no open troubleshooting session"):
            await _call(c, "troubleshoot", action="stop", project="TS")


async def test_render_adapter_default_and_no_secret_anywhere(front):
    outs = []
    async with Client(front["mcp"]) as c:
        outs.append(await _call(c, "inventory", action="render", name="PALAB", format="pyats"))
        outs.append(await _call(c, "credential", action="list"))
        only_fab = await _call(c, "credential", action="list", inventory="FAB")     # A5: just this inventory's tags
        assert only_fab.startswith("FAB:") and "LAB_PANOS" not in only_fab
        assert "LAB_PANOS" in await _call(c, "credential", action="list")
        outs.append(await _call(c, "credential", action="test", inventory="FAB", device="QALEAF1A", via="ssh",
                                debug=True))
        r = await _call(c, "adapter", action="default", inventory="FAB", devices=["QALEAF1A"], via="ssh")
        assert r["draft"] and r["preview"]["commit_code"]
        outs.append(r)
        outs.append(await _call(c, "adapter", action="list"))
        brief = await _call(c, "role", action="brief", role="SHIP_ENGINEER", task="pyATS reads time out on EDGE1")
        assert "troubleshoot action=start" in brief["brief"] and "Tool names" in brief["brief"]
        assert any(x["role"] == "SHIP_ENGINEER" for x in await _call(c, "role", action="list"))
    blob = json.dumps(outs)
    assert SECRET not in blob and "labuser" not in blob
