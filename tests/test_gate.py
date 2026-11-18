"""navigator_gate: the local CA, HTTP users, logins and roles for the HTTP MCP servers."""
from __future__ import annotations

import asyncio
import base64
import socket
import stat
import threading
import time

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from navigator_gate import cli, gate, tls, users


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_CONFIG_DIR", str(tmp_path / "cfg"))
    nav = tmp_path / "navigator.yml"
    nav.write_text(f"STATE_PATH: {tmp_path / 'state'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nav))
    return tmp_path


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_tls_init_makes_a_ca_and_a_signed_server_certificate(env):
    r = tls.init(["navi.lab"])
    f = tls.files()
    assert set(r["made"]) == {"CA", "server certificate"}
    assert _mode(f["ca_key"]) == 0o600 and _mode(f["key"]) == 0o600
    ca = x509.load_pem_x509_certificate(f["ca"].read_bytes())
    cert = x509.load_pem_x509_certificate(f["cert"].read_bytes())
    ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert "navi.lab" in r["names"] and "127.0.0.1" in r["names"] and "localhost" in r["names"]
    assert tls.init()["made"] == []                                 # nothing new the second time
    old = f["cert"].read_bytes()
    r2 = tls.init(renew=True)
    assert r2["made"] == ["server certificate"] and f["cert"].read_bytes() != old
    assert r2["ca_sha256"] == r["ca_sha256"]                         # same CA: clients keep trusting it


def test_users_passwords_are_random_hashed_and_private(env):
    pw = users.add("pat", "admin")
    assert len(pw) == 32 and "." not in pw
    assert users.verify("pat", pw) == "admin"
    assert users.verify("pat", pw + "x") is None and users.verify("nobody", pw) is None
    text = users.users_file().read_text()
    assert pw not in text and "scrypt$" in text and _mode(users.users_file()) == 0o600
    pw2 = users.reset("pat")
    assert pw2 != pw and users.verify("pat", pw) is None and users.verify("pat", pw2) == "admin"
    users.set_role("pat", "engineer")
    assert users.verify("pat", pw2) == "engineer"
    for bad in (("Bad Name", "admin"), ("x", "admin"), ("ok_name", "root")):
        with pytest.raises(users.UserError):
            users.add(*bad)
    with pytest.raises(users.UserError):
        users.add("pat", "admin")
    users.remove("pat")
    assert users.listing() == []


def test_login_header_forms_cache_and_block():
    calls = []

    def verify(name, pw):
        calls.append(name)
        return "engineer" if (name, pw) == ("eng", "good") else None
    now = [1000.0]
    lg = gate.Logins(verify=verify, clock=lambda: now[0])
    assert lg.check("Bearer eng.good", "1.1.1.1") == ("eng", "engineer")
    basic = "Basic " + base64.b64encode(b"eng:good").decode()
    assert lg.check(basic, "1.1.1.1") == ("eng", "engineer")
    n = len(calls)
    assert lg.check("Bearer eng.good", "1.1.1.1") == ("eng", "engineer") and len(calls) == n     # cached
    assert lg.check("", "1.1.1.1") == "missing" and lg.check("Token x", "1.1.1.1") == "missing"
    for _ in range(gate.FAIL_MAX):
        assert lg.check("Bearer eng.bad", "2.2.2.2") == "wrong"
    assert lg.check("Bearer eng.good", "2.2.2.2") == "blocked"      # even the right password, for a while
    assert lg.check("Bearer eng.good", "3.3.3.3") == ("eng", "engineer")
    now[0] += gate.BLOCK_SEC + 1
    assert lg.check("Bearer eng.good", "2.2.2.2") == ("eng", "engineer")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(app, port, **ssl):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on", **ssl))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, th


def _toy():
    from fastmcp import FastMCP
    m = FastMCP("toy")

    @m.tool()
    def inventory(action: str) -> dict:
        return {"ok": action}

    @m.tool()
    def inventory_edit(action: str) -> dict:
        return {"edited": action}
    return m


def test_roles_over_http(env):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.exceptions import ToolError
    adm, eng = users.add("boss", "admin"), users.add("eng", "engineer")
    port = _free_port()
    srv, th = _run(gate.build(_toy(), "NAVIGATOR"), port)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        assert httpx.post(url, json={}).status_code == 401
        assert httpx.post(url, json={}, headers={"Authorization": f"Bearer eng.{adm}"}).status_code == 401

        async def go(user, pw):
            async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {user}.{pw}"})) as c:
                names = sorted(t.name for t in await c.list_tools())
                ok = (await c.call_tool("inventory", {"action": "list"})).data
                try:
                    ed = (await c.call_tool("inventory_edit", {"action": "x"})).data
                except ToolError as e:
                    ed = str(e)
                return names, ok, ed
        names, ok, ed = asyncio.run(go("boss", adm))
        assert names == ["inventory", "inventory_edit"] and ok == {"ok": "list"} and ed == {"edited": "x"}
        names, ok, ed = asyncio.run(go("eng", eng))
        assert names == ["inventory"] and ok == {"ok": "list"} and "needs the admin role" in ed
    finally:
        srv.should_exit = True
        th.join(5)
    log = (env / "state" / "http-access.log").read_text()
    assert '"user": "eng"' in log and '"result": "denied"' in log and adm not in log and eng not in log


def test_https_with_the_local_ca(env):
    tls.init()
    users.add("boss", "admin")
    f = tls.files()
    port = _free_port()
    srv, th = _run(gate.build(_toy(), "ATLAS"), port, ssl_certfile=str(f["cert"]), ssl_keyfile=str(f["key"]))
    try:
        r = httpx.post(f"https://127.0.0.1:{port}/mcp", json={}, verify=str(f["ca"]))
        assert r.status_code == 401                                   # TLS verified against the CA, then the login
        with pytest.raises(httpx.ConnectError):
            httpx.post(f"https://127.0.0.1:{port}/mcp", json={})      # not trusted without the CA
    finally:
        srv.should_exit = True
        th.join(5)


def test_serve_refuses_unsafe_starts(env):
    with pytest.raises(gate.GateError, match="no HTTP users"):
        gate.serve(_toy(), "NAVIGATOR")
    users.add("boss", "admin")
    with pytest.raises(gate.GateError, match="no TLS certificate"):
        gate.serve(_toy(), "NAVIGATOR")
    with pytest.raises(gate.GateError, match="only on 127.0.0.1"):
        gate.serve(_toy(), "NAVIGATOR", "0.0.0.0:8791", no_tls=True)


def test_cli_prints_the_password_once_and_logs_no_secret(env):
    lines = []
    assert cli.main(["user", "add", "pat", "--role", "admin"], out=lines.append) == 0
    pw = next(ln.strip() for ln in "\n".join(lines).splitlines() if len(ln.strip()) == 32)
    assert users.verify("pat", pw) == "admin"
    assert pw not in (env / "state" / "settings.log").read_text()
    lines.clear()
    assert cli.main(["http", "show"], out=lines.append) == 0
    text = "\n".join(lines)
    assert ":8791/mcp" in text and ":8792/mcp" in text and ":8793/mcp" in text and pw not in text
