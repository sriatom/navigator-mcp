"""P2: versions, the interview (engine, MCP tools), NetBox export."""
from __future__ import annotations

import json

import httpx
import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from atlas_inventory import config, credentials, interview as iv, inventory, netbox, store, vault
from atlas_inventory.server import mcp
from conftest import SECRET, write_boot


def cfg():
    return config.load()


def run(topic, answers=None, host=True, **kw):
    """start + answer + preview; returns (draft id, preview)."""
    d = iv.start(cfg(), topic, host=host, **kw)
    if answers:
        _, probs = iv.answer(cfg(), d["id"], answers, host=host)
        assert not probs, probs
    return d["id"], iv.preview(cfg(), d["id"], host=host)


DEV = {"DEVICE_NAME": "LEAF9", "DEVICE_ADDR": "198.19.0.99", "OS": "arista_eos", "CREDENTIAL": "FAB",
       "PRODUCTION": "no", "ROLE": "LEAF"}


# --------------------------------------------------------------------------- engine
def test_inventory_new_then_add_device(vault):
    did, pv = run("inventory_new", {"NAME": "lab2", "SITE": "HQ", "PRODUCTION": "no"})
    assert pv["errors"] == [] and pv["commit_code"]
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    path = vault / "INVENTORY_LAB2.yml"
    doc = yaml.safe_load(path.read_text())
    assert doc["META"]["DEFAULTS"] == {"SITE": "HQ", "PRODUCTION": False} and doc["INVENTORY"] == []
    assert inventory.validate_text(path.read_text(), {"FAB"}).ok        # empty but META: fine

    did, pv = run("record_add", {**DEV, "DEVICE_ADDR": "10.1.1.1"}, inventory_name="LAB2")
    assert "add device LEAF9" in pv["summary"] and "+- DEVICE_NAME: LEAF9" in pv["diff"]
    out = iv.commit(cfg(), did, pv["commit_code"], host=True)
    assert any("previous version kept" in o for o in out)
    rec = yaml.safe_load(path.read_text())["INVENTORY"][0]
    assert rec["OS"] == "EOS" and rec["VENDOR"] == "ARISTA" and rec["CONN_TYPE"] == "EAPI"
    assert rec["PORT"] == 443 and rec["DEVICE_ID"] == "LEAF9" and rec["IS_ENABLED"] is True
    assert rec["TAGS"] == {"SITE": "HQ", "ROLE": ["LEAF"]} and rec["PRODUCTION"] is False
    assert len(store.list_versions(cfg(), path)) == 1                # the empty first version
    audit = (cfg().state_path / "audit.log").read_text()
    assert "interview:inventory_new" in audit and "interview:record_add" in audit


def test_answers_are_checked(vault):
    d = iv.start(cfg(), "record_add", host=True, inventory_name="FAB")
    _, probs = iv.answer(cfg(), d["id"], {"DEVICE_NAME": "qaleaf1a", "DEVICE_ADDR": "198.19.0.21",
                                          "OS": "WIDGET-OS", "PORT": "99999", "CREDENTIAL": "NOPE",
                                          "COLOR": "blue"}, host=True)
    text = "\n".join(probs)
    assert "qaleaf1a already exists in INVENTORY_FAB" in text
    assert "already used by PDLEAF1A" in text
    assert "unknown platform" in text and "port is a number" in text
    assert "NOPE does not exist" in text and "COLOR: not a question" in text


def test_conn_type_must_fit_platform(vault):
    d = iv.start(cfg(), "record_add", host=True, inventory_name="FAB")
    _, probs = iv.answer(cfg(), d["id"], {"OS": "PANOS", "CONN_TYPE": "VMANAGE_API"}, host=True)
    assert any("not a connection type for this platform" in p for p in probs)


def test_secrets_refused_in_chat(vault):
    d = iv.start(cfg(), "credential_new", host=False)
    _, probs = iv.answer(cfg(), d["id"], {"TAG": "NEWCRED", "LOGIN_PASSWORD": SECRET, "api_key": SECRET},
                         host=False)
    assert len([p for p in probs if "never given in chat" in p]) == 2
    st = iv.status(cfg(), d["id"], host=False)
    assert SECRET not in json.dumps(st) and SECRET not in (cfg().state_path / "drafts" / f"{d['id']}.yml").read_text()
    assert any("LOGIN_PASSWORD" in w and "atlas-interview secret" in w for w in st["waiting_for"])


