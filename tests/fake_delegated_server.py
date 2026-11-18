"""A stand-in delegated MCP server for the launcher test: reports what it can see, never secret values."""
import os

from fastmcp import FastMCP

mcp = FastMCP("fake-delegated")


@mcp.tool()
def run_command(device: str, command: str, timeout_s: int = 30) -> dict:
    inv = os.environ.get("MULTIVENDOR_INVENTORY_PATH", "")
    return {"status": "ok", "device": device, "command": command,
            "output": (f"inventory_exists={os.path.isfile(inv)} "
                       f"user_set={bool(os.environ.get('MULTIVENDOR_RUDDER_USERNAME'))} "
                       f"leaked_parent_var={'RUDDER_TEST_PARENT_SECRET' in os.environ} "
                       f"home_is_workdir={os.environ.get('HOME', '').startswith('/tmp')}")}


@mcp.tool()
def where() -> dict:
    return {"pid": os.getpid(), "host": os.environ.get("FAKE_HOST")}


if __name__ == "__main__":
    if os.environ.get("FAKE_PORT"):          # HTTP mode, like upstream pyATS MCP
        mcp.run(transport="http", host=os.environ["FAKE_HOST"], port=int(os.environ["FAKE_PORT"]),
                show_banner=False)
    else:
        mcp.run(show_banner=False)
