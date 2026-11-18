# Day 0: set up the host

After [Install](INTEGRATION.md), before any device. Everything here runs on the NAVIGATOR host's shell.
None of it can be done from a chat: an agent that could change where approvals go could approve its own
changes.

## The order

| Step | Command | Done when |
|---|---|---|
| 1. Host settings | `navigator-setup` | `navigator-setup show` lists your folders and approval channel |
| 2. Approval channel | Slack app or Telegram bot, then `navigator-setup test-approval` | a test code is approved in the channel |
| 3. Services | `systemctl --user enable --now atlas-link rudder-scheduler` | both `active` |
| 4. Recovery key and first backup | asked by `navigator-setup` | the key is stored away from the host, the backup file exists |
| 5. HTTPS (optional) | asked by `navigator-setup`, or `navigator-setup tls init` | `navigator-setup http show` lists the URLs |
| 6. Connect the agent host | [Install, step 3](INTEGRATION.md) | `/mcp` lists 16 tools |

## 1. Host settings

```bash
navigator-setup            # Enter keeps the value in [brackets]
navigator-setup show
```

The interview asks:

- **PROJECT_ROOT**: the folder that holds one folder per project. Data only, never software.
- **SKILLS_ROOT** (optional): a folder of your own skills, read-only.
- **Approvals**: the method (Slack, Telegram, or another platform through `rudder-approve`), the approval bot,
  the channel, who may approve, and whether the requester may approve their own change.
- **Retention**: how long finished tickets and their pages are kept (default 90 days).
- **Models**: whether your agent host runs a cloud or a local model, and where a local model is
  ([Cloud and local models](LOCAL_MODELS.md)).
- **HTTPS** for agents on other machines, and the **recovery key**.

Every question and its checks: [The setup interview](INTERVIEW.md).

## 2. The approval channel

Two bots, never one: the chat bot asks for changes, and a **separate** approval bot posts the request and
takes `/pushok`. With one bot, a model that controls it could approve its own change.

- **Slack**: a private channel such as `#approvals`, a second Slack app invited to it, and
  `navigator-setup set APPROVAL.METHOD slack APPROVAL.CHANNEL <channel ID>`.
- **Telegram**: a second bot; requests go to its home chat.
- **Teams, Discord and others**: your host's own `/pushok` handler calls `rudder-approve`.

A change to how approving works is saved only after a test code sent the new way was approved there:

```bash
navigator-setup test-approval
```

Step-by-step Slack setup and every setting: [Approvals](APPROVALS.md).

## 3. Services

```bash
systemctl --user enable --now atlas-link rudder-scheduler
systemctl --user is-active atlas-link rudder-scheduler
```

- **atlas-link** holds the credential key and serves credentials to RUDDER only.
- **rudder-scheduler** applies approved changes, runs scheduled changes, serves the change pages and runs
  the daily clean-up.

## 4. Recovery key and backups

The interview makes a **recovery key** (48 digits) for the credential key and shows it once. Keep it away
from the host, for example in a password manager. Then it offers a first encrypted backup. A backup and the
recovery key restore everything on a new host: [Backup and restore](BACKUP.md).

## 5. Reaching NAVIGATOR from other machines

| How | When |
|---|---|
| stdio on the same host | the agent host runs on the NAVIGATOR host; nothing listens on the network |
| stdio over SSH | Claude Code or Cursor on your PC; needs only SSH key access |
| HTTPS with logins, port 8791 | agents on other machines or sites; a user and password per person |

[Where it runs](HOSTING.md) · [HTTPS for remote clients](HTTPS.md)

Next: [Day 1: bring in your network](DAY1.md).
