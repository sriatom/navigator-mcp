"""Task 17 / decision #160: import an inventory from NetBox or Nautobot (mocked HTTP), then sync it."""
from __future__ import annotations

import json

import httpx
import pytest
import yaml

from atlas_inventory import config, interview as iv, source
from conftest import SECRET


def cfg():
    return config.load()


def nb_dev(i, name, ip, platform="arista-eos", role="leaf", site="qa", tags=(), status="active"):
    return {"id": i, "name": name, "primary_ip4": {"address": f"{ip}/24"} if ip else None,
            "platform": {"slug": platform, "name": platform} if platform else None,
            "device_type": {"model": "7050", "manufacturer": {"name": "Arista", "slug": "arista"}},
            "site": {"slug": site, "name": site.upper()}, "role": {"slug": role, "name": role},
            "tags": [{"slug": t, "name": t} for t in tags], "status": {"value": status, "label": status}}


class Fake:
    """GET /api/dcim/devices/ with paging; counts calls; refuses a wrong token; never accepts writes."""

    def __init__(self, devices, page=2, token="tok-123"):
        self.devices, self.page, self.token, self.calls, self.writes, self.seen = devices, page, token, 0, 0, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method != "GET":
            self.writes += 1
            return httpx.Response(405)
        if req.headers.get("authorization") != f"Token {self.token}":
            return httpx.Response(403, json={"detail": "Invalid token"})
        assert req.url.path == "/api/dcim/devices/"
        self.calls += 1
        self.seen.append(dict(req.url.params))
        rows = self.devices
        if req.url.params.get("site"):
            rows = [d for d in rows if (d.get("site") or {}).get("slug") == req.url.params["site"]]
        off = int(req.url.params.get("offset", 0))
        nxt = None
        if off + self.page < len(rows):
            nxt = str(req.url.copy_set_param("offset", off + self.page).copy_set_param("limit", self.page))
        return httpx.Response(200, json={"count": len(rows), "next": nxt, "results": rows[off:off + self.page]})


