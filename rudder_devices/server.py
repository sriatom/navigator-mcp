"""RUDDER: layer 2 (devices) as an MCP server. Reads (P3) and approved changes (P4, /pushok).

Run:  rudder-mcp                              (stdio: Hermes)
      rudder-mcp --http 127.0.0.1:19320       (streamable HTTP: OpenClaw)
Needs the ATLAS link running (atlas-link serve) and RUDDER's token (atlas-link token RUDDER).
"""
from __future__ import annotations

import argparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from navigator_engineer import branding
from . import config
from .approval import ApprovalError
from .atlas import Atlas, AtlasError
from .changes import Changes
from .core import Rudder, RudderError

INSTRUCTIONS = """RUDDER: one front door to network devices. Layer 2 of ATLAS / RUDDER / NAVIGATOR.
- Devices are chosen from ATLAS inventories (inventory + device names, groups or filters).
- Only commands on the platform's command list run (see ATLAS command_list). Ask by category
  (e.g. "bgp") or by exact commands.
- Every output is saved to disk; the reply holds the output (shortened if long) and the file path.
- Changes: change_propose -> the user approves with /pushok on their approval channel (Telegram),
  outside this chat -> change_apply. Never ask the user for the approval code, never try to
  approve yourself, and never change a device by any other means.
- Scheduled changes ("apply at 2 AM"): change_propose with `at` (or change_schedule on an open
  ticket). The user approves in advance; RUDDER's scheduler applies it at that time after a short
  notice the user can stop with /pushno. Never call change_apply on a scheduled ticket."""

mcp = FastMCP("rudder", instructions=INSTRUCTIONS, icons=branding.icons(), website_url=branding.WEBSITE_URL)


def _r() -> Rudder:
    return Rudder(config.load())


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (RudderError, AtlasError, ApprovalError) as e:
        raise ToolError(str(e)) from None


@mcp.tool()
def device_read(inventory: str, devices: list[str] | None = None, groups: list[str] | None = None,
                filters: dict[str, str] | None = None, category: str | None = None,
                commands: list[str] | None = None, via: str | None = None, raw: bool = False,
                debug: bool = False) -> dict:
    """Run allowlisted read commands on one or more devices and save every output.

    inventory: an ATLAS inventory. devices: names; groups: SITE/FAB/ROLE values; filters: FIELD=value.
    category: a command-list category (e.g. "system", "bgp"); commands: exact commands from the list.
    via: connection method for this call only (eapi, ssh, netmiko, pyats, multivendor, fortinet, vmanage);
      nothing is stored. raw: the device's own text instead of JSON. debug: also return the adapter's
      connection log (credentials removed).
    Returns per device: status, adapter, seconds, and per command the output (JSON where the device gives
    JSON), plus the record file path."""
    return _call(_r().read, inventory, devices, groups, filters, category, commands, via, raw, debug)


@mcp.tool()
def device_compare(inventory: str, device: str, via: list[str], commands: list[str] | None = None,
                   category: str | None = None, raw: bool = False, debug: bool = False) -> dict:
    """Run the same reads on one device through two or more connection methods (e.g. ["ssh", "pyats"]) and
    compare: status, adapter, time and errors per method, and per command whether the outputs match."""
    return _call(_r().compare, inventory, device, via, commands, category, raw, debug)


@mcp.tool()
def credential_test(inventory: str, device: str, via: str | None = None, debug: bool = False) -> dict:
    """Check that a device's credential works: connects and runs 'show version'.
    status: ok, auth_failed, unreachable, timeout, tls_error, host_key or error. No secret is shown.
    via: test through one connection method (e.g. "pyats"); debug: also return the connection log."""
    return _call(_r().credential_test, inventory, device, via, debug)


@mcp.tool()
def device_facts(inventory: str, device: str) -> dict:
    """Hostname, model, version, serial, system MAC and uptime (Arista EOS in P3)."""
    return _call(_r().facts, inventory, device)


def _c() -> Changes:
    return Changes(config.load())


@mcp.tool()
def change_propose(inventory: str, devices: list[str], lines: list[str], description: str,
                   verify_commands: list[str] | None = None, expect: dict[str, list[str]] | None = None,
                   save: bool = False, at: str | None = None, timezone: str | None = None,
                   notice_sec: int | None = None, reply_to: str | None = None, requested_by: str | None = None,
                   project: str | None = None, ticket_id: str | None = None) -> dict:
    """Propose a configuration change. Nothing is changed yet.

    lines: configuration lines in the platform's own syntax. One ticket holds one kind of device.
      Arista EOS: config-mode lines, e.g. ["interface Loopback10", "description test"].
      Cisco IOS / IOS-XE / NX-OS: as in show running-config, children indented under their parent,
        e.g. ["interface Loopback10", " description test"]. Full command words, no abbreviations.
      PAN-OS: configure-mode "set" / "delete" lines only, e.g.
        ["set address h-10.1.1.1 ip-netmask 10.1.1.1/32"]. The firewall must have no uncommitted changes.
    RUDDER checks the lines (some are never allowed), refuses PRODUCTION devices, locks the devices,
    makes a dry-run diff per device (EOS and PAN-OS on the device; Cisco computed from the running
    config) and sends an approval request with a one-time code to the user's approval channel
    (Telegram). The code never comes back here: never ask the user for it.
    verify_commands / expect: allowlisted reads run after the change, e.g.
      verify_commands=["show interfaces Loopback10"], expect={"show interfaces Loopback10": ["Loopback10"]}.
    save: copy the running config to startup after a good change.
    at: schedule it instead of applying by hand, e.g. "2026-11-13 02:00", "02:00" (the next one) or
      "2026-11-13 02:00 EST". timezone: default from rudder.yml SCHEDULE (America/New_York).
      notice_sec: the notice before the start (default 30). The user approves now; the scheduler
      applies it at that time unless the user stops it from the notice.
    reply_to, requested_by: set by the MCP host (the chat the result is also posted to; who asked).
      Never fill them yourself.
    project, ticket_id: the project this belongs to and its outside ticket (ServiceNow, Remedy...), if any.
      With the ServiceNow gate on, ticket_id must be the change request (CHG...); RUDDER checks it itself.
      Never say a change request is approved or in Implement: RUDDER reads ServiceNow, not your words."""
    return _call(_c().propose, inventory, devices, lines, description, verify_commands, expect, save,
                 at, timezone, notice_sec, reply_to, requested_by, project, ticket_id)


