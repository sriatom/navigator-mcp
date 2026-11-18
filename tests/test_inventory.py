from __future__ import annotations

import pytest

from atlas_inventory import inventory as inv
from conftest import INV_LAB


def test_resolve_names_only(vault):
    assert inv.resolve(vault, "PALAB").name == "INVENTORY_PALAB.yml"
    assert inv.resolve(vault, "INVENTORY_FAB.yml").name == "INVENTORY_FAB.yml"
    for bad in ("../CREDENTIALS", "/etc/passwd", "CREDENTIALS", "a/b", ""):
        with pytest.raises(inv.InventoryError):
            inv.resolve(vault, bad)


def test_list_names_ignores_other_files(vault):
    assert inv.list_names(vault) == ["INVENTORY_FAB", "INVENTORY_PALAB"]


def test_select(vault):
    devs = inv.load(vault / "INVENTORY_PALAB.yml")
    assert [d["DEVICE_NAME"] for d in inv.select(devs)] == ["PA-EDGE", "CORE1"]
    assert len(inv.select(devs, include_disabled=True)) == 3
    assert [d["DEVICE_NAME"] for d in inv.select(devs, names=["pa-edge"])] == ["PA-EDGE"]
    assert [d["DEVICE_NAME"] for d in inv.select(devs, groups=["CORE"])] == ["CORE1"]
    assert inv.select(devs, groups=["core"]) == []          # groups are case-sensitive
    assert [d["DEVICE_NAME"] for d in inv.select(devs, filters={"os": "panos"})] == ["PA-EDGE"]
    assert [d["DEVICE_NAME"] for d in inv.select(devs, filters={"SITE": "HQ", "ROLE": "EDGE"})] == ["PA-EDGE"]


def test_valid_inventory(vault):
    rep = inv.validate_text(INV_LAB, {"LAB_PANOS", "LAB_EOS"})
    assert rep.ok, rep.errors
    assert len(rep.devices) == 3


def test_unknown_credential_tag(vault):
    rep = inv.validate_text(INV_LAB, {"LAB_PANOS"})
    assert not rep.ok
    assert any("LAB_EOS does not exist" in e for e in rep.errors)


@pytest.mark.parametrize("key", ["PASSWORD", "password", "ENABLE_PASSWORD", "api_key", "SNMP_COMMUNITY",
                                 "secret", "TOKEN", "ENABLE"])
def test_secret_fields_refused_with_line(key):
    text = INV_LAB.replace("PORT: 22, IS_ENABLED: true,\n   TAGS: {SITE: HQ, FAB: PALAB1, ROLE: [CORE]}",
                           f"PORT: 22, IS_ENABLED: true,\n   {key}: hunter2,\n   TAGS: {{SITE: HQ}}")
    assert key in text
    rep = inv.validate_text(text, None)
    assert not rep.ok
    msg = " ".join(rep.errors)
    assert f"key '{key}'" in msg and "line 7" in msg
    assert "hunter2" not in msg                              # the value is never echoed


def test_duplicates_and_bad_fields():
    text = """\
INVENTORY:
- {DEVICE_ID: A, DEVICE_NAME: A, DEVICE_ADDR: 10.0.0.1, CREDENTIAL: C, VENDOR: X, OS: EOS,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
- {DEVICE_ID: a, DEVICE_NAME: B, DEVICE_ADDR: 10.0.0.1, CREDENTIAL: C, VENDOR: X, OS: WEIRDOS,
   CONN_TYPE: SSH, PORT: 70000, IS_ENABLED: "yes"}
- {DEVICE_ID: C, DEVICE_NAME: "bad name", DEVICE_ADDR: "not an address!", CREDENTIAL: C,
   VENDOR: X, OS: EOS, CONN_TYPE: SSH, PORT: 22}
"""
    rep = inv.validate_text(text, None)
    errs = " | ".join(rep.errors)
    assert "duplicate DEVICE_ID a" in errs
    assert "duplicate DEVICE_ADDR 10.0.0.1" in errs
    assert "PORT must be a number" in errs
    assert "IS_ENABLED must be true or false" in errs
    assert "missing IS_ENABLED" in errs
    assert "DEVICE_NAME may use only" in errs
    assert "not an IP address or host name" in errs
    assert any("WEIRDOS" in w for w in rep.warnings)


def test_not_yaml_or_empty():
    assert not inv.validate_text("INVENTORY: [", None).ok
    assert not inv.validate_text("foo: 1\n", None).ok
    assert not inv.validate_text("", None).ok


def test_production_flag(vault):
    devs = {d["DEVICE_NAME"]: d for d in inv.load(vault / "INVENTORY_FAB.yml")}
    assert inv.is_production(devs["PDLEAF1A"])
    assert not inv.is_production(devs["QALEAF1A"])
    assert inv.is_production({"TAGS": {"ENV": "production"}})


def test_probe_unreachable():
    out = inv.probe([{"DEVICE_NAME": "X", "DEVICE_ADDR": "127.0.0.1", "PORT": 1}], timeout=0.5)
    assert out[0].startswith("X 127.0.0.1:1 NOT reachable")
