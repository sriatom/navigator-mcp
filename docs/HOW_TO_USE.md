# How to use NAVIGATOR

The same on every host (a Slack or Telegram bot on Hermes, Claude Code, Cursor, OpenClaw): you talk in
plain words, the agent picks NAVIGATOR's tools. This page is what you can ask, what happens, and where
things end up. The numbered test plans are TESTING_CHAT.md and
TESTING_CLAUDE_CODE.md; the phrase-to-tool table is [CHEATSHEET.md](CHEATSHEET.md).

## 1. The five things you do

| You want to | Say, for example | What happens |
|---|---|---|
| **See** what exists | "list inventories", "show the spines in EVPN_LAB" | ATLAS answers; no device is touched |
| **Read** devices | "show bgp summary on the spines in EVPN_LAB" | RUDDER runs allowlisted commands; every output is saved |
| **Change** a device | "add Loopback96 on QALEAF1A with description test" | a **ticket** with a dry-run diff; the approval request goes to your approval channel, not this chat |
| **Work a job** | "new LOCAL project BGP_AUDIT on EVPN_LAB: check all BGP sessions" | a project folder with a task, a run log and records |
| **Fix the automation** | "be the ship engineer: pyATS reads on EDGE1 time out" | SHIP_ENGINEER troubleshoots, recorded ([SHIP_ENGINEER.md](SHIP_ENGINEER.md)) |

No project is needed for a quick read: inventory, devices and commands are enough.

## 2. Reads

- Name the inventory once; the agent keeps it for the conversation.
- Devices by name or group: "all leaves", "QALEAF1A and QALEAF1B".
- Commands by name ("show ip route") or category ("bgp", "interfaces"). Only commands on the platform's list
  run: "which commands can I run on EOS?" shows them. In a lab inventory the host may allow any plain `show …`.
- Options: "… via ssh" (another connection method, once), "compare via ssh and pyats", "raw" (the device's own
  text), "with debug" (the connection log, secrets removed).
- Outputs are saved under the project's `RECORDS/DEVICES/` (or RUDDER's records folder for ad hoc reads).

## 3. Changes

1. Say what you want, all of it in one message if it belongs together:
   *"shutdown Loopback98, description of Loopback99 'Hello Moto', OSPF point-to-point on Ethernet1"*.
2. The agent proposes **one ticket** with the exact lines. RUDDER makes a dry run on the device and shows
   the diff. **If the device refuses a line, the change has failed and needs you:** the ticket is kept as
   `failed` with an attention flag, its page says FAILED in red, and an alert goes to the approval channel
   and your chat thread. Nothing is changed and no approval is opened. The agent then investigates why
   (for example EOS takes no OSPF network type on a Loopback), finds lines the device accepts, records the
   finding on the ticket (shown on its page, sent to both channels), tells you, and proposes the alternative
   as a new change with its own approval. It never leaves a requested item out on its own; if a later
   proposal does, the request and the page say `NOT in this change`.
3. The request goes to the **approval channel** with a one-time code and a link to the change page. You (or
   another approver) answer there: `/pushok CODE` or, on Slack, `!pushok CODE`. `/pushno CODE` rejects.
4. RUDDER applies it within seconds: drift check, commit with a rollback timer, verify, confirm, or roll
   back. **One result** is posted in the approval channel and **one** in your chat thread (Hermes hosts).
   On Claude Code / Cursor, ask "status of C…".
5. Later: "changes on QALEAF1A today", "my changes this week", "tickets for CHG0012345".

Scheduled: *"…apply at 02:00"*: approved now, applied at that time after a 30-second notice with a stop
code ([SCHEDULER.md](SCHEDULER.md)). Removing what you just added is a new change with its own approval.
Finished tickets and their pages are kept 90 days by default (`APPROVAL.PAGES_KEEP_DAYS`).

## 4. Projects: the folder that holds a job