def test_commit_needs_matching_code_and_cancel_writes_nothing(vault):
    before = (vault / "INVENTORY_FAB.yml").read_text()
    did, pv = run("record_add", DEV, inventory_name="FAB")
    with pytest.raises(iv.InterviewError, match="does not match"):
        iv.commit(cfg(), did, "ZZZZZZ", host=True)
    iv.answer(cfg(), did, {"DEVICE_ADDR": "198.19.0.98"}, host=True)       # changed after preview
    with pytest.raises(iv.InterviewError):
        iv.commit(cfg(), did, pv["commit_code"], host=True)
    iv.cancel(cfg(), did)
    assert (vault / "INVENTORY_FAB.yml").read_text() == before
    with pytest.raises(iv.InterviewError, match="no draft"):
        iv.status(cfg(), did, host=True)


def test_stale_file_refused(vault):
    did, pv = run("record_add", DEV, inventory_name="FAB")
    p = vault / "INVENTORY_FAB.yml"
    p.write_text(p.read_text() + "\n# edited by hand\n")
    with pytest.raises((iv.InterviewError, store.StoreError), match="changed"):
        iv.commit(cfg(), did, pv["commit_code"], host=True)


def test_edit_remove_and_restore(vault):
    p = vault / "INVENTORY_FAB.yml"
    original = p.read_text()
    did, pv = run("record_edit", {"PORT": 8443}, inventory_name="FAB", name="qaleaf1a")
    assert "edit QALEAF1A: PORT" in pv["summary"]
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    recs = {r["DEVICE_NAME"]: r for r in yaml.safe_load(p.read_text())["INVENTORY"]}
    assert recs["QALEAF1A"]["PORT"] == 8443 and recs["QALEAF1A"]["TAGS"]["ROLE"] == ["LEAF"]   # rest kept

    d = iv.start(cfg(), "record_remove", host=True, inventory_name="FAB", name="PDLEAF1A")
    _, probs = iv.answer(cfg(), d["id"], {"HARD": "yes"}, host=True)
    assert any("/pushok" in x for x in probs)
    pv = iv.preview(cfg(), d["id"], host=True)
    assert "disable PDLEAF1A (soft delete)" in pv["summary"]
    iv.commit(cfg(), d["id"], pv["commit_code"], host=True)
    recs = {r["DEVICE_NAME"]: r for r in yaml.safe_load(p.read_text())["INVENTORY"]}
    assert recs["PDLEAF1A"]["IS_ENABLED"] is False

    versions = store.list_versions(cfg(), p)
    assert len(versions) == 2
    oldest = versions[-1].stem
    did, pv = run("inventory_restore", {"VERSION": oldest}, inventory_name="FAB")
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    assert yaml.safe_load(p.read_text()) == yaml.safe_load(original)


def test_bulk_import_csv(vault):
    data = ("DEVICE_NAME,DEVICE_ADDR,OS,CREDENTIAL,PRODUCTION,SITE,ROLE\n"
            "B1,10.2.0.1,nxos,FAB,no,DC1,\"SPINE\"\n"
            "B2,10.2.0.2,cisco_xe,FAB,no,DC1,EDGE\n")
    did, pv = run("bulk_import", {"FORMAT": "csv", "DATA": data}, inventory_name="FAB")
    assert pv["errors"] == [] and "import 2 device record(s)" in pv["summary"]
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    recs = {r["DEVICE_NAME"]: r for r in yaml.safe_load((vault / "INVENTORY_FAB.yml").read_text())["INVENTORY"]}
    assert recs["B1"]["OS"] == "NXOS" and recs["B1"]["CONN_TYPE"] == "SSH" and recs["B1"]["PORT"] == 22
    assert recs["B2"]["OS"] == "IOSXE"
    bad = "DEVICE_NAME,DEVICE_ADDR,OS,CREDENTIAL,PRODUCTION,PASSWORD\nX1,10.3.0.1,EOS,FAB,no,hunter2\n"
    did, pv = run("bulk_import", {"FORMAT": "csv", "DATA": bad}, inventory_name="FAB")
    assert any("looks like a secret" in e for e in pv["errors"]) and pv["commit_code"] is None
    assert "hunter2" not in json.dumps(pv)


def test_consumer_access_host_only(vault):
    with pytest.raises(iv.InterviewError, match="only on the host"):
        iv.start(cfg(), "consumer_access", host=False, consumer="LAB_BOT")
    did, pv = run("consumer_access", {"INVENTORIES": "INVENTORY_FAB", "CREDENTIALS": "FAB"}, consumer="LAB_BOT")
    assert any("every consumer may use everything" in w for w in pv["warnings"])
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    assert cfg().consumers["LAB_BOT"] == {"INVENTORIES": ["INVENTORY_FAB"], "CREDENTIALS": ["FAB"]}


