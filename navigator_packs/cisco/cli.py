"""Cisco IOS / IOS-XE / NX-OS changes over SSH (netmiko), with a checkpoint for rollback.

These platforms have no configuration sessions, so:

    dry run   computed from the running config: which lines are new (+), which "no" lines remove
              something (-), and "no" lines whose effect cannot be seen in the running config (~).
              Nothing is sent to the device except "show running-config".
    apply     checkpoint first (IOS/IOS-XE: copy running-config <fs>rudder-<ticket>.cfg; NX-OS:
              checkpoint rudder-<ticket>), then the lines in config mode. A line the device refuses
              rolls the device back to the checkpoint at once.
    confirm   remove the checkpoint.
    rollback  IOS/IOS-XE: configure replace <file> force; NX-OS: rollback running-config checkpoint.

There is no rollback timer on these platforms: if RUDDER stops between apply and confirm, the
checkpoint stays on the device and the ticket says how to roll back by hand.

Lines: write them as they appear in "show running-config": child lines indented under their parent
("interface Loopback10" / " description x"). Unindented lines after a mode line (interface, router,
line, vlan, ...) are taken as its children unless they are themselves top-level commands.
"""
from __future__ import annotations

import re

from rudder_devices.adapters import INVALID, DeviceError, NetmikoText, _netmiko_connect
from rudder_devices.config import Settings

VOLATILE = re.compile(r"^(Building configuration|Current configuration\s*:|!\s*Last configuration change|"
                      r"!\s*NVRAM config last updated|!Time:|!Command:|!Running configuration last done|"
                      r"!No configuration change since last restart)", re.I)
MODE = re.compile(r"^(no\s+)?(interface|router|line|vlan|vrf|ip\s+(access-list|prefix-list\s+\S+\s+seq)|"
                  r"ipv6\s+access-list|route-map|class-map|policy-map|key\s+chain|crypto\s+\S+|"
                  r"object-group|control-plane|track|bridge-domain|segment-routing|address-family|"
                  r"aaa\s+group|ip\s+sla\s+\d|event\s+manager\s+applet)\b", re.I)
TOP = re.compile(r"^(no\s+)?(hostname|ip\s+(route|domain|domain-name|name-server|routing|http|ssh|scp|ftp|tftp|"
                 r"multicast-routing|forward-protocol|host|cef|community-list|as-path|vrf)|"
                 r"ipv6\s+(route|unicast-routing|prefix-list)|ntp|snmp-server|banner|access-list|spanning-tree|"
                 r"service|feature|clock|errdisable|license|archive|vtp|username|aaa|boot)\b", re.I)
REFUSED = re.compile(r"^\s*%\s*(Invalid|Incomplete|Ambiguous|Error)|^\s*%\s|^ERROR:|% Invalid|"
                     r"Invalid command|Syntax error", re.I | re.M)
FS = {"cisco_ios": "flash:", "cisco_xe": "bootflash:"}


def _key(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p.lower() for p in path)       # NX-OS writes "interface loopback10"; keywords ignore case


