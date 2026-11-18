# Approvals, change pages and host settings

How a configuration change gets approved, where the approval happens, how to set it up and change it later,
and which ports it needs. The same guide as one web page: approvals.html.

**The rules in one breath:** reads (`show` commands) never need a ticket or an approval. Every configuration
change is a RUDDER ticket (`C<date>-<hex>`) that a person approves **outside the chat**, on the approval
channel, with a one-time code. The chat bot can ask for a change but can never approve one: approvals go
through a **separate app** (two Slack apps, or one Slack app and one Telegram bot; section 0).

## 0. You need two bots: the chat app and a separate approval app

Approvals **never** come through the chat bot's app. The bot you talk to (here **ChatBot**) can only *ask*
for a change; a **separate** bot with its **own** app (here **ApprovalBot**) posts the approval requests and is the
only one that accepts `/pushok`. So a host needs one of:

| Chat bot (asks for changes) | Approval bot (approves them) |
|---|---|
| Slack app #1 (ChatBot) | **Slack app #2** (ApprovalBot), in a private approval channel |
| Slack app (ChatBot) | **a Telegram bot** (ApprovalBot) |

- Two separate apps: their own tokens, their own Hermes profiles (`APPROVAL.BOT` names the approval one), and
  the approval app alone in the approval channel. `navigator-setup` refuses an approval bot that is also a
  chat bot (`REPLY_PROFILES`), and the chat bot's profile does not load the push-approve plugin.
- Why: if the same app both asked and approved, a model that controls the chat bot could approve its own
  change. With two apps the approval always needs a human in a different place.
- **One Slack app per running bot.** Never connect two programs with the same app token: Slack splits the
  events between them, so messages go missing and the other program may act on them.

## 1. The flow

```
 you (chat) ──"add Loopback97 on QALEAF1A"──▶ chat bot ──change propose──▶ NAVIGATOR ─▶ RUDDER
                                                                                    │ ticket + dry-run diff
                                                                                    │ device locked
                                                                                    ▼
 approval channel (Slack #approvals or Telegram) ◀── 🔐 request: diff, /pushok CODE, 🔗 page link
        │
        │ approver: !pushok CODE
        ▼
 push-approve (approval bot) checks method, channel, approver, requester ──▶ decision recorded
        │
        ▼
 rudder-scheduler: applies within seconds (commit with rollback timer → verify → confirm, or roll back)
        │
        ├──▶ ✅ / ❌ result in the approval channel
        ├──▶ ✅ / ❌ result in the chat thread the change came from
        └──▶ change page (read-only) shows the full diff and the progress
```

1. You ask the chat bot for a change. It calls `change propose`. The MCP host adds who asked (`requested_by`)
   and where to answer (`reply_to`); the model cannot set or fake either.
2. RUDDER checks the lines (some are never allowed), refuses PRODUCTION devices, locks the devices, makes a
   dry-run diff on each device and, with change validation on, checks the change with the validator (a fail
   stops here: no approval is opened). It binds devices + lines + diffs + the validation result into one hash.
3. The approval bot posts the request with a one-time code. RUDDER keeps only hashes of the code.
4. An approver answers with the code. The push-approve plugin records the decision only if the rules hold
   (section 3). A refused answer leaves the code open and is logged.
5. `rudder-scheduler` sees the approval and applies the change through the same path as `change apply`:
   commit with the switch's rollback timer, verify, confirm, or roll back. A device that changed since the
   approval is refused (`drifted`), nothing applied.
6. The result goes to the approval channel and to your chat thread. The ticket keeps who asked, who
   decided, and every step.

Scheduled changes (`apply at 02:00`) are approved in advance; 30 s before the start the approval channel
gets a notice with a code to stop it (`/pushno`); no answer means it runs. See [SCHEDULER.md](SCHEDULER.md).

