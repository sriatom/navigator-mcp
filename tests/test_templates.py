"""template_get: the shipped inventory / credentials templates."""
import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from atlas_inventory import credentials, inventory
from atlas_inventory.server import mcp


async def get(kind: str) -> str:
    async with Client(mcp) as c:
        r = await c.call_tool("template_get", {"kind": kind})
        return "\n".join(getattr(x, "text", "") for x in r.content)


async def test_inventory_template_is_valid_and_has_the_roles(tmp_path):
    text = await get("inventory")
    rep = inventory.validate_text(text, credential_tags={"FABRIC_LOGIN", "SDWAN_API", "OOB_LOGIN", "CISCO_LOGIN", "PANOS_LOGIN", "FORTI_API"})
    assert rep.errors == []
    p = tmp_path / "INVENTORY_T.yml"
    p.write_text(text)
    devs = [d for d in inventory.load(p) if d["IS_ENABLED"]]
    assert [d["DEVICE_NAME"] for d in devs] == ["PDSPINE1", "PDLEAF1A", "PDLEAF1B", "PDSVCL1A", "CORE-RTR1",
                                            "DC-NX1", "FW1", "FW1-API", "SDWAN-MGR1", "SDWAN-MGR1-SSH", "BR1-CEDGE", "FGT1", "FRR1",
                                            "JUMP1"]
    oses = {(d["OS"], d["CONN_TYPE"]) for d in inventory.load(p)}
    assert {("ARISTA_EOS", "EAPI"), ("ARISTA_EOS", "SSH"), ("IOSXE", "SSH"), ("NXOS", "SSH"), ("PANOS", "SSH"), ("PANOS", "XML_API"),
            ("VIPTELA", "VMANAGE_API"), ("VIPTELA", "SSH"), ("FORTIOS", "API"), ("FRR", "SSH"), ("LINUX", "SSH")} <= oses        # one per vendor and method
    roles = {r for d in devs for r in d["TAGS"]["ROLE"]}
    assert {"SPINE", "LEAF", "SERVICE_LEAF"} <= roles
    assert [d["DEVICE_NAME"] for d in inventory.select(devs, groups=["SERVICE_LEAF"])] == ["PDSVCL1A"]
    assert "192.0.2." in text and "21.150." not in text       # documentation addresses only


async def test_credentials_template_has_only_placeholders(tmp_path):
    text = await get("credentials")
    data = yaml.safe_load(text)["DATA"]
    secret_fields = {"LOGIN_PASSWORD", "PASSWORD", "API_KEY", "TOKEN", "COMMUNITY", "AUTH_PASS"}
    vals = [v for e in data for k, v in e.items() if k in secret_fields]
    assert vals and set(vals) == {"CHANGE_ME"}
    p = tmp_path / "CREDENTIALS.yml"
    p.write_text(text)
    assert credentials.tags(p) == {"FABRIC_LOGIN", "SDWAN_API", "PANOS_KEY", "SNMP_V2", "OOB_LOGIN"}


async def test_template_kind_is_checked():
    with pytest.raises(ToolError):
        await get("passwords")