def test_credential_new_needs_key_and_stores_encrypted(vault):
    d = iv.start(cfg(), "credential_new", host=True)
    iv.answer(cfg(), d["id"], {"TAG": "LAB_EOS", "TYPE": "LOGIN", "USERNAME": "labadmin"}, host=True)
    with pytest.raises(iv.InterviewError, match="atlas-vault init"):
        iv.set_secret(cfg(), d["id"], "LOGIN_PASSWORD", SECRET)
    vault_mod_create_key()
    iv.set_secret(cfg(), d["id"], "LOGIN_PASSWORD", SECRET)
    st = iv.status(cfg(), d["id"], host=True)
    assert st["ready_for_preview"] and "labadmin" not in json.dumps(st)
    pv = iv.preview(cfg(), d["id"], host=True)
    assert SECRET not in json.dumps(pv) and "labadmin" not in json.dumps(pv)
    out = iv.commit(cfg(), d["id"], pv["commit_code"], host=True)
    assert any(".bak-" in o for o in out)
    text = (vault / "CREDENTIALS.yml").read_text()
    assert SECRET not in text.split("LAB_EOS")[1] and "labadmin" not in text
    got = vault_get("LAB_EOS")
    assert got == {"USERNAME": "labadmin", "LOGIN_PASSWORD": SECRET}
    assert not store.versions_dir(cfg(), vault / "CREDENTIALS.yml").exists()   # no copies in STATE_PATH
    # PALAB now validates: the missing tag exists
    assert inventory.validate_text((vault / "INVENTORY_PALAB.yml").read_text(),
                                   credentials.tags(vault / "CREDENTIALS.yml")).ok


def test_credential_remove_in_use_refused(vault):
    d = iv.start(cfg(), "credential_remove", host=True, tag="FAB")
    pv = iv.preview(cfg(), d["id"], host=True)
    assert any("FAB is used by INVENTORY_FAB/QALEAF1A" in e for e in pv["errors"])
    assert pv["commit_code"] is None


def test_setup_topic_is_later(vault):
    with pytest.raises(iv.InterviewError, match="NAVIGATOR"):
        iv.start(cfg(), "setup", host=True)


def vault_mod_create_key():
    vault.create_key(cfg())


def vault_get(tag):
    return dict(vault.get_credential(cfg(), tag))


HAND = """\
# Lab fabric, kept by hand
INVENTORY:
  -
    DEVICE_ID: SPINE1
    DEVICE_NAME: 'SPINE1'          # quoted on purpose
    DEVICE_ADDR: '198.19.0.1'
    CREDENTIAL: FAB
    VENDOR: ARISTA
    OS: ARISTA_EOS
    CONN_TYPE: EAPI
    PORT: 443
    IS_ENABLED: true
    TAGS:
      SITE: QA
      ROLE:
        - SPINE
  -
    DEVICE_ID: SPINE2
    DEVICE_NAME: 'SPINE2'
    DEVICE_ADDR: '198.19.0.2'
    CREDENTIAL: FAB
    VENDOR: ARISTA
    OS: ARISTA_EOS
    CONN_TYPE: EAPI
    PORT: 443
    IS_ENABLED: true
# end of file comment
"""


def _changed(a: str, b: str) -> list[str]:
    import difflib
    return [x for x in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
            if x[:1] in "+-" and not x.startswith(("+++", "---"))]


def test_hand_written_file_keeps_its_format(vault):
    p = vault / "INVENTORY_HAND.yml"
    p.write_text(HAND)
    did, pv = run("record_add", {**DEV, "DEVICE_ADDR": "198.19.0.3"}, inventory_name="HAND")
    assert pv["warnings"] == [] or all("rewritten" not in w for w in pv["warnings"])
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    after = p.read_text()
    assert all(x.startswith("+") for x in _changed(HAND, after))          # only added lines
    assert "# quoted on purpose" in after and "# end of file comment" in after
    assert "  -\n    DEVICE_NAME: LEAF9" in after                         # the file's own item style
    did, pv = run("record_edit", {"PORT": 8443, "SITE": "QB"}, inventory_name="HAND", name="SPINE1")
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    after2 = p.read_text()
    ch = _changed(after, after2)
    assert len([x for x in ch if x.startswith("-")]) <= 5 and "    PORT: 8443" in after2
    assert "DEVICE_NAME: 'SPINE1'          # quoted on purpose" in after2
    did, pv = run("record_remove", inventory_name="HAND", name="SPINE2")
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    ch = _changed(after2, p.read_text())
    assert ch == ["-    IS_ENABLED: true", "+    IS_ENABLED: false"]


