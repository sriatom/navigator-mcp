---
hide:
  - toc
---

# Introduction

**Your network architect and engineer buddy.** An AI agent that works through the job with you, from the
inventory to the approved change, and never holds a password or changes a device on its own.

## What is NAVIGATOR?

NAVIGATOR is an MCP server that you add to the AI agent you already use: Hermes, NetClaw, OpenClaw, Claude
Code, Cursor or any other MCP client. It turns that agent into a network engineer that works the way a good
team works:

- **It knows your network.** One inventory of devices, controllers and out-of-band gear, with encrypted
  credentials that the agent refers to by name only.
- **It reads safely.** Only allowed show commands, through the right connection method for each device,
  and every output is saved.
- **It changes nothing without you.** Every change is a ticket with a dry-run diff. A person approves it
  outside the chat, then it is applied, verified, and rolled back if the check fails.
- **It works as a team.** Nine specialist roles do the work, and a QC role checks every result before you
  see it.
- **It keeps private data private.** A project marked LOCAL never sends its data to a cloud model.

No AI model runs inside NAVIGATOR: your agent does the thinking, and NAVIGATOR does the checking.

## The problem

Every network tool for agents brings its own inventory format, its own password handling and its own idea of
"approval", often a field the model fills in itself. Give an agent several such tools and every safety rule
must be repeated in each one. One forgotten rule is a way around all the others.

NAVIGATOR puts **one front door** in front of all of them. Every call passes the same checks:

| Check | What it means |
|---|---|
| Which job | The work belongs to a project, with a task file, a run log and saved records. |
| Which data may leave | A LOCAL project's data never goes to a cloud model; a local model reads it instead. |
| Which devices and commands | Only allowed inventories, credential tags and read commands. |
| Which changes | A change is a ticket. A person approves it outside the chat, with a one-time code. |

## A team of specialists, taught by vendor packs

![NAVIGATOR roles and vendor packs](roles-and-packs.svg#only-light)![NAVIGATOR roles and vendor packs](roles-and-packs-dark.svg#only-dark)

You talk in plain words. The lead engineer (NAVIGATOR) hands the work to the right roles:

| Role | Job |
|---|---|
| **DATA_COLLECTOR** | Bulk reads by category (system, interfaces, BGP…) on many devices. Every output saved. |
| **STATE_VALIDATOR** | Targeted reads and a verdict per device, with the evidence: "are all EVPN peers up?" |
| **LOG_REVIEWER** | Root cause from logs, every claim cited to a log line. |
| **ANALYZER** | Research and comparison: your config against the vendor's recommendation. |
| **CONFIG_GENERATOR** | Config with its rollback and verify commands, ready for review. |
| **CONFIG_PUSHER** | A change ticket, your approval outside the chat, apply, verify, roll back if needed. |
| **DOCUMENTOR** | The change record and the project report, in the project folder. |
| **SANITY_AGENT** | The QC gate: an 8-point checklist and a recorded verdict on every result. |
| **SHIP_ENGINEER** | Keeps the automation itself healthy: inventories, logins, connection methods. |

**Teach it a vendor once, and every role knows it.** Everything about one vendor lives in one vendor pack:
its platforms, the read commands by category, the connection methods, and notes for the roles. Add a
FortiGate pack, and DATA_COLLECTOR gets the FortiGate commands for bulk collection, STATE_VALIDATOR gets the
verify commands, CONFIG_GENERATOR and CONFIG_PUSHER learn how FortiGate changes are written and rolled back.
No role changes. Ten packs ship today: Arista, Cisco, Palo Alto, Fortinet, Juniper, Aruba, F5, Check Point,
multivendor and a generic fallback ([Roles and vendor packs](ROLES_AND_PACKS.md)).

## How it fits with your agent

![How NAVIGATOR fits in](front-door.svg#only-light)![How NAVIGATOR fits in](front-door-dark.svg#only-dark)

1. **You talk where you already talk**: Slack, Telegram, Teams, Discord, or a terminal or IDE.
2. **Your agent host** (Hermes, NetClaw, OpenClaw, Claude Code, Cursor…) runs the AI model and starts
   NAVIGATOR as an MCP server. NAVIGATOR needs no code inside the host.
3. **NAVIGATOR** checks every call. Behind it, **ATLAS** holds the inventory and the credentials, and
   **RUDDER** is the only part that reaches a device.
4. **A person approves every change** on a separate approval channel. The chat bot can ask for a change, but
   it can never approve one.

More: [Architecture](THREE_LAYERS.md).

## First time here?

<div class="grid cards" markdown>

-   :material-download: **[Install](INTEGRATION.md)**

    ---

    Install NAVIGATOR on a Linux host, then connect Hermes, NetClaw, OpenClaw, Claude Code or Cursor.
    One page, copy and paste.

-   :material-cog: **[Day 0: set up](DAY0.md)**

    ---

    Approvals, services, HTTPS and the recovery key.

-   :material-lan: **[Day 1: bring in your network](DAY1.md)**

    ---

    Inventory, credentials, the first read and the first approved change.

-   :material-test-tube: **[Try it on a lab](LABS.md)**

    ---

    Two ready containerlab topologies to download: an Arista EVPN fabric and a Palo Alto firewall lab.

</div>

Already running? [Day 2: daily use](HOW_TO_USE.md) and the [cheatsheet](CHEATSHEET.md) of chat phrases.
