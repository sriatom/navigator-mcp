"""Out-of-band approval for changes: a human sends /pushok CODE outside the chat.

There is no approve tool anywhere. The code goes only to the approver's channel; RUDDER keeps
only two hashes of it (pending, decision), so nobody who can read RUDDER's state learns the code.

Channel "hermes" (navhost today) reuses NAVI's existing mechanism unchanged:
  - the request is sent with `hermes -p <profile> send -t telegram -f -`;
  - the push-approve Hermes plugin (gateway code, authorized users only) turns
    `/pushok CODE` / `/pushno CODE` into a decision file in ~/.hermes/push-approvals/,
    named sha256("decision:CODE"); it only accepts codes with a pending file sha256("pending:CODE").
Same hash rule as a skills folder/NAVI/lib/push_approval.py, so one plugin serves nv-push and RUDDER.

One approval method at a time (APPROVAL section):
  METHOD     telegram (default) | slack.  api is reserved (an approval system's API), not built.
  BOT        the Hermes profile of the approval bot (old key: PROFILES {CONSUMER: profile}). Never the
             chat bot: the chat bot must not be able to approve its own requests.
  CHANNEL    where requests go: the Slack channel ID (required for slack) or a Telegram chat ID
             (empty = the bot's home channel). The old value "hermes" meant the transport and is ignored.
  APPROVERS  "*" (default: anyone in that channel; the organization curates who is in it) or user IDs.
  SELF_APPROVE  true (default) | false: may the person who asked for the change approve it.
The pending file carries these rules as JSON; the push-approve plugin enforces them (platform, channel,
approver, requester) and writes who decided on the decision's second line. On Slack the user types
/pushok CODE (slash commands registered in the approval bot's Slack app) or @BOT !pushok CODE (Hermes maps
a known !command to /command; APPROVAL.MENTION, default @<BOT>). The message shows each as copyable code.

Results can also go back to the chat a change came from (reply_to on the ticket, filled by the
MCP host, never by the model): "<profile>/<platform>:<chat>[:<thread>]", sent with
`hermes -p <profile> send -t <platform>:<chat>[:<thread>]`. Only profiles listed in
APPROVAL.REPLY_PROFILES may be used; with none listed, nothing is sent. A reply carries the result
only: approving still happens on the approval channel alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # no 0/O/1/I/L
CODE_LEN = 6
REPLY_RE = re.compile(r"^([a-z0-9_-]{1,32})/((?:slack|telegram|discord):-?[A-Za-z0-9_.@]{1,64}(?::[0-9.]{1,32})?)$")


class ApprovalError(Exception):
    pass


def code_hash(kind: str, code: str) -> str:
    """Must match push_approval.code_hash and the push-approve plugin."""
    return hashlib.sha256(f"{kind}:{code.strip().upper()}".encode()).hexdigest()


class FileDecisions:
    """The decision store shared with the push-approve plugin."""

    def __init__(self, directory: Path):
        self.dir = directory

    def open(self, label: str, rules: dict | None = None) -> tuple[str, dict]:
        """New code -> (code, {pending, decision}). The caller sends the code and forgets it.
        With rules, the pending file is JSON the push-approve plugin enforces (method, channel,
        approvers, requester, self_approve); without, it is the plain label (nv-push, unchanged)."""
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        hashes = {"pending": code_hash("pending", code), "decision": code_hash("decision", code)}
        fd = os.open(self.dir / hashes["pending"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            body = json.dumps({"label": label, **rules}) if rules else label
            os.write(fd, (body + "\n").encode())
        finally:
            os.close(fd)
        return code, hashes

    def _decision(self, hashes: dict) -> list[str]:
        d = self.dir / hashes["decision"] if hashes.get("decision") else None
        return d.read_text().split("\n") if d and d.is_file() else []

    def state(self, hashes: dict) -> str:
        """approved | rejected | waiting | gone (no pending and no decision: expired or cleaned)."""
        if not hashes.get("decision"):
            return "gone"
        lines = self._decision(hashes)
        if lines:
            v = lines[0].strip()
            return v if v in ("approved", "rejected") else "rejected"
        return "waiting" if (self.dir / hashes["pending"]).is_file() else "gone"

    def who(self, hashes: dict) -> str:
        """Who decided, as the plugin wrote it on the decision's second line ("" if unknown)."""
        lines = self._decision(hashes)
        return lines[1].strip()[:120] if len(lines) > 1 else ""

    def close(self, hashes: dict) -> None:
        for k in ("pending", "decision"):
            if hashes.get(k):
                (self.dir / hashes[k]).unlink(missing_ok=True)


# The platform the approval request goes to and the /pushok must come from. Telegram and Slack are
# handled by the push-approve Hermes plugin; any platform the host can send to works with a host's own
# /pushok handler that calls rudder-approve (rudder_devices/approve_cli.py).
METHODS = ("telegram", "slack", "discord", "teams", "mattermost", "matrix", "whatsapp", "signal")
CHANNEL_REQUIRED = ("slack", "teams", "mattermost", "matrix")      # no "home channel": name the channel
CHAT_ID_RE = re.compile(r"^-?[A-Za-z0-9_.@]{1,64}$")   # no ':' (the send target is platform:chat[:thread])


def approvers(cfg: dict) -> list[str] | str:
    """APPROVAL.APPROVERS: "*" (anyone in the approval channel; the organization curates it) or a list of
    user IDs. Default "*"."""
    v = cfg.get("APPROVERS", "*")
    if v in (None, "", "*", ["*"]):
        return "*"
    items = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
    return "*" if not items or "*" in items else items


