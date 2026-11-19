"""navigator-setup connect: hook an agent host up to NAVIGATOR in one step.

    navigator-setup connect hermes [--profile navigator] [--approvals approvals] [--no-restart]
    navigator-setup connect cursor [--ssh-host user@host] [--write]
    navigator-setup connect claude [--ssh-host user@host]

hermes   on this host: makes the chat profile if Hermes has none by that name, copies the navigator-reply and
         navigator-qc plugins and the chat SOUL, adds `navigator` to mcp_servers and the plugins to
         plugins.enabled (config.yaml is backed up first), lets the profile post results back
         (APPROVAL.REPLY_PROFILES), and restarts the gateway if it runs. --approvals NAME also makes the
         approval bot's profile (push-approve plugin and its SOUL). Slack / Telegram tokens stay a manual step.
cursor   prints an "Add to Cursor" link and the mcp.json entry for a PC that reaches this host over SSH;
         --write puts the entry in ~/.cursor/mcp.json here (Cursor on this host).
claude   prints the `claude mcp add` command.
Every step is skipped when it is already done, so running it again is safe.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

SERVER = "navigator"
CHAT_PLUGINS = ("navigator-reply", "navigator-qc")
CHAT_SOUL = "chatbot-ChatBot-SOUL.md"            # its public name; the private repo calls it chatbot-ChatBot-SOUL.md
APPROVAL_SOUL = "navi-approval-bot-SOUL.md"


class ConnectError(ValueError):
    pass


def _bin(name: str) -> Path:
    return Path(sys.executable).parent / name


def bootstrap() -> Path:
    return Path(os.environ.get("NAVIGATOR_BOOTSTRAP") or "~/.config/navigator/navigator.yml").expanduser()


def integrations() -> Path:
    """integrations/hermes: from $NAVIGATOR_HOME, the folder that holds this venv, or a source checkout."""
    cands = []
    if os.environ.get("NAVIGATOR_HOME"):
        cands.append(Path(os.environ["NAVIGATOR_HOME"]).expanduser())
    exe = Path(sys.executable)
    if len(exe.parents) > 2:
        cands.append(exe.parents[2])                  # ~/navigator/.venv/bin/python -> ~/navigator
    cands.append(Path(__file__).resolve().parent.parent)
    for c in cands:
        if (c / "integrations" / "hermes").is_dir():
            return c / "integrations" / "hermes"
    raise ConnectError("integrations/hermes not found: get it with the install step (the release archive) or set "
                       "NAVIGATOR_HOME to the folder that holds it")


def _soul(src_dir: Path, public: str) -> Path:
    for name in (public, public.replace("ChatBot", "ChatBot")):
        if (src_dir / "soul" / name).is_file():
            return src_dir / "soul" / name
    raise ConnectError(f"soul/{public} not found in {src_dir}")


# ------------------------------------------------------------------ config.yaml edits (comments kept)
def _block_end(lines: list[str], i: int) -> int:
    """Index after the indented block that belongs to the top-level key on line i."""
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or lines[j].startswith((" ", "\t", "#"))):
        j += 1
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1
    return j


def _top(lines: list[str], key: str) -> int | None:
    for i, ln in enumerate(lines):
        if re.match(rf"^{key}:\s*(\{{\}}|\[\]|null|~)?\s*(#.*)?$", ln):
            lines[i] = f"{key}:"
            return i
    return None


def add_server(text: str, command: str, env: dict) -> tuple[str, bool]:
    data = yaml.safe_load(text) or {}
    if SERVER in (data.get("mcp_servers") or {}):
        return text, False
    block = [f"  {SERVER}:", f"    command: {command}", "    env:"]
    block += [f"      {k}: {json.dumps(str(v))}" for k, v in env.items()]
    block += ["    connect_timeout: 60", "    enabled: true"]
    lines = text.splitlines()
    i = _top(lines, "mcp_servers")
    if i is None:
        lines += ["", "mcp_servers:"] + block
    else:
        end = _block_end(lines, i)
        lines[end:end] = block
    return "\n".join(lines) + "\n", True


def enable_plugins(text: str, names) -> tuple[str, list[str]]:
    data = yaml.safe_load(text) or {}
    have = ((data.get("plugins") or {}).get("enabled")) or []
    todo = [n for n in names if n not in have]
    if not todo:
        return text, []
    lines = text.splitlines()
    items = [f"    - {n}" for n in todo]
    i = _top(lines, "plugins")
    if i is None:
        lines += ["", "plugins:", "  enabled:"] + items
    else:
        end = _block_end(lines, i)
        j = next((k for k in range(i + 1, end) if re.match(r"^  enabled:", lines[k])), None)
        if j is None:
            lines[end:end] = ["  enabled:"] + items
        else:
            if re.match(r"^  enabled:\s*\[\s*\]\s*$", lines[j]):
                lines[j] = "  enabled:"
            elif re.match(r"^  enabled:\s*\[", lines[j]):
                raise ConnectError("plugins.enabled is an inline list in config.yaml: add "
                                   f"{', '.join(todo)} to it by hand")
            k = j + 1
            while k < end and re.match(r"^\s{4}- ", lines[k]):
                k += 1
            lines[k:k] = items
    return "\n".join(lines) + "\n", todo


def _save_config(path: Path, new: str, check) -> str:
    old = path.read_text() if path.exists() else ""
    if new == old:
        return "config.yaml: already set"
    data = yaml.safe_load(new) or {}
    check(data)                                        # never write a file that does not say what we meant
    if path.exists():
        bak = path.with_name(f"config.yaml.bak-{time.strftime('%Y%m%d%H%M%S')}-navigator-connect")
        shutil.copy2(path, bak)
    path.write_text(new)
    return f"config.yaml updated{' (backup ' + bak.name + ')' if old else ''}"


# ------------------------------------------------------------------ hermes
def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def _hermes_bin() -> str | None:
    return shutil.which("hermes") or next((str(p) for p in (Path("~/.local/bin/hermes").expanduser(),)
                                           if p.is_file()), None)


def _profile(name: str, description: str, out: list[str]) -> tuple[Path, bool]:
    pdir = _hermes_home() / "profiles" / name
    if pdir.is_dir():
        return pdir, False
    hb = _hermes_bin()
    if not hb:
        raise ConnectError(f"Hermes is not installed (no `hermes` command) and there is no profile {name}. "
                           "Install Hermes first: https://hermes-agent.nousresearch.com/")
    r = subprocess.run([hb, "profile", "create", name, "--description", description], capture_output=True, text=True)
    if r.returncode or not pdir.is_dir():
        raise ConnectError(f"hermes profile create {name} failed: {(r.stderr or r.stdout).strip()[:300]}")
    out.append(f"profile {name}: created")
    return pdir, True


def _copy_plugin(src: Path, pdir: Path, name: str, out: list[str]) -> None:
    dst = pdir / "plugins" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    same = dst.is_dir() and all((dst / f.name).is_file() and (dst / f.name).read_bytes() == f.read_bytes()
                                for f in (src / name).iterdir() if f.is_file())
    if same:
        return
    shutil.copytree(src / name, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    out.append(f"plugin {name}: installed")


def _place_soul(src: Path, pdir: Path, created: bool, out: list[str]) -> None:
    dst = pdir / "SOUL.md"
    if dst.is_file() and dst.read_text() == src.read_text():
        return
    if dst.is_file() and dst.read_text().strip() and not created:
        alt = pdir / "SOUL.navigator.md"
        shutil.copy2(src, alt)
        out.append(f"SOUL.md kept (the profile has its own): NAVIGATOR's rules are in {alt.name}; "
                   "merge its sections into SOUL.md")
        return
    shutil.copy2(src, dst)
    out.append("SOUL.md: NAVIGATOR's chat rules")


def _restart(out: list[str], no_restart: bool) -> None:
    if not shutil.which("systemctl"):
        out.append("gateway: restart Hermes to load the changes (no systemctl on this host)")
        return
    r = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway"], capture_output=True, text=True)
    if r.stdout.strip() != "active":
        out.append("gateway: not running. Start it: hermes gateway install; systemctl --user restart hermes-gateway")
    elif no_restart:
        out.append("gateway: restart it to load the changes: systemctl --user restart hermes-gateway")
    else:
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"], check=False)
        out.append("gateway: restarted")


def hermes(profile: str = "navigator", approvals: str | None = None, no_restart: bool = False,
           reply_profiles=None) -> list[str]:
    src = integrations()
    out: list[str] = []
    pdir, created = _profile(profile, "Network chat bot: NAVIGATOR front door", out)
    for p in CHAT_PLUGINS:
        _copy_plugin(src, pdir, p, out)
    _place_soul(_soul(src, CHAT_SOUL), pdir, created, out)
    cfg = pdir / "config.yaml"
    text = cfg.read_text() if cfg.exists() else ""
    cmd = str(_bin("navigator-mcp"))
    text, added = add_server(text, cmd, {"NAVIGATOR_BOOTSTRAP": bootstrap(), "NAVIGATOR_TRUST_HOST_MODEL": "1"})
    text, enabled = enable_plugins(text, CHAT_PLUGINS)

    def check(d):
        if (d.get("mcp_servers") or {}).get(SERVER) is None or \
                not set(CHAT_PLUGINS) <= set((d.get("plugins") or {}).get("enabled") or []):
            raise ConnectError("config.yaml edit did not come out right; nothing was written")
    out.append(_save_config(cfg, text, check))
    have = (yaml.safe_load(text) or {}).get("mcp_servers", {}).get(SERVER, {})
    if not added and have.get("command") != cmd:
        out.append(f"mcp_servers.{SERVER} was already there with command {have.get('command')}; left as it is")
    others = [k for k in ((yaml.safe_load(text) or {}).get("mcp_servers") or {}) if k != SERVER]
    if others:
        out.append(f"note: the profile has other MCP servers too ({', '.join(others)}). Give it no other device "
                   "servers for the devices NAVIGATOR manages")
    (reply_profiles or _reply_profiles)(profile, out)
    if approvals:
        adir, acreated = _profile(approvals, "Approval bot: /pushok only, no agent tools", out)
        _copy_plugin(src, adir, "push-approve", out)
        _place_soul(_soul(src, APPROVAL_SOUL), adir, acreated, out)
        acfg = adir / "config.yaml"
        atext, _ = enable_plugins(acfg.read_text() if acfg.exists() else "", ["push-approve"])

        def acheck(d):
            if "push-approve" not in ((d.get("plugins") or {}).get("enabled") or []):
                raise ConnectError("approval profile config.yaml edit did not come out right; nothing was written")
        out.append(f"{approvals}: " + _save_config(acfg, atext, acheck))
    _restart(out, no_restart)
    out += ["", "Next, by hand (tokens never go through a script or a chat):",
            f"  hermes -p {profile} model                       pick the chat bot's model",
            f"  ~/.hermes/profiles/{profile}/.env                 its Slack or Telegram tokens",
            "  the approval bot: its own Slack app or Telegram bot, then navigator-setup set APPROVAL.BOT "
            f"{approvals or '<profile>'} APPROVAL.METHOD slack|telegram ...",
            "  steps 3, 4 and 6 of 'Hermes, step by step' on the Install page say how."]
    return out


def _reply_profiles(profile: str, out: list[str]) -> None:
    from . import setup
    try:
        msgs = setup.edit_list("APPROVAL.REPLY_PROFILES", profile, True)
        out += [m for m in msgs if m]
    except Exception:  # noqa: BLE001   no approval settings yet: say what to run later
        out.append(f"results back in the chat thread: after navigator-setup, run "
                   f"navigator-setup add APPROVAL.REPLY_PROFILES {profile}")


# ------------------------------------------------------------------ cursor / claude
def _ssh_target(ssh_host: str | None) -> str:
    if ssh_host:
        return ssh_host
    try:
        host = socket.getfqdn() or socket.gethostname()
    except OSError:
        host = "navhost"
    return f"{getpass.getuser()}@{host}"


def _remote_cmd() -> str:
    exe = str(_bin("navigator-mcp")).replace(str(Path.home()), "$HOME", 1)
    boot = str(bootstrap()).replace(str(Path.home()), "$HOME", 1)
    return f"NAVIGATOR_BOOTSTRAP={boot} exec {exe} 2>/dev/null"


def cursor_entry(ssh_host: str | None, local: bool) -> dict:
    if local:
        return {"command": str(_bin("navigator-mcp")), "env": {"NAVIGATOR_BOOTSTRAP": str(bootstrap())}}
    return {"command": "ssh", "args": ["-T", "-o", "BatchMode=yes", _ssh_target(ssh_host), _remote_cmd()]}


def cursor_link(entry: dict) -> str:
    cfg = base64.b64encode(json.dumps(entry, separators=(",", ":")).encode()).decode()
    return f"cursor://anysphere.cursor-deeplink/mcp/install?name={SERVER}&config={cfg}"


def cursor(ssh_host: str | None = None, write: bool = False) -> list[str]:
    entry = cursor_entry(ssh_host, write)
    out = []
    if write:
        p = Path("~/.cursor/mcp.json").expanduser()
        data = json.loads(p.read_text()) if p.is_file() and p.read_text().strip() else {}
        servers = data.setdefault("mcpServers", {})
        if servers.get(SERVER) == entry:
            out.append(f"{p}: already set")
        else:
            if p.is_file():
                shutil.copy2(p, p.with_name(f"mcp.json.bak-{time.strftime('%Y%m%d%H%M%S')}"))
            servers[SERVER] = entry
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2) + "\n")
            out.append(f"{p}: navigator added. In Cursor: Settings -> MCP shows navigator with 16 tools")
        return out
    t = _ssh_target(ssh_host)
    out += [f"Cursor on your PC, reaching this host as `ssh {t}` (a key, no password prompt).",
            "One click: open this link on the PC (or paste it into the browser there):", "",
            cursor_link(entry), "",
            "Or put this in ~/.cursor/mcp.json on the PC:", "",
            json.dumps({"mcpServers": {SERVER: entry}}, indent=2), "",
            "Another SSH name for this host: navigator-setup connect cursor --ssh-host <user@host>"]
    return out


def claude(ssh_host: str | None = None) -> list[str]:
    here = (f"claude mcp add {SERVER} --scope user -e NAVIGATOR_BOOTSTRAP={bootstrap()} -- "
            f"{_bin('navigator-mcp')}")
    remote = (f"claude mcp add {SERVER} --scope user -- ssh -T -o BatchMode=yes {_ssh_target(ssh_host)} "
              f"'{_remote_cmd()}'")
    return ["Claude Code on this host:", f"  {here}", "Claude Code on your PC (over SSH):", f"  {remote}"]


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="navigator-setup connect", description=__doc__.split("\n\n")[0])
    ap.add_argument("target", choices=("hermes", "cursor", "claude"))
    ap.add_argument("--profile", default="navigator", help="hermes: the chat bot's profile (default navigator)")
    ap.add_argument("--approvals", help="hermes: also make this approval bot profile (e.g. approvals)")
    ap.add_argument("--no-restart", action="store_true", help="hermes: do not restart the gateway")
    ap.add_argument("--ssh-host", help="cursor / claude: how the PC reaches this host (user@host)")
    ap.add_argument("--write", action="store_true", help="cursor: write ~/.cursor/mcp.json on this host")
    a = ap.parse_args(argv)
    try:
        if a.target == "hermes":
            out = hermes(a.profile, a.approvals, a.no_restart)
        elif a.target == "cursor":
            out = cursor(a.ssh_host, a.write)
        else:
            out = claude(a.ssh_host)
    except (ConnectError, OSError, yaml.YAMLError) as e:
        print(f"navigator-setup connect: {e}", file=sys.stderr)
        return 2
    print("\n".join(out))
    return 0
