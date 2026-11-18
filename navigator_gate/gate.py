"""Serve an MCP server over HTTPS with logins and roles.

Every HTTP request needs `Authorization: Bearer <user>.<password>` or HTTP Basic. Ten failed logins from one
address in five minutes block that address for five minutes. Each tool call is logged (time, server, user,
role, address, tool, result; never arguments) in <STATE_PATH>/http-access.log. Engineers do not see, and
cannot call, the tools that commit inventory changes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from pathlib import Path

from . import tls, users
from .paths import http_settings, state_dir

# tools an engineer may not use, per server (admin: everything)
ENGINEER_DENY = {"NAVIGATOR": {"inventory_edit"}, "ATLAS": {"interview_commit"}, "RUDDER": set()}
FAIL_MAX, FAIL_WINDOW, BLOCK_SEC = 10, 300, 300
CACHE_SEC = 600


class GateError(Exception):
    pass


class Logins:
    """Password checks with a short cache (scrypt is slow on purpose) and a per-address block."""

    def __init__(self, verify=users.verify, clock=time.time):
        self.verify, self.clock = verify, clock
        self.cache: dict[str, tuple[float, str, str]] = {}
        self.fails: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}
        self.lock = threading.Lock()

    @staticmethod
    def parse(header: str) -> tuple[str, str] | None:
        h = (header or "").strip()
        if h[:7].lower() == "bearer ":
            tok = h[7:].strip()
            name, _, pw = tok.partition(".")
        elif h[:6].lower() == "basic ":
            try:
                name, _, pw = base64.b64decode(h[6:].strip()).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
        else:
            return None
        return (name, pw) if name and pw else None

    def check(self, header: str, addr: str) -> tuple[str, str] | str:
        """(user, role), or a reason string: 'blocked' / 'missing' / 'wrong'."""
        now = self.clock()
        with self.lock:
            if self.blocked.get(addr, 0) > now:
                return "blocked"
        cred = self.parse(header)
        if not cred:
            return "missing"
        key = hashlib.sha256(header.encode()).hexdigest()
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit[0] > now:
                return hit[1], hit[2]
        role = self.verify(*cred)
        with self.lock:
            if role:
                self.cache[key] = (now + CACHE_SEC, cred[0], role)
                self.fails.pop(addr, None)
                return cred[0], role
            f = [t for t in self.fails.get(addr, []) if t > now - FAIL_WINDOW] + [now]
            self.fails[addr] = f
            if len(f) >= FAIL_MAX:
                self.blocked[addr] = now + BLOCK_SEC
                self.fails.pop(addr, None)
        return "wrong"


def _log(line: dict) -> None:
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(d / "http-access.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **line}) + "\n")
    except OSError:
        pass


class AuthApp:
    """ASGI wrapper: rejects HTTP requests without a valid login; puts (user, role) in scope['state']."""

    def __init__(self, app, server: str, logins: Logins | None = None):
        self.app, self.server, self.logins = app, server, logins or Logins()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        addr = (scope.get("client") or ("?", 0))[0]
        res = self.logins.check(headers.get("authorization", ""), addr)
        if isinstance(res, str):
            if res != "missing":
                _log({"server": self.server, "addr": addr, "login": res})
            code = 429 if res == "blocked" else 401
            body = json.dumps({"error": "too many failed logins; wait 5 minutes" if code == 429 else
                               "login required: Authorization: Bearer <user>.<password>"}).encode()
            await send({"type": "http.response.start", "status": code,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b'Bearer realm="navigator", Basic realm="navigator"')]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})
        scope["state"]["navigator_user"] = res
        return await self.app(scope, receive, send)


def _who() -> tuple[str, str, str]:
    from fastmcp.server.dependencies import get_http_request
    try:
        req = get_http_request()
    except RuntimeError:
        return "stdio", "admin", "-"
    u = (req.scope.get("state") or {}).get("navigator_user") or ("?", "")
    return u[0], u[1], (req.client.host if req.client else "?")


def role_middleware(server: str):
    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware

    deny = ENGINEER_DENY.get(server.upper(), set())

    class Roles(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            _, role, _ = _who()
            return tools if role == "admin" else [t for t in tools if t.name not in deny]

        async def on_call_tool(self, context, call_next):
            user, role, addr = _who()
            name = context.message.name
            if role != "admin" and (name in deny or not role):
                _log({"server": server, "user": user, "role": role, "addr": addr, "tool": name, "result": "denied"})
                raise ToolError(f"{name}: needs the admin role (you are {user}, {role or 'no role'})")
            try:
                out = await call_next(context)
            except Exception:
                _log({"server": server, "user": user, "role": role, "addr": addr, "tool": name, "result": "error"})
                raise
            _log({"server": server, "user": user, "role": role, "addr": addr, "tool": name, "result": "ok"})
            return out

    return Roles()


def build(mcp, server: str, logins: Logins | None = None):
    """The ASGI app: FastMCP's streamable HTTP at /mcp, roles inside, logins in front."""
    mcp.add_middleware(role_middleware(server))
    return AuthApp(mcp.http_app(path="/mcp"), server.upper(), logins)


def serve(mcp, server: str, hostport: str | None = None, no_tls: bool = False) -> None:
    """Run one server over HTTPS (default port from navigator.yml HTTP.PORTS)."""
    import uvicorn
    server = server.upper()
    h = http_settings()
    if hostport:
        host, _, port = hostport.rpartition(":")
        host, port = host or h["BIND"], int(port)
    else:
        host, port = h["BIND"], h["PORTS"][server]
    if not users.listing():
        raise GateError("no HTTP users yet: navigator-setup user add <name> --role admin")
    ssl = {}
    if no_tls:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise GateError("--no-tls only on 127.0.0.1 (a password must never cross the network in clear text)")
    else:
        f = tls.files()
        if not (Path(f["cert"]).is_file() and Path(f["key"]).is_file()):
            raise GateError("no TLS certificate yet: navigator-setup tls init")
        ssl = {"ssl_certfile": str(f["cert"]), "ssl_keyfile": str(f["key"])}
    app = build(mcp, server)
    print(f"{server.lower()}-mcp: {'http' if no_tls else 'https'}://{host}:{port}/mcp (logins required)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="on", proxy_headers=False, **ssl)