class HermesChannel:
    name = "hermes"

    def __init__(self, cfg: dict, consumer: str, runner=subprocess.run):
        self.hermes = os.path.expanduser(str(cfg.get("HERMES_BIN", "~/.local/bin/hermes")))
        profiles = {str(k).upper(): v for k, v in (cfg.get("PROFILES") or {}).items()}
        self.profile = str(cfg.get("BOT") or profiles.get(consumer.upper(), consumer.lower()))
        self.method = str(cfg.get("METHOD") or "telegram").lower()
        if self.method not in METHODS:
            raise ApprovalError(f"APPROVAL.METHOD must be one of {', '.join(METHODS)} (api is not built yet)")
        chan = str(cfg.get("CHANNEL") or "").strip()
        self.chat = "" if chan.lower() == "hermes" else chan       # CHANNEL: hermes was the old transport key
        if self.chat and not CHAT_ID_RE.match(self.chat):
            raise ApprovalError("APPROVAL.CHANNEL must be a channel or chat ID")
        if self.method in CHANNEL_REQUIRED and not self.chat:
            raise ApprovalError(f"APPROVAL.METHOD {self.method} needs APPROVAL.CHANNEL (the approval channel's ID)")
        self.approvers = approvers(cfg)
        self.self_approve = str(cfg.get("SELF_APPROVE", True)).strip().lower() not in ("0", "false", "no", "off")
        self.decisions = FileDecisions(Path(os.path.expanduser(str(cfg.get("APPROVAL_DIR",
                                                                            "~/.hermes/push-approvals")))))
        self.reply_profiles = {str(p).lower() for p in (cfg.get("REPLY_PROFILES") or [])}
        self.mention = str(cfg.get("MENTION") or f"@{self.profile.upper()}")[:40]
        self.run = runner

    @property
    def target(self) -> str:
        return f"{self.method}:{self.chat}" if self.chat else self.method

    @property
    def where(self) -> str:
        if self.method == "slack":
            return f"the approval channel (Slack {self.chat}, bot {self.profile})"
        return f"{self.profile} Telegram"

    @property
    def prefix(self) -> str:
        """The command prefix shown first. On Slack both work: /pushok (a slash command registered in the
        approval bot's Slack app) and @BOT !pushok (Hermes maps a known !command to /command)."""
        return "/"

    def how(self, verb: str = "pushok") -> str:
        return f"!{verb} <code>" if self.method == "slack" else f"/{verb} <code>"

    def _text(self, msg: str) -> str:
        """Telegram: unchanged (/pushok CODE is tappable there). Slack: !pushok CODE as its own copyable code
        span (Hermes maps a known !command to /command; the approval channel is set to need no @mention,
        SLACK_FREE_RESPONSE_CHANNELS on the approval bot). /pushok works too once registered as a slash
        command in the bot's Slack app."""
        if self.method != "slack":
            return msg
        return re.sub(r"(?<![\w!`])/(push(?:ok|no)) ([A-Za-z0-9]{4,12})\b", r"`!\1 \2`", msg)

    def _send(self, text: str, what: str) -> None:
        try:
            sent = self.run([self.hermes, "-p", self.profile, "send", "-t", self.target, "-f", "-"],
                            input=self._text(text), text=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise ApprovalError(f"could not send the {what} ({type(e).__name__})") from None
        if sent.returncode != 0:
            raise ApprovalError(f"could not send the {what} to {self.where} (hermes exit {sent.returncode})")

    def reply_ok(self, target: str | None) -> bool:
        """A reply target this channel may send to (format and allowed profile)."""
        m = REPLY_RE.match(str(target or ""))
        return bool(m) and m.group(1) in self.reply_profiles

    def reply(self, target: str, text: str) -> None:
        """The result, posted in the chat the change came from. Never carries a code."""
        m = REPLY_RE.match(str(target or ""))
        if not m or m.group(1) not in self.reply_profiles:
            raise ApprovalError("reply target not allowed")
        try:
            sent = self.run([self.hermes, "-p", m.group(1), "send", "-t", m.group(2), "-f", "-"],
                            input=text, text=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise ApprovalError(f"could not send the reply ({type(e).__name__})") from None
        if sent.returncode != 0:
            raise ApprovalError(f"could not send the reply (hermes exit {sent.returncode})")

    def rules(self, requester: str | None = None) -> dict:
        """What the push-approve plugin must check before it records a decision for this code."""
        return {"method": self.method, "channel": self.chat, "approvers": self.approvers,
                "requester": str(requester or ""), "self_approve": self.self_approve}

    def request(self, label: str, message_for: callable, requester: str | None = None) -> dict:
        """Open a code, send the message (built with the code), return the hashes only."""
        code, hashes = self.decisions.open(label, self.rules(requester))
        msg = message_for(code)
        try:
            self._send(msg, "approval request")
        except ApprovalError:
            self.decisions.close(hashes)
            raise
        finally:
            del code, msg
        return {**hashes, "channel": self.name, "method": self.method, "where": self.where}

    def notify(self, text: str) -> None:
        """A plain message to the approval channel (no code): results, notices, bypass reports."""
        self._send(text, "message")

    def state(self, hashes: dict) -> str:
        return self.decisions.state(hashes)

    def who(self, hashes: dict) -> str:
        return self.decisions.who(hashes)

    def close(self, hashes: dict) -> None:
        self.decisions.close(hashes or {})


def transport(cfg: dict) -> str:
    """TRANSPORT (default hermes). The old key CHANNEL: hermes meant the same thing."""
    t = cfg.get("TRANSPORT")
    if t:
        return str(t).lower()
    return "hermes"


def channel(cfg: dict | None, consumer: str, runner=subprocess.run):
    cfg = cfg or {}
    kind = transport(cfg)
    if kind == "hermes":
        return HermesChannel(cfg, consumer, runner)
    raise ApprovalError(f"approval transport {kind!r} is not built yet (hermes only)")
