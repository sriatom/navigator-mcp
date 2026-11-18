"""The NAVIGATOR logo, advertised as the icon of the three MCP servers (navigator, atlas, rudder).

The SVG ships inside the package and goes out as a data: URI, so hosts on LOCAL or offline networks can
still show it (no fetch from navigator.atomidea.com).
"""

from __future__ import annotations

import base64
from importlib import resources

WEBSITE_URL = "https://navigator.atomidea.com/"


def logo_svg() -> bytes:
    return resources.files("navigator_engineer").joinpath("logo.svg").read_bytes()


def icons() -> list:
    """[Icon] for FastMCP(icons=...); [] if the logo or the Icon type is missing, so a server never fails to start."""
    try:
        from mcp.types import Icon
        uri = "data:image/svg+xml;base64," + base64.b64encode(logo_svg()).decode("ascii")
        return [Icon(src=uri, mimeType="image/svg+xml", sizes=["any"])]
    except Exception:  # noqa: BLE001 - branding must never stop a server
        return []