def config_paths(running: str) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Every line of a running config as its path from the top, keyed case-insensitively:
    {("interface lo1", "description x"): ("interface Lo1", "description x")}."""
    paths: dict[tuple[str, ...], tuple[str, ...]] = {}
    stack: list[tuple[int, str]] = []
    for raw in running.splitlines():
        if not raw.strip() or raw.strip().startswith("!") or VOLATILE.match(raw.strip()):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= ind:
            stack.pop()
        line = " ".join(raw.split())
        path = tuple(s for _, s in stack) + (line,)
        paths[_key(path)] = path
        stack.append((ind, line))
    return paths


def line_paths(lines: list[str]) -> list[tuple[tuple[str, ...], str]]:
    """(parent path, line) for each change line; indentation wins, then the mode-line heuristic."""
    out: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[int, str]] = []
    for raw in lines:
        if not raw.strip():
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        line = " ".join(raw.split())
        if ind == 0 and stack and stack[-1][0] == 0 and not TOP.match(line) and not MODE.match(line):
            parent = (stack[0][1],)                         # unindented child of the last mode line
            out.append((parent, line))
            continue
        while stack and stack[-1][0] >= ind:
            stack.pop()
        out.append((tuple(s for _, s in stack), line))
        if MODE.match(line) and not line.lower().startswith("no "):
            stack.append((ind, line))
    return out


def plan_diff(running: str, lines: list[str]) -> str:
    """+ new line, - line removed by a "no" line, ~ "no" line whose effect the running config cannot
    show (for example "no shutdown" on a port that is already up), plain = existing parent."""
    have = config_paths(running)
    shown: set[tuple[str, ...]] = set()
    out: list[str] = []

    def context(parent: tuple[str, ...]):
        for i in range(1, len(parent) + 1):
            k = _key(parent[:i])
            if k not in shown:
                shown.add(k)
                out.append((" " if k in have else "+") + " " * (i - 1) + have.get(k, parent[:i])[-1])

    for parent, line in line_paths(lines):
        pad = " " * len(parent)
        path = _key(parent + (line,))
        m = re.match(r"^no\s+(.*)$", line, re.I)
        if m:
            if path in have:
                continue                                       # "no X" is already in the config
            target = _key(parent + (m.group(1),))
            gone = sorted(k for k in have if k[:len(target)] == target or
                          (len(k) == len(target) and k[:-1] == target[:-1] and k[-1].startswith(target[-1] + " ")))
            context(parent)
            if gone:
                for k in gone:
                    if k not in shown:
                        shown.add(k)
                        out.append("-" + " " * (len(k) - 1) + have[k][-1])
            else:
                out.append("~" + pad + line + "   (cannot tell from the running config)")
            continue
        if path in have or path in shown:
            continue
        context(parent)
        shown.add(path)
        out.append("+" + pad + line)
    return "\n".join(out) + ("\n" if out else "")


class CiscoCliWriter:
    name = "native:netmiko-cli"
    reads, writes = True, True
    timer_rollback = False

    def __init__(self, s: Settings, device_type: str, connect=None):
        self.s, self.device_type = s, device_type
        self._connect = connect or _netmiko_connect

    # reads (verify commands)
    def run(self, dev, cred, commands):
        return NetmikoText(self.s, self.device_type, connect=self._connect).run(dev, cred, commands)

    def _cp(self, session: str) -> str:
        return f"{FS.get(self.device_type, 'flash:')}{session}.cfg"

    def checkpoint_name(self, session: str) -> str:
        return session if self.device_type == "cisco_nxos" else self._cp(session)

    def manual_rollback(self, session: str) -> str:
        if self.device_type == "cisco_nxos":
            return f"rollback running-config checkpoint {session}"
        return f"configure replace {self._cp(session)} force"

    def running_config(self, dev, cred) -> str:
        conn = self._connect(dev, cred, self.device_type, self.s)
        try:
            text = conn.send_command("show running-config", read_timeout=max(60, self.s.timeout))
        finally:
            conn.disconnect()
        if INVALID.search(text or "") and "hostname" not in (text or ""):
            raise DeviceError("rejected", "show running-config was refused (privilege level? ENABLE_PASSWORD)")
        return "\n".join(ln for ln in text.splitlines() if not VOLATILE.match(ln.strip())) + "\n"

    def dry_run(self, dev, cred, lines, session) -> str:
        return plan_diff(self.running_config(dev, cred), lines)

    def candidate_config(self, dev, cred, lines, session) -> str:
        """Best effort for the validator: the running config with the lines added at the end (IOS / NX-OS
        merge a repeated stanza); "no" lines are left out (the validator cannot apply them to a text)."""
        running = self.running_config(dev, cred).rstrip("\n")
        running = running[:-3].rstrip("\n") if running.endswith("\nend") else running
        keep = [ln for ln in lines if not ln.strip().lower().startswith("no ")]
        return running + "\n!\n" + "\n".join(keep) + "\n!\nend\n"

    def _send(self, conn, cmd: str) -> str:
        out = conn.send_command_timing(cmd, read_timeout=max(60, self.s.timeout))
        for _ in range(3):                                   # "Destination filename [x]?" / "[confirm]"
            if re.search(r"\[[^\]]*\]\s*\??\s*$|\(y/n\)\??\s*$|\[confirm\]\s*$", out.strip(), re.I):
                out += conn.send_command_timing("\n", read_timeout=max(60, self.s.timeout))
            else:
                break
        return out

    def _checkpoint(self, conn, session: str) -> None:
        if self.device_type == "cisco_nxos":
            out = self._send(conn, f"checkpoint {session}")
            if REFUSED.search(out) or "error" in out.lower():
                raise DeviceError("rejected", f"could not create checkpoint {session}: {out.strip()[:200]}")
        else:
            out = self._send(conn, f"copy running-config {self._cp(session)}")
            if not re.search(r"bytes copied|\[OK\]|copied", out, re.I) or REFUSED.search(out):
                raise DeviceError("rejected", f"could not save a checkpoint to {self._cp(session)}: "
                                              f"{out.strip()[:200]}")

    def _rollback(self, conn, session: str) -> None:
        out = self._send(conn, self.manual_rollback(session))
        ok = (re.search(r"Rollback Done|completed successfully|rollback.*success", out, re.I)
              and not re.search(r"Rollback aborted|failed|%\s*Error", out, re.I))
        if not ok:
            raise DeviceError("error", f"rollback to the checkpoint did not report success: {out.strip()[-200:]}")

    def apply(self, dev, cred, lines, session, timer=None) -> None:
        conn = self._connect(dev, cred, self.device_type, self.s)
        try:
            self._checkpoint(conn, session)
            out = conn.send_config_set(lines, cmd_verify=False, read_timeout=max(60, self.s.timeout))
            bad = REFUSED.search(out or "")
            if bad:
                snippet = (out[max(0, bad.start() - 120):bad.end() + 120]).strip()
                try:
                    self._rollback(conn, session)
                    note = "rolled back to the checkpoint"
                except DeviceError as e:
                    note = f"ROLLBACK FAILED ({e}); roll back by hand: {self.manual_rollback(session)}"
                raise DeviceError("rejected", f"the device refused a line: {snippet[:300]}; {note}")
        finally:
            conn.disconnect()

    def confirm(self, dev, cred, session) -> None:
        """Remove the checkpoint. Best effort: a leftover checkpoint file does no harm."""
        try:
            conn = self._connect(dev, cred, self.device_type, self.s)
        except DeviceError:
            return
        try:
            if self.device_type == "cisco_nxos":
                self._send(conn, f"no checkpoint {session}")
            else:
                self._send(conn, f"delete /force {self._cp(session)}")
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.disconnect()

    def abort(self, dev, cred, session) -> None:
        conn = self._connect(dev, cred, self.device_type, self.s)
        try:
            self._rollback(conn, session)
        finally:
            conn.disconnect()
        self.confirm(dev, cred, session)

    def save(self, dev, cred) -> None:
        conn = self._connect(dev, cred, self.device_type, self.s)
        try:
            out = conn.save_config()
        finally:
            conn.disconnect()
        if REFUSED.search(out or ""):
            raise DeviceError("error", f"save failed: {out.strip()[:200]}")
