"""ATLAS link: the server-to-server door from layer 2 (RUDDER) to layer 1 (ATLAS).

A Unix socket (mode 600, in a mode-700 folder). One JSON request per connection, one JSON reply.
Every request carries the client's token; each client may act only for the consumers listed for
it. This is the ONLY way a decrypted credential leaves ATLAS, and it never goes to a model.

bootstrap.yml:
    LINK:
      SOCKET: ~/.atlas/link.sock                 # default
      CLIENTS:
        RUDDER:
          TOKEN_FILE: ~/.config/rudder/atlas-link.token
          CONSUMERS: [NAVI, LAB_BOT]               # who RUDDER may ask for

Requests ({"op": ..., "token": ..., ...}):
    hello                                    -> {client, consumers}
    records   consumer inventory [names groups filters include_disabled record_type]
    platform  name                           -> catalogue entry
    commands  platform                       -> {category: [commands]}
    credential consumer tag                  -> {fields}   (decrypted; never logged)

Run:  atlas-link serve            atlas-link token RUDDER   (creates the token file, mode 600)
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

from . import catalog as catalog_mod
from . import config, consumers, inventory, store, vault

log = logging.getLogger("atlas_inventory.link")
MAX_REQUEST = 64 * 1024


class LinkError(Exception):
    pass


def _expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def settings(cfg: config.Config) -> tuple[Path, dict]:
    raw = config.raw_bootstrap(cfg).get("LINK") or {}
    sock = _expand(raw.get("SOCKET") or cfg.state_path / "link.sock")
    clients = {}
    for name, c in (raw.get("CLIENTS") or {}).items():
        c = c or {}
        clients[str(name).upper()] = {"token_file": _expand(c.get("TOKEN_FILE") or
                                                            f"~/.config/{str(name).lower()}/atlas-link.token"),
                                      "consumers": [str(x).upper() for x in c.get("CONSUMERS") or []]}
    return sock, clients


def _client_for(token: str, clients: dict) -> tuple[str, dict]:
    for name, c in clients.items():
        try:
            want = c["token_file"].read_text().strip()
        except OSError:
            continue
        if want and hmac.compare_digest(want.encode(), str(token).encode()):
            return name, c
    raise LinkError("unknown token")


def handle(req: dict) -> dict:
    """One request -> one reply. Config is re-read each time so allow-list changes apply at once."""
    cfg = config.load()
    _, clients = settings(cfg)
    name, client = _client_for(req.get("token", ""), clients)
    op = req.get("op")
    if op == "hello":
        return {"client": name, "consumers": client["consumers"]}
    if op in ("platform", "commands", "platforms"):
        cat = catalog_mod.Catalog(cfg.catalog_path)
        if op == "platform":
            return {"platform": cat.platform(req.get("name"))}
        if op == "platforms":
            return {"platforms": [{"ID": k, "vendor": v.get("vendor"), "reads": v.get("reads"),
                                   "writes": v.get("writes")} for k, v in sorted(cat.platforms.items())]}
        return {"commands": cat.commands_for(req.get("platform"))}
    who = str(req.get("consumer") or "").upper()
    if who not in client["consumers"]:
        raise LinkError(f"client {name} may not act for consumer {who or '(none)'}")
    if op == "records":
        path = inventory.resolve(cfg.data_path, str(req.get("inventory") or ""))
        if not consumers.allowed_inventory(cfg, path.stem, who):
            raise LinkError(f"inventory {path.stem} is not available to consumer {who}")
        recs = inventory.load_all(path)
        rts = ["DEVICE", "CONTROLLER", "OOB"] if str(req.get("record_type", "DEVICE")).upper() == "ALL" \
            else [str(req.get("record_type", "DEVICE")).upper()]
        pool = [r for rt in rts for r in recs.get(rt, [])]
        sel = inventory.select(pool, names=req.get("names"), groups=req.get("groups"),
                               filters=req.get("filters"), include_disabled=bool(req.get("include_disabled")))
        out = []
        for r in sel:
            r = dict(r)
            r["PRODUCTION"] = inventory.is_production(r)
            if r.get("CREDENTIAL") and not consumers.allowed_tag(cfg, str(r["CREDENTIAL"]), who):
                r["CREDENTIAL"] = None
            out.append(r)
        return {"inventory": path.stem, "records": out}
    if op == "credential":
        cred = vault.get_credential(cfg, str(req.get("tag") or ""), consumer=who)
        return {"fields": dict(cred)}
    raise LinkError(f"unknown op {op!r}")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(MAX_REQUEST)
            req = json.loads(line or b"{}")
            reply = {"ok": True, **handle(req)}
        except (LinkError, inventory.InventoryError, catalog_mod.CatalogError, vault.VaultError,
                PermissionError) as e:
            reply = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001   (never echo request content: it holds the token)
            log.exception("link request failed")
            reply = {"ok": False, "error": f"internal error ({type(e).__name__})"}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(sock_path: Path) -> Server:
    store.ensure_dir(sock_path.parent)
    if sock_path.exists():
        try:                                        # refuse to steal a live socket
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(sock_path))
            s.close()
            raise LinkError(f"{sock_path} is in use by another atlas-link")
        except (ConnectionRefusedError, FileNotFoundError):
            sock_path.unlink()
    old = os.umask(0o177)
    try:
        srv = Server(str(sock_path), _Handler)
    finally:
        os.umask(old)
    os.chmod(sock_path, 0o600)
    return srv


def serve_in_thread(sock_path: Path) -> Server:
    srv = serve(sock_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_token(path: Path) -> Path:
    store.ensure_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (secrets.token_urlsafe(32) + "\n").encode())
    finally:
        os.close(fd)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas-link", description="ATLAS server-to-server socket for RUDDER")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    t = sub.add_parser("token", help="create a client's token file (never overwrites)")
    t.add_argument("client")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = config.load()
    sock, clients = settings(cfg)
    if a.cmd == "token":
        c = clients.get(a.client.upper())
        if not c:
            print(f"error: no LINK.CLIENTS.{a.client.upper()} in bootstrap.yml", file=sys.stderr)
            return 1
        if c["token_file"].exists():
            print(f"{c['token_file']} exists already; not overwritten")
            return 0
        print(f"created {make_token(c['token_file'])} (mode 600)")
        return 0
    if a.cmd == "status":
        print(f"socket {sock}: {'present' if sock.exists() else 'not running'}")
        for n, c in clients.items():
            print(f"client {n}: token {'present' if c['token_file'].exists() else 'MISSING'}, "
                  f"consumers {', '.join(c['consumers']) or 'none'}")
        return 0
    srv = serve(sock)
    log.info("atlas-link listening on %s (%d client(s))", sock, len(clients))

    def _stop(signum, frame):                      # systemd / kill send SIGTERM: stop cleanly
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        sock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
