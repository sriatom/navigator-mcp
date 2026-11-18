"""Vendor packs: the loader, the Arista pack, and turning packs off."""
from __future__ import annotations

from pathlib import Path

import yaml

import navigator_packs
from atlas_inventory.catalog import Catalog

CAT = Path(__file__).resolve().parent.parent / "atlas_inventory" / "catalog"
TEMPLATE = Path(__file__).resolve().parent.parent / "atlas_inventory" / "templates" / "INVENTORY_TEMPLATE.yml"


def test_arista_pack_is_found_and_complete():
    assert "arista" in navigator_packs.available()
    p = navigator_packs.pack("arista")
    assert set(p["platforms"]) == {"EOS"} and set(p["methods"]) == {"eapi", "ssh-json"}
    assert {"CONN_TYPE", "PORT"} <= set(p["fields"])
    assert "configuration session" in navigator_packs.knowledge("arista")
    row = next(r for r in navigator_packs.summary() if r["pack"] == "arista")
    assert row["enabled"] and row["platforms"] == ["EOS"]


def test_eos_comes_from_the_pack_not_the_generated_file():
    gen = yaml.safe_load((CAT / "PLATFORMS.yml").read_text())["PLATFORMS"]
    assert "EOS" not in gen and "ARISTA_EOS" not in gen          # not even as a generic netmiko platform
    c = Catalog(CAT)
    for name in ("EOS", "arista_eos", "ARISTA-EOS", "aristaeos"):
        p = c.platform(name)
        assert p["ID"] == "EOS" and p["pack"] == "arista" and p["reads"] == p["writes"] == "native:eapi"
    assert sum(len(v) for v in c.commands_for("EOS").values()) == 95


