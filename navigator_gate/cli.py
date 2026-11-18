"""navigator-setup tls ... / user ... / http ...: the HTTPS certificate, the HTTP users and the client settings.

  navigator-setup tls init [--name NAME ...]   make the local CA (once) and the server certificate
  navigator-setup tls renew [--name NAME ...]  a new server certificate, same CA (clients keep trusting it)
  navigator-setup tls show
  navigator-setup user add NAME [--role engineer|admin]    prints the new password ONCE
  navigator-setup user reset NAME                           a new password, printed once
  navigator-setup user role NAME ROLE
  navigator-setup user remove NAME
  navigator-setup user list
  navigator-setup http show                    the URLs, ports and how to connect Claude Code / Cursor / Hermes
"""
from __future__ import annotations

import argparse
import getpass
import socket
import sys
import time

from . import tls, users
from .paths import http_settings, state_dir

SERVERS = (("NAVIGATOR", "navigator-mcp", "the front door: every tool"),
           ("ATLAS", "atlas-mcp", "ATLAS on its own: inventories"),
           ("RUDDER", "rudder-mcp", "RUDDER on its own: device reads and changes"))


def _log(line: str) -> None:
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(d / "settings.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{getpass.getuser()}\t{line}\n")
    except OSError:
        pass


def _show_password(name: str, pw: str, out=print) -> None:
    out(f"\nUser {name}: password (shown once, not stored anywhere in clear text):\n\n    {pw}\n")
    out(f"Clients send:  Authorization: Bearer {name}.<password>")
    out("Keep it in your password manager. Lost: navigator-setup user reset " + name + "\n")


def tls_show(out=print) -> None:
    i = tls.info()
    if not i.get("ready"):
        out(f"TLS: not set up ({i['dir']}): navigator-setup tls init")
        return
    out(f"CA file (give it to clients): {i['ca_file']}")
    out(f"CA: {i['ca_subject']}, expires {i['ca_expires']}")
    out(f"CA SHA-256: {i['ca_sha256']}")
    out(f"server certificate: {i['cert_file']}, expires {i['cert_expires']} ({i['days_left']} days)")
    out(f"names in it: {', '.join(i['names'])}")


def http_show(out=print) -> None:
    h = http_settings()
    host = socket.getfqdn() or socket.gethostname()
    out(f"bind {h['BIND']} (navigator.yml HTTP.BIND)")
    for key, prog, what in SERVERS:
        out(f"  {prog:14} https://{host}:{h['PORTS'][key]}/mcp   {what}   (systemd: {prog}-https)")
    tls_show(out)
    us = users.listing()
    out("users: " + (", ".join(f"{u['user']} ({u['role']})" for u in us) if us else
                     "none yet: navigator-setup user add <name> --role admin"))
    port = h["PORTS"]["NAVIGATOR"]
    ca = tls.files()["ca"]
    out(f"""
Connect (copy {ca} to the client machine first):
  Claude Code:
    claude mcp add --transport http navigator https://{host}:{port}/mcp --header "Authorization: Bearer <user>.<password>"
    and start Claude Code with NODE_EXTRA_CA_CERTS=<path to ca.pem>
  Cursor (~/.cursor/mcp.json), with NODE_EXTRA_CA_CERTS set the same way:
    {{"mcpServers": {{"navigator": {{"url": "https://{host}:{port}/mcp",
                                  "headers": {{"Authorization": "Bearer <user>.<password>"}}}}}}}}
  Hermes on another machine (config.yaml mcp_servers), with SSL_CERT_FILE=<path to ca.pem>:
    navigator: {{url: "https://{host}:{port}/mcp", headers: {{Authorization: "Bearer <user>.<password>"}}}}
  Check:  curl --cacert ca.pem https://{host}:{port}/mcp   -> 401 (login required) means TLS works""")


def main(argv: list[str], out=print) -> int:
    ap = argparse.ArgumentParser(prog="navigator-setup", description="HTTPS and users of the HTTP MCP servers")
    sub = ap.add_subparsers(dest="area", required=True)
    t = sub.add_parser("tls").add_subparsers(dest="cmd", required=True)
    for c in ("init", "renew"):
        x = t.add_parser(c)
        x.add_argument("--name", action="append", default=[], help="another DNS name or IP for the certificate")
    t.add_parser("show")
    u = sub.add_parser("user").add_subparsers(dest="cmd", required=True)
    x = u.add_parser("add")
    x.add_argument("name")
    x.add_argument("--role", default="engineer", choices=users.ROLES)
    for c in ("reset", "remove"):
        u.add_parser(c).add_argument("name")
    x = u.add_parser("role")
    x.add_argument("name")
    x.add_argument("role", choices=users.ROLES)
    u.add_parser("list")
    sub.add_parser("http").add_subparsers(dest="cmd", required=True).add_parser("show")
    a = ap.parse_args(argv)
    try:
        if a.area == "tls":
            if a.cmd == "show":
                tls_show(out)
            else:
                r = tls.init(a.name, renew=a.cmd == "renew")
                _log(f"TLS {a.cmd}: made {', '.join(r['made']) or 'nothing'}; names {', '.join(r.get('names', []))}")
                out(f"made: {', '.join(r['made']) or 'nothing (already there; tls renew for a new certificate)'}")
                tls_show(out)
                if r["made"]:
                    out("Now: systemctl --user restart rudder-scheduler (change pages, 8790) and the *-mcp-https "
                        "units you use")
        elif a.area == "user":
            if a.cmd == "add":
                pw = users.add(a.name, a.role)
                _log(f"HTTP user added: {a.name.lower()} ({a.role})")
                _show_password(a.name.lower(), pw, out)
            elif a.cmd == "reset":
                pw = users.reset(a.name)
                _log(f"HTTP user password reset: {a.name}")
                _show_password(a.name, pw, out)
                out("Running servers accept the old password for up to 10 more minutes (login cache).")
            elif a.cmd == "role":
                users.set_role(a.name, a.role)
                _log(f"HTTP user role: {a.name} -> {a.role}")
                out(f"{a.name}: {a.role} (running servers: within 10 minutes, or restart them)")
            elif a.cmd == "remove":
                users.remove(a.name)
                _log(f"HTTP user removed: {a.name}")
                out(f"{a.name} removed (running servers: within 10 minutes, or restart them)")
            else:
                for r in users.listing() or [{"user": "(none)", "role": "", "created": ""}]:
                    out(f"{r['user']:20} {r['role']:9} created {r['created'] or '-'}")
        else:
            http_show(out)
    except (tls.TlsError, users.UserError) as e:
        print(f"navigator-setup: {e}", file=sys.stderr)
        return 2
    return 0
