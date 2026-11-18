# SHIP_ENGINEER manual

SHIP_ENGINEER keeps the charts and the engine room working: the **inventories and credentials** (ATLAS) and
the **device connections** (RUDDER). It is also how you troubleshoot the automation itself: when a read
fails, find out where and why. It is **read-only on devices**: a configuration change is always a ticket you
approve. Its brief for subagents is `navigator_engineer/roles/SHIP_ENGINEER.md` (`role action=brief
role=SHIP_ENGINEER`); this page is the manual for you.

Start it with: *"be the ship engineer: …"* or work with the tools below directly.

## 1. Inventories

| Job | Say | Tool |
|---|---|---|
| What exists | "list inventories", "show EVPN_LAB", "show the SPINE group" | `inventory action=list / show` |
| Platforms | "which platforms do you support?", "what does PANOS support?" | `inventory action=platforms / platform` |
| The template | "give me the inventory template" | one example per vendor and connection method |
| Add / change / remove | "add device QALEAF3A …", "set ADAPTER=ssh on EDGE1" | `inventory_edit` start → answers → **preview** → your yes → commit |
| Bulk | "import these 40 devices" (a CSV / YAML in RAW), "set SITE=NYC on all leaves" | `inventory_edit topic=bulk_import / bulk_edit` |
| History | host: `atlas-interview history EVPN_LAB` | every version is kept |

Rules: every change is a preview you say yes to; production devices carry `PRODUCTION: true`; LOCAL
projects stay on their own inventory.

## 2. Rendering files for other tools

"Render EVPN_LAB for pyATS" (`inventory action=render format=pyats | ansible | anta | multivendor`)
writes a testbed or inventory with **references** (`%ENV{…}`) instead of passwords, into the project or ATLAS's
render folder. Use them to run those tools yourself; NAVIGATOR does not need them.

## 3. Credentials

| Job | How |
|---|---|
| Which tags exist | "list credential tags" (`credential action=list`): names and what uses them, never values |
| A new tag | chat: `inventory_edit topic=credential_new` → host: `atlas-interview secret <draft> LOGIN_PASSWORD` |
| Change a password | host: `atlas-interview secret …` |
| Does it work? | "test the login on QALEAF1A" (`credential action=test`: login + `show version`), optionally `via=ssh` |
| Who may use it | host: `atlas-interview run consumer_access --consumer NAVI` |

Statuses: `ok`, `auth_failed` (wrong password or enable), `unreachable` / `timeout` (path, port, VPN),
`host_key` (a new SSH key: check before trusting it), `tls_error` (eAPI / XML API certificate).

## 4. Connection methods

Each platform has a default method, from its vendor pack ([ROLES_AND_PACKS.md](ROLES_AND_PACKS.md)):

| Vendor pack | Methods (`via=`) |
|---|---|
| Arista | `eapi` (default, JSON), `ssh` (`| json`), `netmiko`, `pyats` |
| Cisco | `pyats` (IOS-XE / NX-OS default, Genie JSON), `ssh` / `netmiko` (text), `vmanage` (SD-WAN Manager API) |
| Palo Alto | `ssh` (text), `xml-api` (JSON) |
| Fortinet, multivendor | delegated MCP servers (`fortinet`, `multivendor`) |
| everything else | the generic netmiko pack |

| Job | Say | Tool |
|---|---|---|
| Try another method once | "show version on EDGE1 via ssh" | `device action=read via=ssh` (nothing stored) |
| Side by side | "compare show version on EDGE1 via ssh and pyats" | `device action=compare via="ssh,pyats"`: both outputs, times, errors, a diff |
| The device's own text | "… raw" | `raw=true` |
| What happened on the wire | "… with debug" | `debug=true`: prompts, timeouts, paging, the delegated server's log; secrets removed |
| Make a method the default | "make ssh the default for EDGE1" | `adapter action=default` → preview → your yes → commit |
| What is set | "which adapters are set in SDWAN_LAB?" | `adapter action=list` |

## 5. Troubleshooting, recorded

1. **Name it**: "start troubleshooting: pyATS reads on EDGE1 time out" (`troubleshoot action=start`).
   From now on every read, compare and login test with this project goes into
   `<project>/RECORDS/TROUBLESHOOT/<time>-<title>.md`: command, method, adapter, timing, status, output,
   debug log.
2. **Reproduce once** with the failing method and `debug=true`; read the status first.
3. **Change one thing at a time**: another method, raw, another device of the same platform, a login test.
   `compare` tells whether the device or the method is the problem.
4. **Note what you see**: "note: SSH works in 2 s, pyATS needs 29 s to log in".
5. **Fix it the proper way**: inventory fields and defaults through the interview, credentials on the host,
   device configuration through a ticket. Then run the same read again to prove it.
6. **Close it**: "stop troubleshooting: cause …, fix …" (`troubleshoot action=stop summary=…`).
   "Show the troubleshooting log" shows it (LOCAL: the file path only).

| Status | Usually |
|---|---|
| `auth_failed` | password, enable secret, or the tag this consumer may use |
| `unreachable`, `timeout` | routing, ACL, VPN, the port (eAPI 443, SSH 22, XML API 443), a slow login |
| `command_failed`, "device rejected the command" | syntax for this platform or version; the command list |
| `skipped` | no adapter installed, not on the command list, no credential tag for this consumer |
| a change refused at propose | the device rejected a line in the dry run (e.g. EOS: no OSPF network type on a Loopback) |

## 6. Vendor packs

Packs live in `navigator_packs/<vendor>/` (platforms, command lists, interview fields, template examples,
adapters, knowledge). The host turns packs on or off with `NAVIGATOR_PACKS`; `COMMANDS.yml` overrides a
pack's command list. After a pack change restart `atlas-link`, `rudder-scheduler` and the MCP host.
"Which packs are on?" / "what does the Palo Alto pack give?" answer from the catalogue.

## 7. Rules

- Read-only on devices; changes are tickets you approve. SHIP engineer IDs (`APPROVAL.SHIP_ENGINEERS`) skip
  the `/pushok` only in lab inventories, never scheduled, never production, and are still audited.
- Commands only from the platform's list; never try to get around it.
- Never print, ask for or store a password, key or token. If one shows up in an output, stop and say so.
- Pass `project=` so the records land in the job.
