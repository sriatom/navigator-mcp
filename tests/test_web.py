"""Copy-friendly Slack approval messages and the read-only change pages."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import urllib.error
import urllib.request

import pytest

from rudder_devices import approval, web
from test_p3 import rig  # noqa: F401
from test_p4 import ch, propose  # noqa: F401


def _slack(ch, monkeypatch, **extra):  # noqa: F811
    cfg = {"METHOD": "slack", "BOT": "navi", "CHANNEL": "C0123ABCDEF", "APPROVAL_DIR": str(ch["dir"]),
           "HERMES_BIN": "/bin/true", **extra}
    chan = approval.HermesChannel(cfg, "NAVI", runner=ch["chan"].run)
    monkeypatch.setattr(ch["c"], "channel", chan)
    monkeypatch.setattr(ch["c"], "s", dataclasses.replace(ch["c"].s, approval={**(ch["c"].s.approval or {}), **cfg}))
    return chan


def test_slack_message_has_copyable_commands(ch, monkeypatch):  # noqa: F811
    _slack(ch, monkeypatch)
    propose(ch)
    msg = ch["sent"][-1]["message"]
    code = re.search(r"`!pushok (\w+)`", msg).group(1)
    assert f"Approve:  `!pushok {code}`" in msg and f"Reject:   `!pushno {code}`" in msg
    assert "/push" not in msg and "@NAVI" not in msg              # one form, copied as-is
    assert "```\n[QALEAF1A]" in msg                              # the diff as a code block
    assert "needs approval" in msg


def test_telegram_message_is_unchanged(ch):  # noqa: F811
    propose(ch)
    msg = ch["sent"][-1]["message"]
    assert re.search(r"Approve:  /pushok \w+\nReject:   /pushno \w+", msg) and "`" not in msg and "@NAVI" not in msg


def test_long_diff_goes_to_the_page(ch, monkeypatch):  # noqa: F811
    _slack(ch, monkeypatch, WEB_BIND="127.0.0.1:18790", MENTION="@Approver")
    from rudder_devices import changes as C
    monkeypatch.setattr(C, "SHORT_DIFF", 10)
    st = propose(ch)
    msg = ch["sent"][-1]["message"]
    assert "too long for this message" in msg and "[QALEAF1A]" not in msg
    assert f"🔗 Full change and its progress: {st['page']}" in msg and st["page"].startswith("http://127.0.0.1:18790/c/")
    t = ch["c"]._load(st["ticket"])
    assert t["web_token"] in st["page"] and t["web_token"] not in (ch["s"].state_path / "audit.log").read_text()


def test_page_renders_masks_and_cannot_act(ch, monkeypatch):  # noqa: F811
    _slack(ch, monkeypatch, WEB_BIND="127.0.0.1:0")
    st = propose(ch, lines=["interface Loopback10", "description rudder-test"], requested_by="U0123ABCDEF|Pat",
                 ticket_id="CHG0012345")
    t = ch["c"]._load(st["ticket"])
    t["devices"][0]["diff"] += "+snmp-server community s3cr3t ro\n+username x secret 5 abc\n"
    P = web.Pages(ch["s"].state_path, 60, "/pushok <code>", "Slack")
    page = P.render(t)
    assert "s3cr3t" not in page and "community ****" in page and "secret 5 ****" in page
    assert "Pat (U0123ABCDEF)" in page and "CHG0012345" in page and "waiting_approval" in page
    assert "<form" not in page and "<script" not in page and 'http-equiv="refresh"' in page
    assert "proposed QALEAF1A" in page                           # progress from the audit log
    assert P.find(t["web_token"])["id"] == st["ticket"] and P.find("x" * 32) is None


def test_page_link_dies_after_the_ticket_ends(ch):  # noqa: F811
    st = propose(ch)
    t = ch["c"]._load(st["ticket"])
    ch["c"].cancel(st["ticket"])
    P = web.Pages(ch["s"].state_path, keep_min=60)
    assert P.find(t["web_token"]) is not None                    # still readable for a while
    p = ch["s"].state_path / "tickets" / f"{st['ticket']}.json"
    old = time.time() - 2 * 3600
    os.utime(p, (old, old))
    assert P.find(t["web_token"]) is None


def test_server_serves_only_ticket_pages(ch, monkeypatch):  # noqa: F811
    _slack(ch, monkeypatch)
    st = propose(ch)
    t = ch["c"]._load(st["ticket"])
    srv = web.serve(ch["s"].state_path, {"WEB_BIND": "127.0.0.1:0"}, "/pushok <code>", "Slack")
    try:
        port = srv.server_address[1]
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/c/{t['web_token']}")
        body = r.read().decode()
        assert r.status == 200 and st["ticket"] in body
        assert "default-src 'none'" in r.headers["Content-Security-Policy"] and r.headers["Cache-Control"] == "no-store"
        for bad in ("/", "/c/" + "a" * 32, "/c/../tickets", f"/c/{t['web_token']}x"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}")
            assert e.value.code == 404
        req = urllib.request.Request(f"http://127.0.0.1:{port}/c/{t['web_token']}", data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 405
    finally:
        srv.shutdown()
    assert web.serve(ch["s"].state_path, {}, "", "") is None    # no WEB_BIND: no server


def test_setup_checks_web_settings(tmp_path, monkeypatch):
    from navigator_engineer import setup as S
    (tmp_path / "hermes" / "profiles" / "navi").mkdir(parents=True)
    monkeypatch.setenv("HERMES_ROOT", str(tmp_path / "hermes"))
    base = {"BOT": "navi"}
    assert S.normalize_approval({**base, "WEB_BIND": "192.0.2.240:8790"})["WEB_BIND"] == "192.0.2.240:8790"
    for bad in ("0.0.0.0:8790", "192.0.2.240", "192.0.2.240:80"):
        with pytest.raises(S.SetupError, match="WEB_BIND"):
            S.normalize_approval({**base, "WEB_BIND": bad})
    with pytest.raises(S.SetupError, match="WEB_URL"):
        S.normalize_approval({**base, "WEB_URL": "javascript:alert(1)"})
    with pytest.raises(S.SetupError, match="MENTION"):
        S.normalize_approval({**base, "MENTION": "NAVI<script>"})
    assert json.dumps(S.normalize_approval({**base, "MENTION": "@NAVI"}))


def _tls_env(tmp_path, monkeypatch):
    from navigator_gate import tls
    monkeypatch.setenv("NAVIGATOR_TLS_DIR", str(tmp_path / "tls"))
    tls.init()
    return tls.files()


def test_scheme_follows_the_certificate(tmp_path, monkeypatch):
    from rudder_devices.changes import web_base
    b = {"WEB_BIND": "127.0.0.1:8790"}
    assert web.scheme(b) == "http" and web_base(b) == "http://127.0.0.1:8790"
    assert web.scheme({**b, "WEB_TLS": "on"}) is None and web_base({**b, "WEB_TLS": "on"}) is None
    _tls_env(tmp_path, monkeypatch)
    assert web.scheme(b) == "https" and web_base(b) == "https://127.0.0.1:8790"
    assert web_base({**b, "WEB_TLS": "off"}) == "http://127.0.0.1:8790"
    assert web_base({**b, "WEB_URL": "https://navhost.lab:8790"}) == "https://navhost.lab:8790"


def test_pages_over_https(ch, monkeypatch, tmp_path):  # noqa: F811
    import ssl
    f = _tls_env(tmp_path, monkeypatch)
    _slack(ch, monkeypatch, WEB_BIND="127.0.0.1:18790")
    st = propose(ch)
    assert ch["c"].page_url(ch["c"]._load(st["ticket"])).startswith("https://127.0.0.1:18790/c/")
    t = ch["c"]._load(st["ticket"])
    srv = web.serve(ch["s"].state_path, {"WEB_BIND": "127.0.0.1:0"}, "/pushok <code>", "Slack")
    try:
        port = srv.server_address[1]
        ctx = ssl.create_default_context(cafile=str(f["ca"]))
        r = urllib.request.urlopen(f"https://127.0.0.1:{port}/c/{t['web_token']}", context=ctx)
        assert r.status == 200 and st["ticket"] in r.read().decode()
        assert r.headers["Strict-Transport-Security"]
        with pytest.raises(urllib.error.URLError):                       # not trusted without the CA
            urllib.request.urlopen(f"https://127.0.0.1:{port}/c/{t['web_token']}")
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):   # plain HTTP is dropped
            urllib.request.urlopen(f"http://127.0.0.1:{port}/c/{t['web_token']}", timeout=5)
        r = urllib.request.urlopen(f"https://127.0.0.1:{port}/c/{t['web_token']}", context=ctx)  # still serving
        assert r.status == 200
    finally:
        srv.shutdown()


def test_setup_checks_web_tls(tmp_path, monkeypatch):
    from navigator_engineer import setup as S
    (tmp_path / "hermes" / "profiles" / "navi").mkdir(parents=True)
    monkeypatch.setenv("HERMES_ROOT", str(tmp_path / "hermes"))
    base = {"BOT": "navi", "WEB_BIND": "127.0.0.1:8790"}
    assert S.normalize_approval(base)["WEB_TLS"] == "auto"
    with pytest.raises(S.SetupError, match="WEB_TLS"):
        S.normalize_approval({**base, "WEB_TLS": "maybe"})
    with pytest.raises(S.SetupError, match="no certificate"):
        S.normalize_approval({**base, "WEB_TLS": "on"})
    _tls_env(tmp_path, monkeypatch)
    assert S.normalize_approval({**base, "WEB_TLS": "on"})["WEB_TLS"] == "on"
    with pytest.raises(S.SetupError, match="not in the certificate"):
        S.normalize_approval({**base, "WEB_BIND": "10.9.9.9:8790"})
    with pytest.raises(S.SetupError, match="http://"):
        S.normalize_approval({**base, "WEB_URL": "http://navhost:8790"})
    assert S.normalize_approval({**base, "WEB_BIND": "10.9.9.9:8790", "WEB_TLS": "off"})["WEB_TLS"] == "off"
