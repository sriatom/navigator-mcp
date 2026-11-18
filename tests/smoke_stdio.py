"""Smoke test over real stdio against the configured DATA_PATH. Prints only summaries
(tags, field names and counts; never values).

    .venv/bin/python tests/smoke_stdio.py
"""
import asyncio
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO = Path(__file__).resolve().parent.parent


async def main() -> None:
    t = StdioTransport(command=sys.executable, args=["-m", "atlas_inventory.server"], cwd=str(REPO))
    async with Client(t) as c:
        print("tools:", sorted(x.name for x in await c.list_tools()))
        for tool, args in [("inventory_list", {}), ("credential_list", {})]:
            r = await c.call_tool(tool, args)
            print(f"== {tool}\n{r.content[0].text}")
        names = [ln.split(":")[0] for ln in
                 (await c.call_tool("inventory_list", {})).content[0].text.splitlines()]
        for n in names:
            r = await c.call_tool("inventory_validate", {"name": n})
            lines = r.content[0].text.splitlines()
            unknown = [ln for ln in lines if "platform catalogue" in ln or "not listed for" in ln]
            print("== validate", lines[0] + (f"  ({len(unknown)} platform warnings)" if unknown else ""))
            for ln in unknown[:5]:
                print("    ", ln.strip())
        r = await c.call_tool("platform_list", {})
        print("== platform_list:", r.content[0].text.splitlines()[0])
        for p in ("EOS", "IOSXE", "NXOS", "PANOS", "VIPTELA"):
            r = await c.call_tool("command_list", {"platform": p})
            print("== command_list", r.content[0].text.splitlines()[0])


asyncio.run(main())
