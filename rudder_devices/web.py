"""Read-only web pages for change tickets, served by rudder-scheduler (no extra service).

  APPROVAL.WEB_BIND   "192.0.2.240:8790" (host:port to listen on; empty = no pages)
  APPROVAL.WEB_URL    the base the links use, if not <scheme>://<WEB_BIND> (e.g. a Tailscale or proxy name)
  APPROVAL.WEB_TLS    auto (default): HTTPS when NAVIGATOR's local certificate exists (navigator-setup tls init,
                      the same CA as the MCP servers on 8791-8793), else HTTP | on: HTTPS or no pages | off: HTTP
  APPROVAL.PAGES_KEEP_DAYS  how long a finished ticket and its page are kept (default 90; retention.py
                         removes them in the daily clean-up). Replaces the old WEB_KEEP_MIN.

GET /c/<token> shows one ticket: what, who asked, devices, the full diff per device, the lines, the
approval and apply progress (ticket log + audit lines), and refreshes itself while the ticket is open.
The token is random per ticket (in the ticket file, never in the audit log); anything else is 404.
Nothing on the page can approve, change or cancel: the code is never stored, approving happens only on
the approval channel. Secret-looking values in the diff (password, secret, key, community...) are masked.
"""
from __future__ import annotations

import html
import json
import logging
import re
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("rudder.web")
OPEN = ("waiting_approval", "approved", "applying")
TOKEN_RE = re.compile(r"^/c/([A-Za-z0-9_-]{20,64})/?$")
SECRET_RE = re.compile(r"(?i)\b(password|secret|key-string|key|community|pre-shared-key|psk|token|"
                       r"md5|sha\d*|authentication-key|auth-key)(\s+(?:\d\s+)?)(\S+)")