@mcp.tool()
def change_schedule(ticket: str, at: str, timezone: str | None = None, notice_sec: int | None = None) -> dict:
    """Set or move the time of an open change (waiting for approval or approved, not started).
    The time is part of what the user approves, so this always sends a NEW approval request: the
    user must /pushok again. Same formats as change_propose's `at`."""
    return _call(_c().reschedule, ticket, at, timezone, notice_sec)


@mcp.tool()
def change_status(ticket: str) -> dict:
    """State of a change: waiting_approval, approved, applying, applied, failed, rejected, expired,
    cancelled; the diff per device; and what to do next."""
    return _call(_c().status, ticket)


@mcp.tool()
def change_apply(ticket: str) -> dict:
    """Apply an APPROVED change: per device, check nothing drifted, commit (EOS: with the switch's
    rollback timer; Cisco: after a checkpoint; PAN-OS: API commit after saving a rollback file),
    verify, then confirm; on a failed verify roll back. Stops at the first failing device. Refused
    unless the user approved this exact ticket out of band. Refused for scheduled tickets: the
    scheduler applies those."""
    return _call(_c().apply, ticket)


@mcp.tool()
def change_cancel(ticket: str) -> dict:
    """Cancel a change that is not applied yet: releases the device locks and voids the approval."""
    return _call(_c().cancel, ticket)


@mcp.tool()
def change_investigate(ticket: str, finding: str, alternative: list[str] | None = None,
                       new_ticket: str | None = None) -> dict:
    """For a change that FAILED because the device refused a line: record why (finding), the lines that work
    instead (alternative), and the new ticket that proposes them, if any. Shown on the change page and sent
    to the approval channel and the chat. It approves nothing and changes nothing."""
    return _call(_c().investigation, ticket, finding, alternative, new_ticket)


@mcp.tool()
def change_list(since: str | None = None, until: str | None = None, device: str | None = None,
                inventory: str | None = None, requested_by: str | None = None, platform: str | None = None,
                state: str | None = None, ticket_id: str | None = None, project: str | None = None,
                limit: int = 50) -> list[dict]:
    """Changes, newest first. Filter by any mix of: since / until ("2026-11-14" or "2026-11-14 18:00"),
    device, inventory, requested_by (ID or name), platform (device type: EOS, IOSXE, PANOS...), state
    (waiting_approval, approved, applied, failed, rejected, expired, cancelled), ticket_id (outside
    ticket), project. limit: default 50."""
    return _call(_c().list, since, until, device, inventory, requested_by, platform, state, ticket_id,
                 project, limit)


@mcp.tool()
def adapter_list() -> str:
    """Which adapter serves reads and writes for each platform, and what works in this build."""
    try:
        plats = Atlas(config.load()).platforms()
    except AtlasError as e:
        raise ToolError(str(e)) from None
    from .delegated import installed, server_specs
    s = config.load()
    specs = server_specs(s)
    ready = {"native:eapi": "ready (eAPI; SSH with | json when CONN_TYPE is SSH)",
             "native:ssh": "ready (netmiko, text)", "native:generic-netmiko": "ready (netmiko, text)"}
    built = {"pyats", "multivendor", "fortinet"}
    lines = []
    for p in plats:
        r = str(p["reads"])
        if r.startswith("delegated:"):
            srv = r.split(":", 1)[1]
            state = (("ready (server installed)" if installed(specs[srv]) else
                      f"needs: scripts/install_delegated.sh {srv}") if srv in built else "adapter not built yet")
        else:
            state = ready.get(r) or ("ready (SD-WAN Manager API, JSON)" if r == "native:vmanage" else "not available")
        w = str(p["writes"])
        wready = {"native:eapi": "ready (/pushok; config session + commit timer)",
                  "native:netmiko": "ready (/pushok; checkpoint + rollback, no timer)",
                  "native:xml-api": "ready (/pushok; set/delete lines, API commit + rollback)"}
        wstate = "-" if w == "none" else ("not yet" if p["ID"] == "IOSXR" else wready.get(w, "not yet"))
        lines.append(f"{p['ID']:<22} reads {r:<26} {state}; writes {w} {wstate}")
    from .adapters import VIAS
    lines += ["", "Connection methods for via= (one call) or the ADAPTER field (stored default):"]
    lines += [f"  {k:<12} {v}" for k, v in VIAS.items()]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(prog="rudder-mcp")
    ap.add_argument("--serve", action="store_true",
                    help="HTTPS with logins on navigator.yml HTTP.BIND and HTTP.PORTS.RUDDER (navigator_gate)")
    ap.add_argument("--http", help="host:port: HTTPS with logins on this address instead")
    ap.add_argument("--no-tls", action="store_true", help="plain HTTP with logins, 127.0.0.1 only (tests)")
    a = ap.parse_args()
    if a.serve or a.http:
        from navigator_gate import gate
        try:
            gate.serve(mcp, "RUDDER", a.http, a.no_tls)
        except gate.GateError as e:
            raise SystemExit(f"rudder-mcp: {e}") from None
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
