import base64

from navigator_engineer import branding


def test_icon_is_the_packaged_logo():
    (icon,) = branding.icons()
    assert icon.mimeType == "image/svg+xml" and icon.sizes == ["any"]
    assert icon.src.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(icon.src.split(",", 1)[1])
    assert svg == branding.logo_svg() and b"<svg" in svg and len(svg) < 4096


async def test_three_servers_advertise_the_icon():
    from atlas_inventory import server as A
    from navigator_engineer import server as N
    from rudder_devices import server as R
    for srv in (A.mcp, R.mcp, N.mcp):
        assert srv.icons and srv.icons[0].src.startswith("data:image/svg+xml;base64,"), srv.name
        assert srv.website_url == branding.WEBSITE_URL