STYLE = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6b6b70;--card:#f5f5f7;--line:#e2e2e6;--add:#e7f6ec;--addf:#116329;
--del:#fdecec;--delf:#a40e26;--acc:#0b5cad}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mut:#9a9aa2;--card:#1e1e22;--line:#2e2e34;
--add:#12301d;--addf:#7ee2a0;--del:#3a1518;--delf:#ff9aa6;--acc:#7cb7ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.mut{color:var(--mut)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:13px;background:var(--card);
border:1px solid var(--line)}.ok{color:var(--addf)}.bad{color:var(--delf)}
table{border-collapse:collapse;width:100%}td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td:first-child{color:var(--mut);width:160px;white-space:nowrap}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;
font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;margin:0}
.a{background:var(--add);color:var(--addf);display:block}.d{background:var(--del);color:var(--delf);display:block}
.box{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
code{font:13px ui-monospace,Consolas,monospace}
"""


def mask(text: str) -> str:
    return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", text)


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else ""


def _diff_html(diff: str) -> str:
    out = []
    for ln in mask(diff or "").splitlines():
        cls = "a" if ln.startswith("+") else "d" if ln.startswith("-") else ""
        out.append(f'<span class="{cls}">{_e(ln) or " "}</span>' if cls else _e(ln))
    return "<pre>" + "\n".join(out) + "</pre>"


class Pages:
    def __init__(self, state_path: Path, keep_min: float = 60, how: str = "/pushok <code>",
                 where: str = "the approval channel"):
        self.tdir = state_path / "tickets"
        self.audit = state_path / "audit.log"
        self.keep = float(keep_min) * 60
        self.how, self.where = how, where

    def find(self, token: str) -> dict | None:
        for p in self.tdir.glob("C*.json"):
            try:
                t = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if t.get("web_token") and t["web_token"] == token:
                if t.get("state") not in OPEN and time.time() - p.stat().st_mtime > self.keep:
                    return None                              # ended long enough ago: the link is dead
                if t.get("state") in OPEN and t.get("expires", 0) + self.keep < time.time():
                    return None
                return t
        return None

    def _audit(self, tid: str) -> list[str]:
        try:
            return [ln.split("\t", 3)[0] + "  " + ln.split("\t", 3)[3]
                    for ln in self.audit.read_text().splitlines() if f"\t{tid}\t" in ln]
        except (OSError, IndexError):
            return []

    def render(self, t: dict) -> str:
        st = t.get("state", "")
        cls = "ok" if st == "applied" else "bad" if st in ("failed", "rejected", "expired", "cancelled",
                                                           "interrupted") else ""
        who = t.get("requested_by") or {}
        who = f"{who.get('name', '')} ({who.get('id', '')})".replace(" ()", "") if who else "-"
        rows = [("State", f'<span class="pill {cls}">{_e(st)}</span>'),
                ("Requested by", _e(who)), ("Created", _e(_when(t.get("created")))),
                ("Inventory", _e(t.get("inventory"))),
                ("Devices", _e(", ".join(f"{d['device']} ({d.get('platform', '')})" for d in t.get("devices", []))))]
        for k, label in (("ticket_id", "Ticket ID"), ("project", "Project"), ("decided_by", "Decided by")):
            if t.get(k):
                rows.append((label, _e(t[k])))
        if t.get("bypass"):
            rows.append(("Approval", "none needed: " + _e(t["bypass"].get("why"))))
        sc = t.get("schedule")
        if sc:
            rows.append(("Scheduled", _e(f"{sc.get('at_text')} ({sc.get('tz')}), {sc.get('state')}")))
        if st in ("waiting_approval",):
            nxt = (f"Waiting for approval on {_e(self.where)}: <code>{_e(self.how)}</code> with the code from the "
                   f"approval message. This page cannot approve.")
        elif st == "approved":
            nxt = "Approved: RUDDER applies it within seconds."
        elif st == "applying":
            nxt = "Being applied now."
        else:
            nxt = f"Finished: {_e(st)}."
        v = t.get("validation")
        if v:
            rows.append(("Validation", _e(f"{v.get('validator')}: {str(v.get('status', '')).upper()}: "
                                          f"{mask(v.get('summary', ''))}")))
        for d in t.get("devices", []):
            if d.get("validation"):
                r = d["validation"]
                rows.append((f"After push ({_e(d['device'])})", _e(f"{str(r.get('status', '')).upper()}: "
                                                                  f"{mask(r.get('summary', ''))}")))
        f = t.get("failure") or {}
        if f.get("kind") == "validation":
            rows.append(("Attention", "<span class='bad'>needs a person</span>"))
            nxt = ("<p class='bad'><b>FAILED validation before the push.</b> Nothing was changed on any device "
                   "and no approval was opened.</p><pre>" + _e(mask("\n".join(f.get("details") or []))) + "</pre>")
        if f.get("kind") == "refused_by_device":
            rows.append(("Attention", "<span class='bad'>needs a person</span>"))
            inv = (f"<p><b>Investigation:</b> {_e(mask(f.get('finding', '')))}</p>"
                   + (f"<p><b>Lines that work instead:</b></p><pre>{_e(mask(chr(10).join(f['alternative'])))}</pre>"
                      if f.get("alternative") else "")
                   + (f"<p><b>New change:</b> {_e(f['new_ticket'])}</p>" if f.get("new_ticket") else "")
                   if f.get("investigation") == "done" else "<p>Investigation: in progress (the agent is looking "
                   "for lines the device accepts).</p>")
            nxt = (f"<p class='bad'><b>FAILED: needs human attention.</b> {_e(f.get('device'))} refused "
                   f"<code>{_e(mask(f.get('line', '')))}</code> in the dry run. Nothing was changed on any device and "
                   f"no approval was opened.</p><p class='mut'>{_e(mask(f.get('error', '')))}</p>{inv}")
        table ="".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows)
        devs = "".join(f"<h2>{_e(d['device'])} <span class='mut'>{_e(d.get('state', ''))}</span></h2>"
                       + _diff_html(d.get("diff", ""))
                       + (f"<p class='bad'>{_e(mask(d['error']))}</p>" if d.get("error") else "")
                       for d in t.get("devices", []))
        lines = _e(mask("\n".join(t.get("lines", []))))
        if t.get("left_out"):
            lines += ("</pre><p class='bad'>NOT in this change (the device refused these lines in an earlier "
                      "proposal):</p><pre>" + _e(mask("\n".join(f"{x.get('line')}  ({x.get('why', '')[:160]})"
                                                             for x in t["left_out"]))))
        verify = ""
        if t.get("verify_commands"):
            verify = "<h2>Verify after the change</h2><pre>" + _e("\n".join(t["verify_commands"])) + "</pre>"
        progress = [*(_e(x) for x in t.get("log", [])), *(_e(x) for x in self._audit(t["id"]))]
        refresh = '<meta http-equiv="refresh" content="10">' if st in OPEN else ""
        return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width,initial-scale=1'>{refresh}"
                f"<title>Change {_e(t['id'])}</title><style>{STYLE}</style></head><body><main>"
                f"<h1>Change {_e(t['id'])}</h1><p class='mut'>{_e(t.get('description'))}</p>"
                f"<div class='box'>{nxt}</div><h2>Ticket</h2><table>{table}</table>"
                f"<h2>Configuration lines</h2><pre>{lines}</pre>{devs}{verify}"
                f"<h2>Progress</h2><pre>{chr(10).join(progress) or 'nothing yet'}</pre>"
                f"<p class='mut'>Read-only. Secret-looking values are masked. "
                f"{'This page refreshes every 10 s.' if refresh else ''}</p></main></body></html>")


def _handler(pages: Pages):
    class H(BaseHTTPRequestHandler):
        server_version = "rudder"
        sys_version = ""

        def log_message(self, fmt, *args):            # no tokens in logs
            log.debug("web %s %s", self.client_address[0], self.command)

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            if isinstance(self.connection, ssl.SSLSocket):
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            m = TOKEN_RE.match(self.path.split("?", 1)[0])
            t = pages.find(m.group(1)) if m else None
            if not t:
                return self._send(404, "<!doctype html><title>Not found</title><p>Not found or expired.</p>")
            return self._send(200, pages.render(t))

        def do_HEAD(self):  # noqa: N802
            self._send(405, "")

        do_POST = do_PUT = do_DELETE = do_HEAD

    return H


TLS_MODES = ("auto", "on", "off")


def tls_mode(appr: dict) -> str:
    v = str((appr or {}).get("WEB_TLS") or "auto").strip().lower()
    return v if v in TLS_MODES else "auto"


def tls_files(appr: dict) -> tuple[Path, Path] | None:
    """(certificate, key) when the pages are to be served over HTTPS and the files exist."""
    if tls_mode(appr) == "off":
        return None
    from navigator_gate import tls
    f = tls.files()
    return (f["cert"], f["key"]) if f["cert"].is_file() and f["key"].is_file() else None


def scheme(appr: dict) -> str | None:
    """https / http for the links; None when WEB_TLS is on but there is no certificate (pages off)."""
    if tls_files(appr):
        return "https"
    return None if tls_mode(appr) == "on" else "http"


class _Server(ThreadingHTTPServer):
    """The TLS handshake runs in each request's own thread (with a timeout), so one slow client cannot
    hold up the others."""
    ctx: ssl.SSLContext | None = None

    def finish_request(self, request, client_address):
        if not self.ctx:
            return super().finish_request(request, client_address)
        request.settimeout(15)
        try:
            tls_sock = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError, socket.timeout):
            return                                       # plain HTTP or a bad client: dropped
        try:
            super().finish_request(tls_sock, client_address)
        finally:
            try:
                tls_sock.close()
            except OSError:
                pass


def serve(state_path: Path, appr: dict, how: str, where: str) -> ThreadingHTTPServer | None:
    """Start the page server in a daemon thread; None when APPROVAL.WEB_BIND is not set (or WEB_TLS on
    without a certificate)."""
    bind = str(appr.get("WEB_BIND") or "").strip()
    if not bind:
        return None
    sch = scheme(appr)
    if not sch:
        log.error("change pages off: APPROVAL.WEB_TLS is on but there is no certificate "
                  "(navigator-setup tls init)")
        return None
    host, _, port = bind.rpartition(":")
    from .retention import keep_days
    pages = Pages(state_path, keep_days(appr) * 1440, how, where)     # pages live as long as their tickets
    srv = _Server((host, int(port)), _handler(pages))
    files = tls_files(appr)
    if files:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(files[0]), str(files[1]))
        srv.ctx = ctx
    threading.Thread(target=srv.serve_forever, name="rudder-web", daemon=True).start()
    log.info("change pages on %s://%s (links: %s)", sch, bind, appr.get("WEB_URL") or f"{sch}://{bind}")
    return srv