def test_one_line_records_keep_their_format(vault):
    """conftest's INV_FAB uses one-line `- {...}` records (like the real VIPTELA61)."""
    p = vault / "INVENTORY_FAB.yml"
    before = p.read_text()
    did, pv = run("bulk_edit", {"MATCH": "PLATFORM=EOS", "SET": "PORT=8443"}, inventory_name="FAB")
    assert pv["warnings"] == []
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    after = p.read_text()
    ch = _changed(before, after)
    assert len(ch) == 4 and all("PORT: 8443" in x for x in ch if x.startswith("+"))   # 2 lines out, 2 in
    did, pv = run("record_add", {**DEV, "DEVICE_ADDR": "198.19.0.77"}, inventory_name="FAB")
    assert pv["warnings"] == []
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    added = _changed(after, p.read_text())
    assert len(added) == 1 and added[0].startswith("+- {DEVICE_NAME: LEAF9")


def test_bulk_edit_keeps_format_and_only_touches_matches(vault):
    p = vault / "INVENTORY_HAND.yml"
    p.write_text(HAND.replace("CREDENTIAL: FAB\n    VENDOR: ARISTA\n    OS: ARISTA_EOS\n    CONN_TYPE: EAPI\n    PORT: 443\n"
                              "    IS_ENABLED: true\n# end",
                              "CREDENTIAL: FAB\n    VENDOR: CISCO\n    OS: IOSXE\n    CONN_TYPE: SSH\n    PORT: 22\n"
                              "    IS_ENABLED: true\n# end"))
    (vault / "CREDENTIALS.yml").write_text((vault / "CREDENTIALS.yml").read_text()
                                           + "- {TAG: NEWTAG, USERNAME: u, PASSWORD: p}\n")
    before = p.read_text()
    did, pv = run("bulk_edit", {"MATCH": "PLATFORM=eos", "SET": "CREDENTIAL=NEWTAG"}, inventory_name="HAND")
    assert pv["summary"] == ["set CREDENTIAL=NEWTAG on 1 device record(s): SPINE1"] and pv["warnings"] == []
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    ch = _changed(before, p.read_text())
    assert ch == ["-    CREDENTIAL: FAB", "+    CREDENTIAL: NEWTAG"]
    assert "# quoted on purpose" in p.read_text()
    d = iv.start(cfg(), "bulk_edit", host=True, inventory_name="HAND")
    _, probs = iv.answer(cfg(), d["id"], {"MATCH": "PLATFORM=NOPE", "SET": "DEVICE_ADDR=1.2.3.4"}, host=True)
    assert any("unknown platform" in x for x in probs) and any("unique per record" in x for x in probs)
    _, probs = iv.answer(cfg(), d["id"], {"MATCH": "PLATFORM=IOSXE|NXOS", "SET": "CREDENTIAL=GHOST"}, host=True)
    assert any("GHOST does not exist" in x for x in probs)


# --------------------------------------------------------------------------- MCP round trip
async def test_mcp_interview_round_trip(vault):
    async with Client(mcp) as c:
        r = await c.call_tool("interview_start", {"topic": "record_add", "inventory": "FAB"})
        did = r.structured_content["draft"]
        assert r.structured_content["next_questions"][0]["field"] == "DEVICE_NAME"
        r = await c.call_tool("interview_answer", {"draft": did, "answers": DEV})
        assert r.structured_content["problems"] == [] and r.structured_content["ready_for_preview"]
        pv = (await c.call_tool("interview_preview", {"draft": did})).structured_content
        with pytest.raises(ToolError, match="does not match"):
            await c.call_tool("interview_commit", {"draft": did, "code": "WRONG1"})
        r = await c.call_tool("interview_commit", {"draft": did, "code": pv["commit_code"]})
        assert "add device LEAF9" in r.content[0].text
        hist = (await c.call_tool("inventory_history", {"name": "FAB"})).content[0].text
        assert "1 earlier version" in hist
        with pytest.raises(ToolError, match="only on the host"):
            await c.call_tool("interview_start", {"topic": "consumer_access"})


async def test_mcp_respects_consumer_limits(vault):
    write_boot(vault.parent, """\
        CONSUMERS:
          LAB_BOT: {INVENTORIES: [INVENTORY_PALAB], CREDENTIALS: [LAB_PANOS]}
        CONSUMER: LAB_BOT
        """)
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="not available to consumer LAB_BOT"):
            await c.call_tool("interview_start", {"topic": "record_add", "inventory": "FAB"})
        r = await c.call_tool("interview_start", {"topic": "record_add", "inventory": "PALAB"})
        did = r.structured_content["draft"]
        r = await c.call_tool("interview_answer", {"draft": did, "answers": {"CREDENTIAL": "FAB"}})
        assert any("may not use credential FAB" in p for p in r.structured_content["problems"])


