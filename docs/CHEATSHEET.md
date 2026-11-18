# NAVIGATOR — cheatsheet

One MCP server, **navigator**, is the front door (P7d). You talk to the bot in plain words; these are the
phrases that work and the tool + action they map to. Inside it run three layers:

| Layer | Component | What it does |
| :--- | :--- | :--- |
| 1 | **ATLAS** | Inventories, platforms, allowed read commands, credential *tags* (never values), rendered files |
| 2 | **RUDDER** | Reads devices (any connection method); proposes and applies changes after your `/pushok` |
| 3 | **NAVIGATOR** | Projects, CLOUD / LOCAL, the 9 roles, QC, lessons, scrambler, research, troubleshooting |

Every inventory, device, change and credential call is checked first: the project and its mode (CLOUD or LOCAL;
a project with an inventory works only on it), the inventories this consumer may use, the command allowlist.

## Before you start (host)
The ATLAS link must be running: RUDDER gets credentials through it. It is a user service
([ATLAS.md](ATLAS.md#the-atlas-link-service)):
```bash
systemctl --user status atlas-link          # check
systemctl --user restart atlas-link         # after a code update, or if RUDDER says "ATLAS link is not running"
journalctl --user -u atlas-link -f          # log
systemctl --user status rudder-scheduler    # scheduled changes (docs/SCHEDULER.md)
docker ps --filter name=searxng             # web search for research (docs/SEARXNG.md)
```
After a NAVIGATOR code update, restart the gateway: Hermes caches each server's tool list
(`<profile>/cache/mcp_schema_cache.json`) and keeps the old tools while the server's config entry is unchanged
(seen 2026-11-13: the bot only knew the pre-P7d tools).
```bash
systemctl --user restart hermes-gateway     # all bots reconnect in ~20 s
```
In chat: `/reload-mcp` (or start a new session) after the server was added or changed.

## Host settings (`navigator-setup`)
Host settings are changed in the host's shell, never in chat: if the agent could change where approvals go, it
could send them to itself. Run these on the NAVIGATOR host. `navigator-setup` is in `~/.local/bin`, so open a
new shell or run `source ~/.profile` first. Every change is checked before it is saved, the old file is backed
up, and the change is logged in `settings.log`.

| Command | Why |
| :--- | :--- |
| `navigator-setup show` | every current value, and where it comes from |
| `navigator-setup set KEY VALUE [KEY VALUE ...]` | change one or more keys in one go |
| `navigator-setup add KEY VALUE` / `remove KEY VALUE` | add to or remove from a list, e.g. `APPROVAL.APPROVERS` |
| `navigator-setup` | the setup interview again, from the start |
| `navigator-setup test-approval` | send a test code the current way and wait for your `/pushok` |

After a change, restart what reads the settings, then send `/reload-mcp` in chat:
```bash
systemctl --user restart rudder-scheduler navigator-mcp-https hermes-gateway
```
Only move the schema cache aside after a code update, not after a settings change.

### Approvals ([APPROVALS.md](APPROVALS.md))
Keys marked **tested** change how approving works. They are saved only after a test code, sent the new way,
was approved there.

```bash
navigator-setup set APPROVAL.SELF_APPROVE false        # tested; default true
navigator-setup add APPROVAL.APPROVERS U0123ABCD        # tested; "*" = anyone in the approval channel
navigator-setup add APPROVAL.SHIP_ENGINEERS U0123ABCD   # tested; user IDs only, never "*"
navigator-setup set APPROVAL.METHOD slack APPROVAL.CHANNEL C0123ABCD   # tested; default telegram
navigator-setup set APPROVAL.TTL_MIN 60                 # how long a code is valid: 5-1440 min, default 30
navigator-setup set APPROVAL.PAGES_KEEP_DAYS 30         # keep finished tickets and pages: 1-3650 days, default 90
navigator-setup set APPROVAL.CLEANUP_AT 03:30           # daily clean-up time, local; default 02:00, off = never
```

| Key | Why |
| :--- | :--- |
| `APPROVAL.SELF_APPROVE` | `true` (default): whoever asks for a change may approve it, fine for a lab or a small team. `false`: someone else must approve |
| `APPROVAL.APPROVERS` | who may answer `/pushok` |
| `APPROVAL.SHIP_ENGINEERS` | people (user IDs) whose changes need no approval, only on lab inventories, never scheduled, never PRODUCTION. Still a ticket, audited and announced. Not the SHIP_ENGINEER role |
| `APPROVAL.METHOD`, `.BOT`, `.CHANNEL` | where approval requests go: the chat platform (telegram, slack, discord, teams, mattermost, matrix, whatsapp, signal), the approval bot's Hermes profile, and the channel ID (Slack: `C…` or `G…`) |
| `APPROVAL.TTL_MIN` | how long a `/pushok` code is valid |
| `APPROVAL.PAGES_KEEP_DAYS`, `.CLEANUP_AT` | how long finished tickets, change pages and baseline records are kept, and when the daily clean-up runs |

An approver can turn self-approval off from chat: `/selfapprove off` in the approval channel (on Slack:
`!selfapprove off`). There is no "on" in chat. Only the shell turns it back on
(`navigator-setup set APPROVAL.SELF_APPROVE true`), and that needs the test code.

### Models ([LOCAL_MODELS.md](LOCAL_MODELS.md))
```bash
navigator-setup set MODELS.LOCAL_URL http://127.0.0.1:11434/v1 MODELS.LOCAL_MODEL qwen3.5:9b
navigator-setup set MODELS.HOST local                   # cloud (default) | local
navigator-setup set MODELS.TIMEOUT_SEC 600              # 10-3600 s, default 300
```
- `MODELS.HOST`: the model your MCP host itself runs. `local` needs a local URL and model.
- `MODELS.LOCAL_URL`, `MODELS.LOCAL_MODEL`: the local model NAVIGATOR calls for LOCAL work. Setup refuses a URL
  that is not a local address (loopback, private or link-local), and a model the endpoint does not list.

### Change validation
The `VALIDATION.*` keys are in the "Change validation" part of Devices, further down this page.

### ServiceNow
Off by default. Saved only after a test login could read change requests.
```bash
navigator-setup set SERVICENOW.MODE link SERVICENOW.URL https://example.service-now.com SERVICENOW.CREDENTIAL SNOW_API
navigator-setup set SERVICENOW.MODE gate                # also needs the change request in Implement, inside its window
navigator-setup set SERVICENOW.MODE off
```
- `SERVICENOW.MODE`: `off` | `link` (read the ticket, write the results back) | `gate` (link, and a change runs
  only while its change request is in Implement and inside its window).
- `SERVICENOW.URL`: `https://<instance>.service-now.com`, no path.
- `SERVICENOW.CREDENTIAL`: the ATLAS credential *tag* of the ServiceNow account, never the password.
- `SERVICENOW.VERIFY_TLS` (default true), `SERVICENOW.CLOSE_STATE` (the state to close a change request with).

### Paths
```bash
navigator-setup set PROJECT_ROOT /data/projects         # required; must be readable and writable
navigator-setup set SKILLS_ROOT /data/skills            # optional; folders with <skill>/SKILL.md
```

## Inventory (`inventory`, `inventory_edit`)
| Say | Tool |
| :--- | :--- |
| "list inventories" | `inventory action=list` |
| "show SDWAN_LAB" / "show the spines in EVPN_LAB" | `inventory action=show` (groups = ROLE / SITE / FAB tag) |
| "which devices have OS IOSXE in SDWAN_LAB, with address and port" | `inventory action=query` |
| "validate EVPN_LAB and probe the ports" | `inventory action=validate probe=true` |
| "give me an inventory template" / "a credentials template" | `inventory action=template kind=inventory / credentials` |
| "what read commands are allowed on EOS" | `inventory action=commands platform=EOS` |
| "history of SDWAN_LAB" / "restore yesterday's version" | `inventory action=history`, `inventory_edit topic=inventory_restore` |
| "give me a pyATS testbed for EVPN_LAB" (or Ansible, ANTA, multivendor) | `inventory action=render format=pyats` — references like `%ENV{NAVI_CRED_<TAG>_PASSWORD}`, never passwords |

**Changes to an inventory always go through the interview:** start → answers → preview → your yes → commit.
- New inventory from a file: attach the filled `.yml` or `.csv` in chat and say
  *"create inventory LAB2 from this file"* → `inventory_edit topic=inventory_new`, then `topic=bulk_import`.
- Add / edit / remove one device: *"add LEAF9 10.0.0.9 EOS eAPI to LAB2"* → `topic=record_add / record_edit / record_remove`.
- Change many at once: *"set CREDENTIAL FAB2 on every LEAF in LAB2"* → `topic=bulk_edit` (addresses are refused here).
- **Never** paste or attach CREDENTIALS.yml or a password in chat. Set secrets on the host:
  `atlas-interview secret <draft> LOGIN_PASSWORD`.

## Credentials (`credential`)
| Say | Tool |
| :--- | :--- |
| "which credential tags are there, and who uses them" | `credential action=list` (fields and type only) |
| "test the login on EDGE1 in SDWAN_LAB" | `credential action=test` |
| "test the login on EDGE1 via pyats, with the debug log" | `credential action=test via=pyats debug=true` |

## Devices (`device`, `adapter`)
| Say | Tool |
| :--- | :--- |
| "show bgp summary on all spines in EVPN_LAB" | `device action=read` (commands must be on the platform list) |
| "collect the routing category from QA leaves" | `device action=read category=routing` |
| "run show version on EDGE1 **via pyats**" | `device action=read via=pyats` — this call only, nothing stored |
| "…and give me the raw text" / "…with the debug log" | `raw=true` / `debug=true` (connection log, secrets removed) |
| "compare show version on EDGE1 **via ssh and pyats**" | `device action=compare via="ssh,pyats"`: both outputs, timing, errors, diff |
| "which adapter serves PAN-OS?" | `adapter action=list` (also lists the via= names) |
| "use pyats for IOSXE in SDWAN_LAB from now on" | `adapter action=default platform=IOSXE via=pyats` → preview → your yes → `inventory_edit action=commit` |

Connection methods: `eapi` (Arista), `ssh` (Arista with `| json`, others as text), `netmiko` (text), `pyats`,
`multivendor`, `fortinet`, `vmanage`; `default` = the platform's own.

### A change (always approved out of band)
1. *"on QALEAF1A add `interface Loopback99` / `description test`"* → `change action=propose` → ticket + diff.
2. The approval request arrives on the **approval bot** (a separate bot, here called ApprovalBot). Answer there with `/pushok <code>` (valid 30 min).
3. *"apply ticket <id>"* → `change action=apply` (checkpoint, push, commit; rolls back if a line is refused).
4. `change action=status / list / cancel`.
- One kind of device per ticket; PRODUCTION devices get extra checks; IOS-XR writes are refused.

### Change validation (host shell, [RUDDER.md](RUDDER.md#change-validation))
With validation on, every proposal is checked before the approval (a fail blocks it; the approval message shows
`✅ / ⚠️ Validation (batfish): PASS / WARN: …`) and again after the commit (a fail rolls back).
```bash
docker run -d --name batfish --restart unless-stopped -p 127.0.0.1:9996:9996 batfish/allinone
navigator-setup set VALIDATION.MODE batfish VALIDATION.HOST 127.0.0.1    # saved after a test snapshot answers
navigator-setup set VALIDATION.MODE netclaw                              # NetClaw's Batfish (BATFISH_HOST / PORT)
navigator-setup set VALIDATION.ON_UNREACHABLE warn                       # default block
navigator-setup set VALIDATION.POST_PUSH false                           # no check after the commit
navigator-setup set VALIDATION.MODE none                                 # off
```

### A scheduled change ([SCHEDULER.md](SCHEDULER.md))
1. *"… apply at 2 AM"* → `change action=propose at="02:00"`. Approve on the approval bot whenever you like before then.
2. 30 s before: a notice with a code. `/pushno CODE` stops it, `/pushok CODE` starts it now, no answer = it runs.
3. The result arrives on the approval bot and in the chat thread. *"what is scheduled?"* → `change action=list`; *"move it to 01:30"* → `change action=schedule` (new `/pushok`).
- The agent cannot apply a scheduled ticket early. Not approved by the notice time, or the scheduler more than 10 min late = not applied.

## Connection methods and vendor packs

Every platform belongs to a vendor pack ([ROLES_AND_PACKS.md](ROLES_AND_PACKS.md)). Pick a method per call with `via=`:

| Say | Does |
|---|---|
| "show version on QALEAF1A" | the platform's default (Arista: eAPI, JSON) |
| "… via ssh" | Arista: SSH with `\| json`; Cisco / PAN-OS / SD-WAN Manager: SSH text |
| "show system info on PA-EDGE in PALAB1 via the XML API" | `via=xml-api`: PAN-OS op command over the XML API, JSON |
| "show system status on MAN1 in SDWAN_LAB" / "… via ssh" | SD-WAN Manager over its REST API (JSON) / its own CLI over SSH (text) |
| "show version on EDGE1 via pyats" | the delegated pyATS server (Genie JSON) |
| "compare show system info on PA-EDGE via ssh and xml-api" | both methods side by side, with times |
| "which methods does PANOS support?" | `inventory action=platform name=PANOS`: connection types, read / write owner, pack |
| "give me the inventory template" | one example record per vendor and connection method |

## Troubleshooting with SHIP_ENGINEER (`troubleshoot`)
| Say | Tool |
| :--- | :--- |
| "be the ship engineer: pyATS reads on EDGE1 time out" | `role action=brief role=SHIP_ENGINEER` (the bot runs it, or works as it) |
| "start troubleshooting: pyATS reads on EDGE1 time out" | `troubleshoot action=start title=…` |
| (then reads, compares, login tests with debug) | each one is recorded in `RECORDS/TROUBLESHOOT/<time>-<title>.md` |
| "note: SSH works in 2 s, pyATS needs 29 s to log in" | `troubleshoot action=note` |
| "stop troubleshooting: cause is the pyATS connect time" | `troubleshoot action=stop summary=…` |
| "show the troubleshooting log" | `troubleshoot action=show` (LOCAL: the file path only) |
- Read-only: anything that changes a device is still a ticket and your `/pushok`.

## Projects (`project`, `mode`, `role`, `qc`, `lesson`, `research`)
| Say | Tool |
| :--- | :--- |
| "new LOCAL project ACME_BGP: check BGP on the QA fabric" | `project action=create` (mode, description, inventory) |
| "list projects" / "show ACME_BGP" | `project action=list / show` |
| "mark ACME_BGP blocked: waiting for the maintenance window" | `project action=status` (writes NAVIGATOR_STATE.json) |
| "switch ACME_BGP to CLOUD" | `mode action=set` → `/pushok` on the approval bot (to LOCAL is immediate) |
| "brief a STATE_VALIDATOR for this" | `role action=brief` |
| "run QC on the result" | `qc action=check`, then `qc action=record` |
| "remember: EOS 4.33 needs `show bgp evpn summary`" | `lesson action=add` (refuses IPs / project name in LOCAL) |
| "close the project" | `project action=close` |
| "search the web: EOS 4.33 EVPN type-5 not installed" / "…and read the top 2 pages" | `research` (fetch=2): SearXNG, logged; LOCAL refuses specific queries ([SEARXNG.md](SEARXNG.md)) |

## Chat model (Hermes: `/model`)
| Say | Does |
| :--- | :--- |
| `/model local` (any case) | this thread thinks on the local model (the default when the profile has one); slower |
| `/model cloud` | this thread thinks on the cloud model; LOCAL device output stays withheld |
| "new LOCAL project X" on a cloud thread | the thread switches to local first, then a project card (folder, mode, inventory, model) |
| "local model status" | `local_model action=status` |

A new project takes the thread's model as its mode ("new project X" on a local thread is LOCAL).
The rules: [LOCAL_MODELS.md](LOCAL_MODELS.md#words-cloud-and-local).

Roles: ANALYZER, CONFIG_GENERATOR, CONFIG_PUSHER, DATA_COLLECTOR, STATE_VALIDATOR, LOG_REVIEWER, DOCUMENTOR,
SANITY_AGENT, SHIP_ENGINEER.

## Roles (sub-agents) and the QC gate

| Say | Does |
|---|---|
| "which roles do you have?" | the nine roles and what each does |
| "as DATA_COLLECTOR, collect the bgp category from the QA leaves in DEMO" | bulk reads, records saved in the project |
| "as STATE_VALIDATOR: are all EVPN peers up on the QA fabric?" | targeted reads, a verdict per device with the evidence |
| "as LOG_REVIEWER, review RAW/leaf.log in DEMO" | root cause with every claim cited to a log line (LOCAL: local model only) |
| "as ANALYZER, compare this config with the vendor's recommendation" | research + comparison |
| "as CONFIG_GENERATOR, write the config to …" | config, rollback and verify commands; no ticket |
| "QC that" | SANITY_AGENT: the checklist and a recorded verdict (`qc`) |
| "push it" | CONFIG_PUSHER: a ticket, `!pushok` in `#approvals`, applied and verified |
| "as DOCUMENTOR, write the change record for ticket …" | a document in the project's OUT/ |

## Scrambler (`scrambler`)
| Say | Action |
| :--- | :--- |
| "sanitize RAW in ACME_BGP" | `action=sanitize mode=default`: removes passwords, type-7, keys, SNMP communities, PSKs, hashes, certs |
| "deep-sanitize RAW in ACME_BGP" | `mode=deep`: also public IPs, public ASNs, hostnames, domains, users, descriptions, serials, MACs |
| "sanitize this output: …" | `action=text` (deep needs a project) |
| "restore this answer for ACME_BGP: …" | `action=restore` (LOCAL: written to RESTORED/, not shown in chat) |
- Output lands in `<project>/SANITIZED/<time>-<mode>/` with `_MODEL_NOTE.txt` and `_SANITIZE_REPORT.md`.
- A run where anything real survives is marked `-FAILED`: do not upload it.

## Where things are (navhost)
| What | Where |
| :--- | :--- |
| Inventories + CREDENTIALS.yml | `/data/private/navigator` |
| Inventory versions, audit, rendered files | `~/.atlas/versions`, `~/.atlas/audit.log`, `~/.atlas/rendered/<consumer>/` |
| Tickets, device records (no project) | `~/.rudder` |
| Projects (device records in `RECORDS/DEVICES`, troubleshooting in `RECORDS/TROUBLESHOOT`) | `/data/projects/<PROJECT>` and `ADHOC_WORK/` |
| Lessons, scrambler maps, open troubleshooting sessions | `~/.navigator` (mode 700) |
| Settings | `~/navigator/{bootstrap,rudder}.yml` (LAB_INVENTORIES, LAB_ANY_SHOW), `~/.config/navigator/navigator.yml` |
| SearXNG | `~/searxng` (Docker, 127.0.0.1:8888); query logs in `<project>/RECORDS/PUBLIC_QUERIES.md` |

## Known gaps
- Catalyst SD-WAN cEdges: CLI changes are not supported. (pyATS reads were fixed on 2026-11-13, decision #121:
  one ~3 s login per read, parsed JSON.)
- vManage / validator / controller: no `show version` probe, so `credential action=test` skips them; use `device action=read`.
- `ask_public` (a public model's answer, scrubbed) is not built yet; `research` returns search results and page text.
- Some vendor sites show a bot check to the page fetch; the search results still come back.