**A line the device refuses is a failed change.** When the dry run is rejected by the device ("invalid
command"), no approval is opened and nothing is changed; the ticket is kept as `failed` with `attention`,
its change page shows FAILED (needs human attention) and the refused line, and an alert (❌ … FAILED: needs
attention) goes to the approval channel and to the chat thread. The agent must find out why, find lines that
work and record it with `change action=investigate` (finding, alternative, new ticket): the page shows it and
both channels get it (🔎). The alternative is a new change with its own approval. Connection errors
(unreachable, login) are not changes: the propose fails as before.

## 2. One approval method at a time

| Method | Where the request goes | How the approver answers |
|---|---|---|
| `telegram` (default) | the approval bot's Telegram (its home channel, or `APPROVAL.CHANNEL`) | `/pushok CODE` (tap it) |
| `slack` | a private Slack channel (`APPROVAL.CHANNEL`, e.g. `#approvals`) | `!pushok CODE` (no @mention needed in the approval channel) |
| `teams`, `discord`, `mattermost`, `matrix`, `whatsapp`, `signal` | the channel the host sends to (`APPROVAL.CHANNEL`; required for teams / mattermost / matrix) | the host's own `/pushok` handler calls **`rudder-approve`** (section 13) |
| `api` | reserved: an approval system's API (ServiceNow, Remedy…) | not built yet |

Only the configured method counts. With `slack`, a Telegram `/pushok` is refused ("approvals for this request
are taken on slack only") and the code stays open for the right channel.

## 3. Settings

All approval settings live in **one APPROVAL section for the host**, in `~/.config/rudder/rudder.local.yml`,
used by RUDDER and NAVIGATOR. Change them only with `navigator-setup` (section 5).

| Setting | Default | Meaning |
|---|---|---|
| `APPROVAL.METHOD` | `telegram` | `telegram` or `slack` |
| `APPROVAL.BOT` | – | the approval bot's Hermes profile. **Never the chat bot**: the bot that asks must not approve |
| `APPROVAL.CHANNEL` | empty | Slack: the private approval channel's ID (`C…`, required). Telegram: a chat ID, or empty for the bot's home channel |
| `APPROVAL.APPROVERS` | `*` | `*` = anyone in the approval channel (the organization curates who is in it), or a list of user IDs |
| `APPROVAL.SELF_APPROVE` | `true` | `false`: the person who asked for a change cannot approve it (they can still reject it). Not asked in the interview; an approver turns it off with `/selfapprove off` (Slack: `!selfapprove off`) in the approval channel; only `navigator-setup set APPROVAL.SELF_APPROVE true` turns it back on |
| `APPROVAL.SHIP_ENGINEERS` | none | user IDs whose changes need no approval, **only in lab inventories** (`LAB_INVENTORIES`), never scheduled, never PRODUCTION. Still a ticket: audited, announced on the approval channel, searchable |
| `APPROVAL.TTL_MIN` | `30` | minutes a ticket may wait for approval and apply; then it expires, nothing changes, the device lock is released |
| `APPROVAL.REPLY_PROFILES` | none | chat bots (Hermes profiles) allowed to post results back in the thread a change came from |
| `APPROVAL.WEB_BIND` | empty | `address:8790` to serve the change pages on; empty = no pages, the message carries the diff |
| `APPROVAL.WEB_URL` | `https://<WEB_BIND>` (or `http://` without a certificate) | the base the page links use (a VPN or DNS name later) |
| `APPROVAL.WEB_TLS` | `auto` | `auto`: HTTPS when the local certificate exists, else HTTP; `on`: HTTPS or no pages; `off`: HTTP |
| `APPROVAL.PAGES_KEEP_DAYS` | `90` | days a **finished** ticket, its change page and its baseline records are kept; then the daily clean-up removes them (open and scheduled tickets are never removed; the audit log is never trimmed). Replaces `WEB_KEEP_MIN` |
| `APPROVAL.CLEANUP_AT` | `02:00` | the daily clean-up time, local (`SCHEDULE.TIMEZONE`), run by rudder-scheduler; `off` = never. If the scheduler was down then, it runs at its next pass that day |

The other host settings, in `~/.config/navigator/navigator.yml`:

| Setting | Meaning | Checked |
|---|---|---|
| `PROJECT_ROOT` (required; old name `WORKSPACE`) | the folder that holds project folders (data only, never software) | absolute, exists, readable and writable |
| `SKILLS_ROOT` (optional; old name `EXTRA_SKILLS`) | a folder of your own skills, one `<skill>/SKILL.md` each, read-only | absolute, exists, readable, has at least one skill |

## 4. What the push-approve plugin checks

A RUDDER or NAVIGATOR code carries its rules in the pending file. A decision is recorded only when:

1. the answer came from the configured **method** (telegram / slack) and, if a channel is set, from **that
   channel** (a thread inside it counts);
2. the sender is an **approver** (`*` = anyone in the channel);
3. for an approval with `SELF_APPROVE: false`, the sender is **not the requester**.

Who decided is written on the decision and lands on the ticket (`decided_by`) and in the audit log. Refusals
are logged in `~/.hermes/push-approvals/decisions.log`. Codes from older tools (plain pending files, e.g.
nv-push) work as before.

## 5. Setting it up and changing it later: `navigator-setup`

Run on the host, in a shell. **It is never a chat tool**: an agent that could change where approvals go could
send them to itself.

```bash
navigator-setup                                   # the interview: everything, current values as defaults
navigator-setup show                              # current values
navigator-setup set PROJECT_ROOT /data/projects
navigator-setup set SKILLS_ROOT /data/skills
navigator-setup set APPROVAL.METHOD slack APPROVAL.CHANNEL C0123ABCDEF
navigator-setup add APPROVAL.APPROVERS U0123ABCDEF      # remove ... ; an empty list means "*" again
navigator-setup set APPROVAL.WEB_BIND 192.0.2.240:8790
navigator-setup set APPROVAL.PAGES_KEEP_DAYS 90 APPROVAL.CLEANUP_AT 02:00
navigator-setup test-approval                      # send a test code the current way and wait for it
```

- Paths are checked before they are saved.
- A change to **how approving works** (METHOD, BOT, CHANNEL, APPROVERS, SELF_APPROVE, SHIP_ENGINEERS) is saved
  **only after a test code sent the new way was approved there**. If the test fails or times out, nothing
  changes.
- Each file is backed up (`*.bak-<time>`) before a change; every change is logged in
  `<STATE_PATH>/settings.log`.
- Afterwards: `systemctl --user restart rudder-scheduler`, and restart the MCP host (Hermes:
  `systemctl --user restart hermes-gateway`, or `/reload-mcp` in chat) so NAVIGATOR reads the new values.

## 6. Slack: one-time setup

1. Create a **private** channel for approvals (e.g. `#approvals`) and copy its ID (channel details → bottom).
   Keep it private: anyone who can join it can approve when `APPROVERS` is `*`.
2. In the **approval bot's** Slack app (api.slack.com → the app; here **ApprovalBot**):
   - **Event Subscriptions → bot events:** `app_mention` and `message.groups` (private channels).
   - **OAuth scopes:** `app_mentions:read`, `groups:history`, `chat:write`.
   - **Reinstall to Workspace** when Slack asks.
3. In the channel: `/invite @ApprovalBot`. Only the approval bot belongs there, never the chat bot.
4. Let the approval bot take `!pushok` in that channel **without an @mention**: in the approval bot's
   Hermes profile `.env`, `SLACK_FREE_RESPONSE_CHANNELS=<channel ID>`, then restart the gateway. (In other
   channels a Slack bot only reacts to a real mention picked from Slack's list; a typed or pasted
   "@ApprovalBot" is plain text and is dropped silently.)
   **Required with it:** the approval bot's push-approve plugin filters that channel: only `!pushok` /
   `!pushno` (also `/pushok`, with or without backticks or an @mention) pass, as the real command; every
   other message there is dropped before the bot's agent sees it. Without that filter an agent that reads
   a code could act on it.
5. On the host: `navigator-setup set APPROVAL.METHOD slack APPROVAL.CHANNEL <C…>` and approve the test code
   in the channel with `!pushok CODE`.

Slack shows the app as "ApprovalBot"; the gateway log calls its bot user `approvalbot`. Same bot.

## 7. The approval message

On Slack each command is its own copyable code span; paste it as it is:

```
🔐 Change C20261114-ab12cd needs approval
Requested by Pat (U0123ABCDEF) via ApprovalBot
Ticket ID: CHG0012345
add Loopback97 on QALEAF1A
Devices: QALEAF1A
Diff:
  [QALEAF1A]
  +interface Loopback97
  +   description slack-approval
✅ Validation (batfish): PASS: no new parse errors, undefined references or broken BGP sessions
🔗 Full change and its progress: https://192.0.2.240:8790/c/…

Approve:  `!pushok K7M2QX`
Reject:   `!pushno K7M2QX`
Expires in 30 min.
```

Telegram messages stay as they were (`/pushok CODE` is tappable there). With change pages on, a diff longer
than about 1,200 characters is only summarised in the message, with the link to the full change.

The **Validation** line is there only when change validation is on (`VALIDATION.MODE`,
[RUDDER](RUDDER.md#change-validation)). It shows ✅ PASS, ⚠️ WARN (with the first warnings) or ➖ SKIPPED (a
platform the validator cannot model yet). The result is part of the approved hash: `/pushok` approves this
change with this result. A FAIL never reaches this message: the change stops before an approval is opened,
and both channels and the change page say FAILED validation.

## 8. Change pages and ports

With `APPROVAL.WEB_BIND` set, **`rudder-scheduler` serves a read-only page per ticket on TCP 8790,
directly**: Python's standard HTTP server, no nginx or other proxy, nothing else to install.

- The page shows who asked, the outside ticket ID and project, each device's full diff, the configuration
  lines, verify commands, and the approval and apply progress. It refreshes every 10 s while the ticket is
  open.
- **It cannot approve, change or cancel anything.** The code is never stored. No scripts, no forms,
  `Cache-Control: no-store`, a strict Content-Security-Policy, `no-referrer`.
- Secret-looking values in the diff (password, secret, key, community, pre-shared key…) are masked.
- Each link is random per ticket (in the ticket file, never in a log) and dies `WEB_KEEP_MIN` after the
  ticket ends. Anything else returns 404.
- **HTTPS with the local CA** (`APPROVAL.WEB_TLS`, default `auto`): when `navigator-setup tls init` has
  made the certificate ([HTTPS.md](HTTPS.md)), the pages are served over HTTPS and the links say `https://`.
  Plain HTTP to the port is dropped. Approvers' browsers trust the pages once the CA file
  (`~/.config/navigator/tls/ca.pem`) is imported:

  | Device | Import `ca.pem` as a trusted root |
  |---|---|
  | Windows | `certutil -user -addstore Root ca.pem` (Chrome and Edge use it) |
  | macOS | Keychain Access → System → import, then set "Always Trust" |
  | iPhone / iPad | send the file, install the profile, then Settings → General → About → Certificate Trust Settings → on |
  | Android | Settings → Security → Encryption & credentials → Install a certificate → CA certificate |
  | Firefox | Settings → Certificates → Import (Firefox keeps its own list) |

  Without it the browser warns ("not private"); the page is still encrypted, but check the CA fingerprint
  (`navigator-setup tls show`) before you accept. `WEB_TLS on`: HTTPS or no pages. `off`: plain HTTP.
  After `navigator-setup tls renew`: `systemctl --user restart rudder-scheduler`.
- Open the port only for the approvers' networks:

  ```bash
  sudo ufw allow from 192.0.2.128/25 to any port 8790 proto tcp comment 'RUDDER change pages'
  ```
- From outside later: use a VPN (Tailscale, WireGuard…), add its name to the certificate
  (`navigator-setup tls renew --name <vpn-name>`) and set `APPROVAL.WEB_URL https://<vpn-name>:8790`.
  Do not open 8790 to the internet.

| What | Runs as | Listens on | Firewall |
|---|---|---|---|
| `navigator-mcp` (the front door) | started by the MCP host (stdio) | nothing | – |
| `atlas-link` | systemd user service | Unix socket `~/.atlas/link.sock` (600) | – |
| `rudder-scheduler` | systemd user service | nothing, unless change pages are on | – |
| **change pages** | inside `rudder-scheduler` | **TCP 8790** on the `WEB_BIND` address only | **open for the approvers' networks** |
| SearXNG | Docker | 127.0.0.1:8888 | – |
| delegated pyATS MCP | started by RUDDER per call | 127.0.0.1, a free port, during the call | – |

## 9. SHIP engineers

`APPROVAL.SHIP_ENGINEERS` lists **people** (user IDs), not a role the bot can take on. Their changes run without
`/pushok` only when all of these hold: the inventory is a lab (`LAB_INVENTORIES`), the change is not scheduled,
no device is PRODUCTION (always refused anyway), and the requester comes from the MCP host (`requested_by`),
never from the model. The change is still a ticket: audited ("approved without /pushok: SHIP engineer …"),
announced in the approval channel ("runs WITHOUT approval"), reported and searchable. Enable it only on hosts
that fill `requested_by` (on Hermes: the `navigator-reply` plugin).

## 10. Finding tickets

`change action=list` with any mix of:

| Filter | Example |
|---|---|
| `since` / `until` | `2026-11-14`, `2026-11-14 18:00` |
| `device` | `QALEAF1A` |
| `inventory` | `EVPN_LAB` |
| `requested_by` | `Pat` or `U0123ABCDEF` (part of a name is enough) |
| `platform` (device type) | `EOS`, `IOSXE`, `PANOS` |
| `state` | `waiting_approval`, `approved`, `applied`, `failed`, `rejected`, `expired`, `cancelled` |
| `ticket_id` | `CHG0012345` |
| `project` | `CHG_TEST` |
| `limit` | default 50 |

In chat: "changes on QALEAF1A today", "changes requested by Pat this week", "failed changes", "changes for
CHG0012345".

**Outside ticket IDs.** `project create` takes an optional `ticket_id` (a ServiceNow `CHG0012345`, a Remedy
ID…). Every change made in that project carries it and can be found by it. Without one, RUDDER's own ticket
IDs are used as always.

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `navigator-setup`: "could not send … (hermes exit 1)"; `hermes send`: `channel_not_found` | the approval bot is not in the private channel: `/invite @ApprovalBot` |
| `!pushok` in Slack does nothing, nothing in the gateway log | the approval channel is not a no-mention channel for the bot (`SLACK_FREE_RESPONSE_CHANNELS`, step 4 of section 6; then restart the gateway), or its app lacks the `message.groups` event. Without it, only a mention picked from Slack's list works (`@ApprovalBot !pushok CODE`); a pasted "@ApprovalBot" is plain text |
| `/pushok` in Slack: "not a valid command" | Slack has no such slash command; use `!pushok CODE` |
| "⛔ Not recorded: approvals for this request are taken on slack only" | answered on Telegram while the method is Slack: answer in the approval channel |
| "⛔ Not recorded: you asked for this change…" | `SELF_APPROVE: false`: another approver must approve |
| Messages to the chat bot go missing, or changes appear with no ticket | a second program uses the same Slack app (Slack splits events between connections): one Slack app per bot instance |
| The page link does not open | firewall: allow TCP 8790 from your network; the link may also have expired (`WEB_KEEP_MIN`) |
| A setting change has no effect | restart `rudder-scheduler` and the MCP host |

## 12. Keep the approval bot's agent away from approvals

The approval bot is a Hermes profile with an AI agent of its own. That agent must never see a code or touch
the approval machinery. On 2026-11-14 a code pasted with backticks was not recognised as a command, reached
the agent as chat, and the agent approved it by calling the plugin itself (the user clicked its command
prompt). Since then the push-approve plugin also:
- **filters the approval channel** (`pre_gateway_dispatch`): only approval commands pass; everything else is
  dropped and logged (`DROPPED … in the approval channel`);
- **blocks the agent's tools** (`pre_tool_call`) from the approval folder, the plugin, `push_approval.py`,
  ticket / lock / audit files and `decision:` / `pending:` code hashing (`BLOCKED agent tool …`).

These are pattern guards: an agent with a shell on the same host is not fully contained by them. The strong
setup is an **approval bot with no agent tools** (a Hermes profile with terminal, code, file and web tools
disabled, its own Slack app or Telegram bot), used only for approvals.

## 13. Any host: `rudder-approve`

Hosts other than Hermes (OpenClaw, a Teams bot, your own Slack app, a ChatOps service…) record decisions
with one command. Their **own** `/pushok` handler (gateway code that knows who typed the command, never a
model tool) runs it and posts the line it prints back to the person:

```bash
rudder-approve ok CODE --user U0123ABCDEF --name "Pat" --platform slack --channel C0123ABCDEF
rudder-approve no CODE --user 123456789 --platform telegram --channel -1001234567890
rudder-approve check                   # validate the config file
```

Exit code 0 = recorded, 1 = refused or no such code (the code stays open), 2 = usage or config error.
`--json` prints `{"recorded", "verdict", "message"}`. `--parent` is the channel a thread belongs to.

**The config file is required**: `--config PATH`, else `$RUDDER_APPROVE_CONFIG`, else
`~/.config/rudder/approve.yml`. **`navigator-setup` writes it** (no hand editing needed):

```bash
navigator-setup                                          # the interview asks: RUDDER-APPROVE ... yes/no
navigator-setup set APPROVE.PLATFORMS teams APPROVE.CHANNELS teams=19:abc@thread.tacv2 APPROVE.APPROVERS '*'
navigator-setup add APPROVE.PLATFORMS discord            # add / remove work on PLATFORMS, CHANNELS, APPROVERS
navigator-setup add APPROVE.CHANNELS discord=1234567890
navigator-setup add APPROVE.APPROVERS U0123ABCDEF
navigator-setup set APPROVE.PLATFORMS ""                 # off: the file is moved aside, every call is refused
navigator-setup show                                     # the APPROVE.* rows
```

Before saving, setup runs rudder-approve's own checks on the new file. A bad platform, a channel for a
platform that isn't listed, or a malformed ID is refused and nothing changes. The file is written mode 600,
the old one is backed up, and the change is logged in `settings.log`. Setup warns when `APPROVAL.METHOD` is
not in `APPROVE.PLATFORMS`, because rudder-approve would then refuse every approval sent there. After a
change, `navigator-setup test-approval` checks the whole path. The interview's defaults come from the
APPROVAL settings (method, channel, approvers).

The file must belong to the user that runs the command and must not be writable by
group or others (`chmod 600`). Example: `examples/rudder-approve/approve.yml`.

| Key | Meaning |
|---|---|
| `APPROVAL_DIR` | the decision store; the same folder as RUDDER's `APPROVAL.APPROVAL_DIR` |
| `PLATFORMS` | required: the platforms this host takes approvals from (`slack`, `telegram`, `teams`, …) |
| `CHANNELS` | optional per platform: only these channel / chat IDs |
| `APPROVERS` | `*` or user IDs: a host-wide list, checked on top of each request's own approvers |
| `LOG` | every call, accepted or refused; codes are never written (default `~/.rudder/approve.log`) |

Checks, the same as the push-approve plugin (section 4) plus the host's own gates: platform in `PLATFORMS`;
channel in `CHANNELS`; user in `APPROVERS`; then the request's rules: method = platform, its channel, its
approvers, and `SELF_APPROVE` (the requester may reject but not approve). Requests without rules (old
plain-text codes) are refused here. A second decision for the same code is refused.

Set RUDDER's side with `navigator-setup set APPROVAL.METHOD teams APPROVAL.CHANNEL <id>` so the request is
sent where the host listens. The config holds no keys today; **encrypting secrets in host configs is
planned** (Bitwarden Secrets Manager is one option; decision #140).