@pytest.fixture
def nb(vault, monkeypatch):
    (vault / "CREDENTIALS.yml").write_text((vault / "CREDENTIALS.yml").read_text()
                                           + "- {TAG: NB, TYPE: API_TOKEN, API_KEY: tok-123}\n"
                                           + f"- {{TAG: LAB_EOS, USERNAME: admin, PASSWORD: {SECRET}}}\n")
    (vault / "INVENTORY_DC.yml").write_text("META: {DESCRIPTION: from netbox, DEFAULTS: {PRODUCTION: false}}\n"
                                            "INVENTORY: []\n")
    fake = Fake([nb_dev(1, "QALEAF7A", "198.19.0.71"), nb_dev(2, "QASPINE7", "198.19.0.70", role="spine"),
                 nb_dev(3, "PAFW7", "198.19.0.72", platform="panos", role="firewall", tags=["fw"]),
                 nb_dev(4, "NOIP7", None), nb_dev(5, "ODD7", "198.19.0.75", platform="widget-os")])
    monkeypatch.setattr(source, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


ANSWERS = {"SOURCE": "netbox", "URL": "https://netbox.test", "AUTH_CREDENTIAL": "NB", "VERIFY_TLS": "yes",
           "CRED_MAP": "TAG:fw=LAB_PANOS, *=FAB"}


def run(topic, answers=None, **kw):
    d = iv.start(cfg(), topic, host=True, inventory_name="DC", **kw)
    if answers:
        _, probs = iv.answer(cfg(), d["id"], answers, host=True)
        assert not probs, probs
    return d["id"], iv.preview(cfg(), d["id"], host=True)


def test_import_shows_the_diff_first_then_writes(nb, vault):
    did, pv = run("source_import", ANSWERS)
    assert pv["errors"] == [], pv["errors"]
    assert nb.writes == 0 and nb.calls >= 3                      # paged, read only
    assert any(s.startswith("from netbox") and "add 3" in s for s in pv["summary"])
    assert any("NOIP7: no primary IP" in w for w in pv["warnings"])
    assert any("ODD7: platform widget-os is not known" in w and "PLATFORM_MAP" in w for w in pv["warnings"])
    assert "+- DEVICE_NAME: QALEAF7A" in pv["diff"] or "DEVICE_NAME: QALEAF7A" in pv["diff"]
    before = (vault / "INVENTORY_DC.yml").read_text()
    assert "QALEAF7A" not in before                              # nothing written by the preview
    assert "tok-123" not in json.dumps(pv) and SECRET not in json.dumps(pv)
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    doc = yaml.safe_load((vault / "INVENTORY_DC.yml").read_text())
    recs = {r["DEVICE_NAME"]: r for r in doc["INVENTORY"]}
    assert set(recs) == {"QALEAF7A", "QASPINE7", "PAFW7"}
    leaf = recs["QALEAF7A"]
    assert leaf["OS"] == "EOS" and leaf["VENDOR"] == "ARISTA" and leaf["DEVICE_ADDR"] == "198.19.0.71"
    assert leaf["CREDENTIAL"] == "FAB" and leaf["SOURCE_ID"] == "netbox:1" and leaf["PRODUCTION"] is False
    assert leaf["TAGS"] == {"SITE": "QA", "ROLE": ["LEAF"]}
    assert recs["PAFW7"]["CREDENTIAL"] == "LAB_PANOS" and recs["PAFW7"]["OS"] == "PANOS"
    src = doc["META"]["SOURCE"]
    assert src["TYPE"] == "netbox" and src["AUTH_CREDENTIAL"] == "NB" and "tok-123" not in str(doc)


def test_sync_updates_owned_fields_keeps_ours_and_disables_gone(nb, vault):
    did, pv = run("source_import", ANSWERS)
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    path = vault / "INVENTORY_DC.yml"
    doc = yaml.safe_load(path.read_text())
    for r in doc["INVENTORY"]:
        if r["DEVICE_NAME"] == "QALEAF7A":
            r["PORT"], r["CONN_TYPE"], r["CREDENTIAL"] = 22, "SSH", "LAB_EOS"     # our own edits
    doc["INVENTORY"].append({"DEVICE_ID": "HAND1", "DEVICE_NAME": "HAND1", "DEVICE_ADDR": "198.19.0.99",
                             "CREDENTIAL": "FAB", "VENDOR": "ARISTA", "OS": "EOS", "CONN_TYPE": "EAPI",
                             "PORT": 443, "IS_ENABLED": True})
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    nb.devices[0]["primary_ip4"] = {"address": "198.19.0.171/24"}          # moved in NetBox
    nb.devices[1]["status"] = {"value": "offline"}                          # offline in NetBox
    del nb.devices[2]                                                       # gone from NetBox
    did, pv = run("inventory_sync")
    assert pv["errors"] == [], pv["errors"]
    s = "\n".join(pv["summary"])
    assert "add 0, change 1, disable 1" not in s and "change 2" in s and "disable 1" in s
    assert "DEVICE_ADDR 198.19.0.71 -> 198.19.0.171" in s and "IS_ENABLED True -> False" in s
    assert "disable: PAFW7" in s
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    recs = {r["DEVICE_NAME"]: r for r in yaml.safe_load(path.read_text())["INVENTORY"]}
    leaf = recs["QALEAF7A"]
    assert leaf["DEVICE_ADDR"] == "198.19.0.171" and leaf["PORT"] == 22 and leaf["CONN_TYPE"] == "SSH"
    assert leaf["CREDENTIAL"] == "LAB_EOS"
    assert recs["QASPINE7"]["IS_ENABLED"] is False and recs["PAFW7"]["IS_ENABLED"] is False   # never deleted
    assert recs["HAND1"]["IS_ENABLED"] is True and "SOURCE_ID" not in recs["HAND1"]           # hand records alone
    did, pv = run("inventory_sync")                                     # nothing new: nothing to change
    assert any("add 0, change 0, disable 0" in x for x in pv["summary"])


def test_source_change_between_preview_and_commit_needs_a_new_preview(nb):
    did, pv = run("source_import", ANSWERS)
    nb.devices[0]["primary_ip4"] = {"address": "198.19.0.200/24"}
    with pytest.raises(iv.InterviewError, match="does not match"):
        iv.commit(cfg(), did, pv["commit_code"], host=True)


def test_hand_record_with_the_same_name_is_adopted(nb, vault):
    path = vault / "INVENTORY_DC.yml"
    path.write_text("META: {DEFAULTS: {PRODUCTION: false}}\nINVENTORY:\n"
                    "- {DEVICE_ID: QALEAF7A, DEVICE_NAME: QALEAF7A, DEVICE_ADDR: 198.19.0.71, CREDENTIAL: LAB_EOS,\n"
                    "   VENDOR: ARISTA, OS: EOS, CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}\n")
    did, pv = run("source_import", ANSWERS)
    assert any("QALEAF7A (adopted" in x or ("QALEAF7A (" in x and "adopted" in x) for x in pv["summary"])
    iv.commit(cfg(), did, pv["commit_code"], host=True)
    leaf = [r for r in yaml.safe_load(path.read_text())["INVENTORY"] if r["DEVICE_NAME"] == "QALEAF7A"][0]
    assert leaf["SOURCE_ID"] == "netbox:1" and leaf["CREDENTIAL"] == "LAB_EOS" and leaf["CONN_TYPE"] == "SSH"


def test_filter_and_platform_map(nb, vault):
    nb.devices.append(nb_dev(6, "HQLEAF1", "10.9.9.1", site="hq"))
    did, pv = run("source_import", {**ANSWERS, "FILTER": "site=hq", "PLATFORM_MAP": "widget-os=EOS"})
    assert pv["errors"] == [] and any("add 1" in s for s in pv["summary"])
    assert all(p.get("site") == "hq" for p in nb.seen if "site" in p) and any("site" in p for p in nb.seen)
    _, pv = run("source_import", {**ANSWERS, "PLATFORM_MAP": "widget-os=EOS"})
    assert any("add 5" in s for s in pv["summary"])                     # ODD7 mapped; HQLEAF1 too


def test_bad_token_and_unknown_credential(nb, vault, monkeypatch):
    monkeypatch.setattr(nb, "token", "something-else")
    _, pv = run("source_import", ANSWERS)
    assert pv["commit_code"] is None and any("token was refused" in e for e in pv["errors"])
    d = iv.start(cfg(), "source_import", host=True, inventory_name="DC")
    _, probs = iv.answer(cfg(), d["id"], {**ANSWERS, "CRED_MAP": "*=NOPE"}, host=True)
    assert any("NOPE does not exist" in p for p in probs)


def test_no_cred_rule_blocks(nb):
    _, pv = run("source_import", {**ANSWERS, "CRED_MAP": "ROLE:spine=FAB"})
    assert pv["commit_code"] is None and any("QALEAF7A: no CRED_MAP rule matches" in e for e in pv["errors"])


def test_sync_needs_a_source(vault):
    with pytest.raises(iv.InterviewError, match="source_import first"):
        iv.start(cfg(), "inventory_sync", host=True, inventory_name="FAB")


def test_inventory_new_asks_where_the_list_is(vault):
    d = iv.start(cfg(), "inventory_new", host=True)
    st = iv.status(cfg(), d["id"], host=True)
    assert "SOURCE" in json.dumps(st)
    iv.answer(cfg(), d["id"], {"NAME": "NB2", "PRODUCTION": "no", "SOURCE": "nautobot"}, host=True)
    pv = iv.preview(cfg(), d["id"], host=True)
    assert any("next: topic source_import (Nautobot) with inventory=NB2" in s for s in pv["summary"])


def test_nautobot_through_the_same_interface(nb, vault):
    rows = [{"id": "9b1c", "name": "NBLEAF1", "primary_ip4": {"address": "198.19.0.81/24"},
             "platform": {"name": "Arista EOS", "network_driver": "arista_eos"},
             "device_type": {"model": "7050", "manufacturer": {"name": "Arista"}},
             "location": {"name": "QA"}, "role": {"name": "leaf"}, "status": {"name": "Active"},
             "tags": [{"name": "lab"}]},
            {"id": "77aa", "name": "NBOLD", "primary_ip4": {"address": "198.19.0.82/24"},
             "platform": {"name": "Arista EOS", "network_driver": "arista_eos"},
             "location": {"name": "QA"}, "role": {"name": "leaf"}, "status": {"name": "Planned"}, "tags": []}]
    fake = Fake(rows)
    t = httpx.MockTransport(fake.handler)
    devs = source.client("nautobot", "https://nautobot.test", "tok-123", transport=t).devices()
    assert [(d.sid, d.site, d.role, d.active, d.addr) for d in devs] == \
        [("nautobot:9b1c", "QA", "leaf", True, "198.19.0.81"), ("nautobot:77aa", "QA", "leaf", False, "198.19.0.82")]
    assert fake.seen[0].get("depth") == "2"
    from atlas_inventory import catalog
    p = source.plan([], devs, {"TYPE": "nautobot", "CRED_MAP": "TAG:lab=LAB_EOS, *=FAB"},
                    catalog.Catalog(cfg().catalog_path), {"FAB", "LAB_EOS"})
    recs = {r["DEVICE_NAME"]: r for r in p.records}
    assert recs["NBLEAF1"]["OS"] == "EOS" and recs["NBLEAF1"]["CREDENTIAL"] == "LAB_EOS"
    assert recs["NBOLD"]["IS_ENABLED"] is False and recs["NBOLD"]["CREDENTIAL"] == "FAB"


def test_cred_map_parsing():
    assert source.parse_cred_map("ROLE:LEAF=FAB, *=X") == [("ROLE", "LEAF", "FAB"), ("*", "*", "X")]
    for bad in ("LEAF=FAB", "COLOR:red=FAB", ""):
        with pytest.raises(source.SourceError):
            source.parse_cred_map(bad)