def test_a_turned_off_pack_is_gone(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_PACKS", "none-of-them")
    assert navigator_packs.enabled() == []
    c = Catalog(CAT)
    assert c.resolve("arista_eos") is None and "EOS" not in c.commands()
    monkeypatch.setenv("NAVIGATOR_PACKS", "arista")
    assert Catalog(CAT).resolve("arista_eos") == "EOS"


def test_rudder_uses_the_pack_adapters():
    import navigator_packs.arista.adapters as A
    from rudder_devices import adapters
    assert adapters.EosEapi is A.EosEapi and adapters.EosEapiWriter is A.EosEapiWriter
    assert adapters.EosSshJson is A.EosSshJson
    assert adapters.DeviceError is A.DeviceError                  # one error type across core and packs


def test_every_pack_method_has_a_template_example():
    recs = {r["DEVICE_ID"]: r for r in yaml.safe_load(TEMPLATE.read_text())["INVENTORY"]}
    for name in navigator_packs.available():
        p = navigator_packs.pack(name)
        for method, spec in (p.get("methods") or {}).items():
            if spec.get("available") is False:                  # server not installed yet: no example
                continue
            ex = (p.get("template_examples") or {}).get(method)
            assert ex in recs, f"{name}: no template example for {method}"
            assert recs[ex]["CONN_TYPE"] == spec["conn"]


# --------------------------------------------------------------------------- Cisco pack
def test_cisco_pack_platforms_and_commands():
    c = Catalog(CAT)
    for name, pid in (("cisco_xe", "IOSXE"), ("CISCO_NXOS", "NXOS"), ("ios-xr", "IOSXR"), ("cisco_asa", "ASA"),
                      ("SDWAN", "VIPTELA"), ("cisco_viptela", "VIPTELA")):
        p = c.platform(name)
        assert p["ID"] == pid and p["pack"] == "cisco"
    gen = yaml.safe_load((CAT / "PLATFORMS.yml").read_text())["PLATFORMS"]
    assert not {"IOSXE", "NXOS", "VIPTELA", "CISCO_VIPTELA"} & set(gen)
    counts = {pid: sum(len(v) for v in c.commands_for(pid).values()) for pid in ("IOSXE", "NXOS", "VIPTELA")}
    assert counts == {"IOSXE": 170, "NXOS": 147, "VIPTELA": 20}


def test_old_module_names_are_the_pack_modules():
    import rudder_devices.cisco as old_cli
    import rudder_devices.vmanage as old_vm
    from navigator_packs.cisco import cli, vmanage
    assert old_cli is cli and old_vm is vmanage


def test_pyats_defaults_come_from_the_pack():
    from rudder_devices import config, delegated
    s = config.load()
    a = delegated.pyats_settings(s, "iosxe")
    assert a["connection_args"]["init_config_commands"] == [] and a["connection_args"]["operating_mode"] is True \
        if "connection_args" in a else True
    base, by_os = delegated._pyats_defaults()
    assert base == {"init_config_commands": []} and by_os["iosxe"] == {"operating_mode": True}


def test_sdwan_manager_over_ssh_or_api():
    from rudder_devices import adapters, config
    from navigator_packs.cisco.vmanage import Vmanage
    s = config.load()
    vip = Catalog(CAT).platform("VIPTELA")
    a, why = adapters.choose(vip, {"CONN_TYPE": "SSH"}, s)
    assert isinstance(a, adapters.NetmikoText) and a.device_type == "cisco_viptela", why
    a, _ = adapters.choose(vip, {"CONN_TYPE": "VMANAGE_API"}, s)
    assert isinstance(a, Vmanage)
    a, _ = adapters.choose(vip, {"CONN_TYPE": "VMANAGE_API"}, s, via="ssh")
    assert isinstance(a, adapters.NetmikoText)
    assert adapters.effective_dev({"CONN_TYPE": "VMANAGE_API", "PORT": 443}, "ssh")["PORT"] == 22


# --------------------------------------------------------------------------- Palo Alto pack
def test_paloalto_pack_entry_commands_and_alias():
    c = Catalog(CAT)
    p = c.platform("pa_panos")
    assert p["ID"] == "PANOS" and p["pack"] == "paloalto" and p["writes"] == "native:xml-api"
    assert sum(len(v) for v in c.commands_for("PANOS").values()) == 30
    import rudder_devices.panos as old
    from navigator_packs.paloalto import writer
    assert old is writer


def test_cli_to_xml():
    from navigator_packs.paloalto.xmlapi import cli_to_xml
    assert cli_to_xml("show system info", {}) == "<show><system><info></info></system></show>"
    assert cli_to_xml("show interface ethernet1/1", {}) == "<show><interface>ethernet1/1</interface></show>"
    assert cli_to_xml("show routing protocol bgp summary", {}) == \
        "<show><routing><protocol><bgp><summary></summary></bgp></protocol></routing></show>"
    assert cli_to_xml('show interface "a&b"', {}) == "<show><interface>a&amp;b</interface></show>"
    assert cli_to_xml("show interface all") == "<show><interface>all</interface></show>"       # from pack.yml
    assert cli_to_xml("show arp all") == "<show><arp><entry name='all'/></arp></show>"


def test_panos_xml_api_reads():
    import httpx
    from navigator_packs.paloalto.xmlapi import PanosXmlApi
    from rudder_devices import adapters, config
    seen = []

    def api(req):
        body = dict(x.split("=", 1) for x in req.content.decode().split("&"))
        seen.append((req.url.port, body.get("type")))
        if body["type"] == "keygen":
            return httpx.Response(200, text="<response status='success'><result><key>K1</key></result></response>")
        return httpx.Response(200, text="<response status='success'><result><system><hostname>pa1</hostname>"
                                        "<sw-version>11.1.0</sw-version></system></result></response>")
    s = config.load()
    pan = Catalog(CAT).platform("PANOS")
    a, _ = adapters.choose(pan, {"CONN_TYPE": "XML_API"}, s,
                           factories={"panos-xml-api": lambda: PanosXmlApi(s, client=lambda **kw: httpx.Client(
                               transport=httpx.MockTransport(api), **kw))})
    out = a.run({"DEVICE_ADDR": "192.0.2.1", "CONN_TYPE": "XML_API", "PORT": 8443},
                {"USERNAME": "u", "LOGIN_PASSWORD": "p"}, ["show system info"])
    assert out[0]["ok"] and out[0]["format"] == "json" and out[0]["output"]["system"]["sw-version"] == "11.1.0"
    assert seen == [(8443, "keygen"), (8443, "op")]                 # the record's own PORT is the API port
    assert isinstance(adapters.choose(pan, {"CONN_TYPE": "SSH"}, s)[0], adapters.NetmikoText)
    assert isinstance(adapters.choose(pan, {"CONN_TYPE": "SSH"}, s, via="xml-api")[0], PanosXmlApi)
    assert adapters.effective_dev({"CONN_TYPE": "SSH", "PORT": 22, "API_PORT": 4443}, "xml-api")["PORT"] == 4443
    eos = Catalog(CAT).platform("EOS")
    assert adapters.choose(eos, {"CONN_TYPE": "EAPI"}, s, via="xml-api")[0] is None


# --------------------------------------------------------------------------- delegated and generic packs
def test_every_platform_lives_in_a_pack():
    import yaml as _y
    ov = _y.safe_load((CAT / "platforms_overlay.yml").read_text())
    assert not ov.get("PLATFORMS")                                # the overlay keeps only controllers / OOB
    c = Catalog(CAT)
    for pid, pack in (("FORTIOS", "fortinet"), ("FRR", "multivendor"), ("SROS", "multivendor"), ("JUNOS", "juniper"),
                      ("AOSCX", "aruba"), ("BIGIP", "f5"), ("GAIA", "checkpoint"), ("LINUX", "generic")):
        assert c.platform(pid)["pack"] == pack
    assert c.commands_for("FORTIOS")["system"] == ["fgt_system_status", "fgt_list_interfaces"]


def test_generic_platforms_follow_the_generic_pack(monkeypatch):
    c = Catalog(CAT)
    assert c.platform("hp_comware")["reads"] == "native:generic-netmiko"
    monkeypatch.setenv("NAVIGATOR_PACKS", "arista,cisco")
    c = Catalog(CAT)
    assert c.resolve("hp_comware") is None and c.resolve("linux") is None and c.resolve("eos") == "EOS"


def test_unavailable_delegated_servers_say_so():
    from rudder_devices import adapters, config
    s = config.load()
    a, why = adapters.choose(Catalog(CAT).platform("JUNOS"), {"CONN_TYPE": "NETCONF"}, s)
    assert a is None and "junos" in why
    assert navigator_packs.pack("juniper")["methods"]["junos"]["available"] is False
