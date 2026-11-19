"""project action=files / search: answer from a project's own configs without a model."""
import pytest

from navigator_engineer import project_files as PF
from navigator_engineer.models import ModelError, read_files

NXOS = """!Command: show running-config
version 10.2(5)
hostname BOS-93360A
vlan 1,3100-3105
vlan 3102
  name APP-TIER
vlan 3103
  name DB-TIER
interface Vlan3102
  description APP gateway
  no shutdown
  ip address 10.31.2.2/24
  hsrp 102
    ip 10.31.2.1
interface Vlan3103
  ip address 10.31.3.2/24
interface Ethernet1/1
  switchport trunk allowed vlan 3102,3103
"""
IOS = """hostname WOR-C9500
!
interface Vlan31020
 ip address 10.99.0.1 255.255.255.0
!
interface Vlan310
 ip address 10.31.0.1 255.255.255.0
"""


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "DEMO2"
    (p / "CONFIG").mkdir(parents=True)
    (p / "CONFIG" / "10.3.1.3-BOS-93360A [10.3.1.3]-20261005.txt").write_text(NXOS)
    (p / "CONFIG" / "10.24.1.250-WOR-C9500 [10.24.1.250].txt").write_text(IOS)
    (p / "OUT").mkdir()
    (p / "OUT" / "old.md").write_text("vlan 3102 from an old answer")
    (p / "NAVIGATOR_STATE.json").write_text('{"note": "vlan 3102"}')
    (p / ".navigator_mode").write_text("LOCAL")
    return p


def test_search_returns_the_config_blocks(proj):
    r = PF.search(proj, "vlan 3102")
    assert list(r["files_with_matches"]) == ["CONFIG/10.3.1.3-BOS-93360A [10.3.1.3]-20261005.txt"]
    text = "\n".join(b["block"] for b in r["blocks"])
    assert "interface Vlan3102" in text and "ip address 10.31.2.2/24" in text and "ip 10.31.2.1" in text
    assert "name APP-TIER" in text
    assert "Vlan31020" not in text and "10.99.0.1" not in text      # whole numbers only
    assert "Vlan3103" not in text                                    # other blocks stay out
    assert "old answer" not in text and "NAVIGATOR_STATE" not in str(r)   # OUT/ and state files skipped


def test_search_flat_and_ios_style(proj):
    r = PF.search(proj, "10.24.1.250")
    assert r["matches"] == 0 and "next" in r                         # names are not searched, only text
    r = PF.search(proj, "Vlan310")
    assert r["blocks"][0]["block"].startswith("interface Vlan310\n ip address 10.31.0.1")


def test_regex_and_errors(proj):
    r = PF.search(proj, r"ip address 10\.31\.\d+\.2", regex=True)
    assert r["matches"] == 2
    with pytest.raises(PF.ProjectFilesError, match="not a valid regex"):
        PF.search(proj, "(", regex=True)
    with pytest.raises(PF.ProjectFilesError, match=r"RAW: not in the project. It has: CONFIG/ \(2 files\)"):
        PF.search(proj, "vlan", files=["RAW"])
    with pytest.raises(PF.ProjectFilesError, match="outside the project"):
        PF.listing(proj, ["../.."])


def test_listing_and_overview(proj):
    r = PF.listing(proj)
    assert r["count"] == 2 and all(f["file"].startswith("CONFIG/") for f in r["files"])
    assert PF.overview(proj) == {"CONFIG/": 2, "OUT/": 1}
    assert PF.listing(proj, ["OUT"])["count"] == 1                  # OUT/ only when named


def test_long_block_is_cut_near_the_hit(tmp_path):
    p = tmp_path / "P"
    p.mkdir()
    body = "router bgp 65001\n" + "".join(f"  neighbor 10.0.0.{i} remote-as 65002\n" for i in range(200))
    (p / "r1.cfg").write_text(body)
    r = PF.search(p, "10.0.0.150")
    blk = r["blocks"][0]["block"].splitlines()
    assert blk[0] == "router bgp 65001" and len(blk) <= PF.BLOCK_LINES + 1
    assert any("10.0.0.150 " in x for x in blk)


def test_local_model_reads_every_file_by_default(proj):
    text, used, left = read_files(proj, None, 100000)
    assert left == []
    assert len(used) == 2 and "OUT/old.md" not in used
    with pytest.raises(ModelError, match="RAW: not in the project. It has: CONFIG/"):
        read_files(proj, ["RAW"], 1000)


def test_project_tool_search_and_files(proj, monkeypatch):
    from navigator_engineer import server

    class FakeScope:
        def __init__(self, project, inv, need_inventory=False, host_model=None):
            self.pdir, self.project, self.mode = proj, "DEMO2", "LOCAL"
            self.hidden = host_model == "cloud"
            self.logged = []

        def log(self, a, r):
            self.logged.append((a, r))

    monkeypatch.setattr(server, "Scope", FakeScope)
    fn = getattr(server.project, "fn", server.project)
    r = fn(action="search", project="DEMO2", search="vlan 3102", host_model="local")
    assert "ip address 10.31.2.2/24" in "\n".join(b["block"] for b in r["blocks"])
    r = fn(action="search", project="DEMO2", search="vlan 3102", host_model="cloud")
    assert r["matches"] == 3 and "blocks" not in r and "local_model" in r["next"]
    r = fn(action="files", project="DEMO2", host_model="cloud")
    assert r["folders"] == {"CONFIG/": 2, "OUT/": 1} and "files" not in r
    r = fn(action="files", project="DEMO2", host_model="local")
    assert r["count"] == 2