`PROJECT_ROOT` (set in `navigator-setup`, for example `/data/projects`) holds one folder per project.
Jobs without a name go to `PROJECT_ROOT/ADHOC_WORK/<JOB>_<time>/`.

```
PROJECT_ROOT/BGP_AUDIT/
├── TASK.md            what the job is (from your words), the outside ticket ID if any
├── RUNLOG.md          one line per step: who (role), what, result. The job's history
├── NAVIGATOR_STATE.json  status (active / waiting_input / done), phase, the open question
├── .navigator_mode    CLOUD or LOCAL
├── .navigator_inventory  the inventory the project is tied to
├── RAW/               what you bring: logs, configs, captures (the agent reads from here)
├── RECORDS/           what NAVIGATOR collects: DEVICES/ (every read), TROUBLESHOOT/, PUBLIC_QUERIES.md
├── CONFIG/  LOGS/     working files of the roles
└── OUT/               results for you: reports, generated configs, local model answers
```

Projects made earlier by NAVI have `NAVI_STATE.json`, `.navi_mode` and `.navi_inventory` instead. NAVIGATOR
reads and updates those in place and never renames them.

A project follows the model: its mode is CLOUD or LOCAL, the same words as the chat model.

- **CLOUD**: cloud models may see the project's data.
- **LOCAL**: the data stays local. Web searches and lessons must be generic, and a **cloud** chat model does
  not see the files or device output: `local_model` lets the local model read them and writes its answer to
  `OUT/` ([LOCAL_MODELS.md](LOCAL_MODELS.md)).
- A new project takes the model the chat runs on when you create it; say "as CLOUD" or "as LOCAL" to choose.
  A folder without a mode file counts as LOCAL. LOCAL → CLOUD needs your `/pushok`, like a change;
  CLOUD → LOCAL is immediate. Projects marked OPEN or LOCKED (the older words) read as CLOUD or LOCAL.
- A project with an inventory works only on that inventory.
- Pass the project in your words ("in BGP_AUDIT, …"): records and run-log lines go there.
- Say "project status", "close BGP_AUDIT: all sessions up", or "what did we do in BGP_AUDIT?" (the run log).
- Give the outside ticket when you create it ("… with ticket CHG0012345"): every change ticket then carries it.

### The chat model: CLOUD or LOCAL

Where the bot's own thinking runs. On Hermes you set it per thread: `/model cloud` or `/model local` (any
case). The default is LOCAL when the profile has a local model. Asking for a LOCAL project on a CLOUD thread
switches that thread to LOCAL first, and a project card shows the folder, mode, inventory and model. LOCAL
replies are slower. Claude Code and Cursor run on a cloud model. The rules for each pair are in
[LOCAL_MODELS.md](LOCAL_MODELS.md#words-cloud-and-local).

## 5. Roles, QC and lessons

For bigger jobs the agent hands work to roles (subagents with a brief): DATA_COLLECTOR, STATE_VALIDATOR,
LOG_REVIEWER, ANALYZER, CONFIG_GENERATOR, CONFIG_PUSHER, DOCUMENTOR, SHIP_ENGINEER, and SANITY_AGENT, the
QC gate every result passes before you see it ([ROLES_AND_PACKS.md](ROLES_AND_PACKS.md)). Mistakes worth
remembering become lessons ("add a lesson: …"), which later briefs include.

## 6. Before data goes to a public model

"Scramble RAW in BGP_AUDIT" removes secrets (default) or also public IPs, ASNs, names (deep) into
`SANITIZED/`; "restore" puts the real names back in an answer. `research` searches the web through the local
SearXNG; in LOCAL projects only generic questions pass.

## 7. What the agent never does

- asks for or shows a password (credentials are set on the host: `atlas-interview secret`);
- approves anything, or asks you for an approval code in the chat;
- changes a device outside a ticket, or touches PRODUCTION devices;
- writes a change result itself (only RUDDER reports results).

If you see any of these, stop and tell the admin.
