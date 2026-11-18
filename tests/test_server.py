"""End-to-end through an in-memory MCP client. Also checks that no tool output carries a secret."""
from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from atlas_inventory.server import mcp
from conftest import SECRET


async def call(tool, **args) -> str:
    async with Client(mcp) as c:
        r = await c.call_tool(tool, args)
        return "\n".join(getattr(x, "text", "") for x in r.content)


async def test_tools_listed(vault):
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"inventory_list", "inventory_show", "inventory_query", "inventory_validate",
                     "platform_list", "platform_show", "command_list", "credential_list",
                     "interview_start", "interview_answer", "interview_status", "interview_preview",
                     "interview_commit", "interview_cancel", "interview_list", "inventory_history",
                     "netbox_export", "template_get", "inventory_render"}


async def test_inventory_list(vault):
    out = await call("inventory_list")
    assert "INVENTORY_PALAB: 2/3 devices enabled, credentials LAB_EOS, LAB_PANOS" in out
    assert "INVENTORY_FAB: 2/2 devices enabled, credentials FAB, 1 PRODUCTION" in out


async def test_inventory_show(vault):
    out = await call("inventory_show", name="FAB")
    assert "QALEAF1A  198.19.0.11  ARISTA/EOS  EAPI:443  cred=FAB" in out
    assert "PDLEAF1A" in out and out.count("PRODUCTION") >= 1
    out = await call("inventory_show", name="PALAB", devices=["core1", "nope"])
    assert "CORE1" in out and "NOT FOUND: NOPE" in out


async def test_inventory_show_errors(vault):
    with pytest.raises(ToolError, match="not found"):
        await call("inventory_show", name="MISSING")
    with pytest.raises(ToolError, match="invalid inventory name"):
        await call("inventory_show", name="../CREDENTIALS")
    with pytest.raises(ToolError, match="no records matched"):
        await call("inventory_show", name="PALAB", groups=["NOPE"])


async def test_inventory_validate(vault):
    out = await call("inventory_validate", name="FAB")
    assert out.startswith("FAB: VALID, 2 device(s)")
    out = await call("inventory_validate", name="PALAB")
    assert "INVALID" in out and "LAB_EOS does not exist" in out


async def test_credential_list(vault):
    out = await call("credential_list")
    assert "FAB: type LOGIN (inferred); fields PASSWORD, USERNAME; used by INVENTORY_FAB" in out
    assert "LAB_PANOS: type LOGIN (inferred); fields PASSWORD, USERNAME; used by INVENTORY_PALAB" in out
    assert "LAB_EOS: MISSING (used by INVENTORY_PALAB" in out


async def test_no_secret_or_username_in_any_output(vault):
    outs = [await call("inventory_list"), await call("credential_list"),
            await call("inventory_show", name="FAB"), await call("inventory_show", name="PALAB"),
            await call("inventory_validate", name="FAB"), await call("inventory_validate", name="PALAB")]
    blob = "\n".join(outs)
    assert SECRET not in blob
    assert "admin" not in blob and "labuser" not in blob


async def test_locked_mode_env(vault, monkeypatch):
    from atlas_inventory import config
    monkeypatch.setenv("NAVI_MODE", "locked")
    assert config.load().mode == "LOCKED"
    monkeypatch.setenv("NAVI_MODE", "sideways")
    with pytest.raises(ValueError):
        config.load()
