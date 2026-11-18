"""P7b: research through SearXNG (mocked), LOCKED query check, logging, safe page fetch. No network."""
from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

from navigator_engineer.research import Research, ResearchError, check_generic
from test_p7 import nav  # noqa: F401

PAGE = (b"<html><head><title>EVPN type-5 | Docs</title><script>evil()</script></head>"
        b"<body><nav>menu</nav><h1>Type-5 routes</h1><p>Check <b>ip routing vrf</b> first.</p></body></html>")


def handler(seen):
    def h(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path == "/search":
            assert req.url.params["format"] == "json"
            return httpx.Response(200, json={"results": [
                {"title": "EVPN  type-5", "url": "https://docs.example.com/evpn", "content": "about type-5",
                 "engines": ["brave"]},
                {"title": "Forum", "url": "https://forum.example.com/t/1", "content": "x", "engines": ["google"]},
                {"title": "no url"}]})
        if req.url.host == "docs.example.com":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=PAGE)
        if req.url.host == "forum.example.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/admin"})
        return httpx.Response(404)
    return h


def research(nav, seen):  # noqa: F811
    client = httpx.Client(transport=httpx.MockTransport(handler(seen)))
    return Research(nav["s"], client=client,
                    resolver=lambda host: not host.startswith(("10.", "127.", "192.168.")))


def test_open_search_fetch_and_log(nav):  # noqa: F811
    nav["p"].create("OPEN", "evpn issue", name="OPENJOB")
    seen = []
    r = research(nav, seen).search("Arista EOS 4.33 EVPN type-5 route not installed", "OPENJOB", fetch=2)
    assert [x["url"] for x in r["results"]] == ["https://docs.example.com/evpn", "https://forum.example.com/t/1"]
    assert r["results"][0]["title"] == "EVPN type-5"
    page = r["pages"][0]
    assert page["title"] == "EVPN type-5 | Docs" and "ip routing vrf" in page["text"]
    assert "evil" not in page["text"] and "menu" not in page["text"]
    assert r["pages"][1]["error"] == "refused: not a public address"      # redirect into the lab refused
    assert not any("10.0.0.5" in u for u in seen)
    log = (nav["ws"] / "OPENJOB" / "RECORDS" / "PUBLIC_QUERIES.md").read_text()
    assert "[CLOUD] Arista EOS 4.33 EVPN type-5" in log and "2 results, 2 pages read" in log


def test_locked_refuses_specific_queries(nav):  # noqa: F811
    nav["p"].create("LOCKED", "secret job", name="ACME_BGP", inventory="INVENTORY_ACMEDC")
    seen = []
    rs = research(nav, seen)
    for q, why in [("bgp flap 10.20.30.1 neighbor", "IP address"),
                   ("router bgp 65001 neighbor down", r"AS number \(65001\)"),
                   ("ACME_BGP evpn issue", "real name"),
                   ("ACMEDC leaf evpn", "real name"),
                   ("interface Ethernet1/1\n ip address x\n shutdown", "config or log line"),
                   ("mac 00:1b:17:00:aa:01 flapping", "MAC address"),
                   ("2001:db8::1 unreachable", "IPv6")]:
        with pytest.raises(ResearchError, match=why):
            rs.search(q, "ACME_BGP")
    assert seen == []                                                     # nothing left navhost
    log = (nav["ws"] / "ACME_BGP" / "RECORDS" / "PUBLIC_QUERIES.md").read_text()
    assert "10.20.30.1" not in log and "65001" not in log and "ACME" not in log and log.count("-> refused") == 7
    r = rs.search("EOS 4.33 BGP EVPN type-5 route not installed at 02:00:00", "ACME_BGP")
    assert r["mode"] == "LOCAL" and len(r["results"]) == 2


def test_locked_uses_scrambler_map_and_denylist(nav):  # noqa: F811
    nav["p"].create("LOCKED", "x", name="JOB2")
    maps = nav["s"].maps_path / "JOB2"
    maps.mkdir(parents=True)
    (maps / "map.json").write_text(json.dumps({"entries": {"host": {"h1": {"real": "nyc-core-rtr1"}}}}))
    (maps / "denylist.txt").write_text("globex\n")
    with pytest.raises(ResearchError, match="real name"):
        research(nav, []).search("nyc-core-rtr1 ospf stuck in exstart", "JOB2")
    with pytest.raises(ResearchError, match="real name"):
        research(nav, []).search("Globex firewall policy", "JOB2")


def test_check_generic_allows_versions():
    assert check_generic("IOS-XE 17.18.2 cEdge controller mode pyATS timeout", []) == []
    assert check_generic("EOS 4.33.1F bfd flap", []) == []


def test_searxng_down(nav):  # noqa: F811
    def down(req):
        raise httpx.ConnectError("refused")
    rs = Research(nav["s"], client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(ResearchError, match="did not answer"):
        rs.search("anything")
    assert "search failed" in (nav["s"].state_path / "PUBLIC_QUERIES.md").read_text()


async def test_research_tool_listed(nav, monkeypatch, tmp_path):  # noqa: F811
    boot = tmp_path / "navigator.yml"
    boot.write_text(f"WORKSPACE: {nav['ws']}\nSTATE_PATH: {tmp_path / 'st'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(boot))
    from navigator_engineer.server import mcp
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "research" in names
