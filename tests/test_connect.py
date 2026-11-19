"""navigator-setup connect: Hermes profile edits, the Cursor link, idempotence."""
import base64
import json
from pathlib import Path

import pytest
import yaml

from navigator_engineer import connect

REPO = Path(__file__).resolve().parent.parent

EXISTING = """# my profile
model:
  default: some-model   # keep this comment
mcp_servers:
  other:
    command: /usr/bin/other-mcp
plugins:
  enabled:
    - navigator-guard
display:
  compact: true
"""


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    h = tmp_path / "hermes"
    (h / "profiles" / "navigator").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("NAVIGATOR_HOME", str(REPO))
    monkeypatch.setattr(connect, "_restart", lambda out, no: out.append("gateway: (test)"))
    return h


def _run(**kw):
    seen = []
    out = connect.hermes(reply_profiles=lambda p, o: seen.append(p), **kw)
    return out, seen


def test_hermes_on_an_existing_profile_keeps_everything_else(hermes_home):
    pdir = hermes_home / "profiles" / "navigator"
    (pdir / "config.yaml").write_text(EXISTING)
    (pdir / "SOUL.md").write_text("my own soul\n")
    out, seen = _run()
    text = (pdir / "config.yaml").read_text()
    d = yaml.safe_load(text)
    assert d["mcp_servers"]["other"]["command"] == "/usr/bin/other-mcp"
    assert d["mcp_servers"]["navigator"]["command"].endswith("/navigator-mcp")
    assert d["mcp_servers"]["navigator"]["env"]["NAVIGATOR_TRUST_HOST_MODEL"] == "1"
    assert d["plugins"]["enabled"] == ["navigator-guard", "navigator-reply", "navigator-qc"]
    assert d["display"] == {"compact": True} and "# keep this comment" in text
    assert (pdir / "plugins" / "navigator-qc" / "__init__.py").is_file()
    assert (pdir / "plugins" / "navigator-guard" / "__init__.py").is_file()
    assert (pdir / "SOUL.md").read_text() == "my own soul\n" and (pdir / "SOUL.navigator.md").is_file()
    assert list(pdir.glob("config.yaml.bak-*")) and seen == ["navigator"]
    assert any("other MCP servers" in x for x in out)
    # again: nothing changes, no new backup
    before = (pdir / "config.yaml").read_text()
    out2, _ = _run()
    assert (pdir / "config.yaml").read_text() == before and "config.yaml: already set" in out2
    assert len(list(pdir.glob("config.yaml.bak-*"))) == 1


def test_hermes_on_an_empty_profile_and_the_approval_bot(hermes_home):
    (hermes_home / "profiles" / "approvals").mkdir(parents=True)
    out, _ = _run(approvals="approvals")
    d = yaml.safe_load((hermes_home / "profiles/navigator/config.yaml").read_text())
    assert d["plugins"]["enabled"] == ["navigator-guard", "navigator-reply", "navigator-qc"]
    assert "navigator" in d["mcp_servers"]
    g = json.loads((hermes_home / "profiles/navigator/navigator-guard.json").read_text())
    assert g["always_local"] is False and g["navigator_bootstrap"].endswith("navigator.yml")
    assert "STRICT" in (hermes_home / "profiles/navigator/SOUL.md").read_text() or \
        (hermes_home / "profiles/navigator/SOUL.md").stat().st_size > 1000
    a = yaml.safe_load((hermes_home / "profiles/approvals/config.yaml").read_text())
    assert a == {"plugins": {"enabled": ["push-approve"]}}
    assert (hermes_home / "profiles/approvals/plugins/push-approve/__init__.py").is_file()
    assert (hermes_home / "profiles/approvals/SOUL.md").is_file()


@pytest.mark.parametrize("cfg", ["mcp_servers: {}\nplugins:\n  enabled: []\n", "plugins: {}\n"])
def test_inline_empty_sections(hermes_home, cfg):
    p = hermes_home / "profiles/navigator/config.yaml"
    p.write_text(cfg)
    _run()
    d = yaml.safe_load(p.read_text())
    assert "navigator" in d["mcp_servers"]
    assert d["plugins"]["enabled"] == ["navigator-guard", "navigator-reply", "navigator-qc"]


def test_no_hermes_no_profile(hermes_home, monkeypatch):
    monkeypatch.setattr(connect, "_hermes_bin", lambda: None)
    with pytest.raises(connect.ConnectError, match="Hermes is not installed"):
        _run(profile="other")


def test_cursor_link_and_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = connect.cursor("me@navhost")
    link = next(x for x in out if x.startswith("cursor://"))
    cfg = json.loads(base64.b64decode(link.split("config=")[1]))
    assert link.startswith("cursor://anysphere.cursor-deeplink/mcp/install?name=navigator&config=")
    assert cfg["command"] == "ssh" and cfg["args"][3] == "me@navhost"
    assert cfg["args"][4].startswith("NAVIGATOR_BOOTSTRAP=") and "exec " in cfg["args"][4]
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text('{"mcpServers": {"x": {"command": "y"}}}')
    connect.cursor(write=True)
    d = json.loads((tmp_path / ".cursor/mcp.json").read_text())
    assert d["mcpServers"]["x"] == {"command": "y"} and d["mcpServers"]["navigator"]["command"].endswith("navigator-mcp")
    assert "already set" in connect.cursor(write=True)[0]


def test_claude_lines():
    out = connect.claude("me@navhost")
    assert any(x.strip().startswith("claude mcp add navigator --scope user -e NAVIGATOR_BOOTSTRAP=") for x in out)
    assert any("ssh -T -o BatchMode=yes me@navhost" in x for x in out)


def test_install_script_is_valid_bash():
    import subprocess
    r = subprocess.run(["bash", "-n", str(REPO / "scripts" / "install.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