# --------------------------------------------------------------------------- NetBox
class FakeNetBox:
    """A tiny in-memory NetBox REST API for the endpoints netbox.py uses."""
    NESTED = {"site": "dcim/sites/", "role": "dcim/device-roles/", "device_type": "dcim/device-types/",
              "platform": "dcim/platforms/", "manufacturer": "dcim/manufacturers/"}

    def __init__(self):
        self.db: dict[str, dict[int, dict]] = {}
        self.next = 1
        self.writes = 0

    def _out(self, ep, o):
        o = dict(o)
        for k, nep in self.NESTED.items():
            if isinstance(o.get(k), int):
                o[k] = {"id": o[k], "slug": self.db[nep][o[k]].get("slug")}
        if "tags" in o:
            o["tags"] = [{"id": t, "slug": self.db["extras/tags/"][t]["slug"]} for t in o["tags"]]
        for k in ("primary_ip4", "primary_ip6"):
            if isinstance(o.get(k), int):
                o[k] = {"id": o[k]}
        return o

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Token tok-123"
        path = req.url.path.removeprefix("/api/")
        parts = path.strip("/").split("/")
        ep = "/".join(parts[:2]) + "/"
        table = self.db.setdefault(ep, {})
        if req.method == "GET":
            q = dict(req.url.params)
            res = []
            for o in table.values():
                ok = all(str(o.get("device" if k == "device_id" else k)) == v for k, v in q.items())
                if ok:
                    res.append(self._out(ep, o))
            return httpx.Response(200, json={"count": len(res), "results": res})
        body = json.loads(req.content)
        self.writes += 1
        if req.method == "POST":
            oid, self.next = self.next, self.next + 1
            table[oid] = {"id": oid, **body}
            return httpx.Response(201, json=self._out(ep, table[oid]))
        oid = int(parts[2])
        table[oid].update(body)
        return httpx.Response(200, json=self._out(ep, table[oid]))


def _netbox_setup(vault_dir):
    write_boot(vault_dir.parent, """\
        NETBOX: {URL: "https://netbox.test", TOKEN_CREDENTIAL: NB}
        """)
    (vault_dir / "CREDENTIALS.yml").write_text(
        (vault_dir / "CREDENTIALS.yml").read_text() + "- {TAG: NB, TYPE: API_TOKEN, API_KEY: tok-123}\n")


def test_netbox_dry_run_then_commit(vault):
    _netbox_setup(vault)
    fake = FakeNetBox()
    t = httpx.MockTransport(fake.handler)
    dry = netbox.export(cfg(), "FAB", transport=t)
    assert dry["dry_run"] and fake.writes == 0
    assert dry["counts"]["create"] >= 8 and any("device QALEAF1A" in x for x in dry["changes"])
    assert "tok-123" not in json.dumps(dry)
    with pytest.raises(netbox.NetBoxError, match="does not match"):
        netbox.export(cfg(), "FAB", code="BADC0DE1", transport=t)
    done = netbox.export(cfg(), "FAB", code=dry["commit_code"], transport=t)
    assert done["applied"] == dry["counts"]
    devs = fake.db["dcim/devices/"]
    assert {d["name"] for d in devs.values()} == {"QALEAF1A", "PDLEAF1A"}
    assert all(d.get("primary_ip4") for d in devs.values())
    assert {o["address"] for o in fake.db["ipam/ip-addresses/"].values()} == {"198.19.0.11/32", "198.19.0.21/32"}
    again = netbox.export(cfg(), "FAB", transport=t)                      # second run: nothing to do
    assert again["commit_code"] is None and set(again["counts"]) == {"ok"}
    assert "netbox_export" in (cfg().state_path / "audit.log").read_text()


def test_netbox_leaves_foreign_devices_alone(vault):
    _netbox_setup(vault)
    fake = FakeNetBox()
    fake.db["dcim/devices/"] = {900: {"id": 900, "name": "QALEAF1A", "tags": []}}
    dry = netbox.export(cfg(), "FAB", transport=httpx.MockTransport(fake.handler))
    assert any(x.startswith("skip") and "QALEAF1A" in x and "not made by ATLAS" in x for x in dry["changes"])


def test_netbox_not_configured(vault):
    with pytest.raises(netbox.NetBoxError, match="not configured"):
        netbox.export(cfg(), "FAB")
