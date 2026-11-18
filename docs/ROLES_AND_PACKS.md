# Roles and vendor packs

NAVIGATOR is organised two ways:
- **by job**: the lead engineer hands work to specialist **roles** (sub-agents), each with one job and a QC gate;
- **by vendor**: everything a vendor needs, from the inventory fields to the connection methods to the know-how,
  is one **vendor pack** that spans the three layers.

```
                         NAVIGATOR (the lead engineer, front door)
   roles:  DATA_COLLECTOR · STATE_VALIDATOR · LOG_REVIEWER · ANALYZER · CONFIG_GENERATOR · CONFIG_PUSHER
           DOCUMENTOR · SHIP_ENGINEER · SANITY_AGENT (QC)          skill: SCRAMBLER
   ─────────────────────────────────────────────────────────────────────────────────────────────
   vendor packs:   ARISTA │ CISCO │ PALO ALTO │ FORTINET │ JUNIPER │ … │ GENERIC (netmiko)
     ATLAS  : platform, aliases, connection types, fields, command list
     RUDDER : built-in read / write methods, delegated MCP servers
     NAVIGATOR: verify commands, validators, lessons, vendor notes for the roles
```

## 1. Roles (NAVIGATOR's sub-agents)

The lead (NAVIGATOR) talks to the user, picks the roles, and shows nothing until the SANITY_AGENT has passed it.
Each role is a brief the host's model follows (`role action=brief`); on Hermes they can also run as sub-agents.
In LOCAL projects the roles that read project data run on a local model only.

| Role | Job | Uses | Never |
|---|---|---|---|
| **DATA_COLLECTOR** | Bulk read-only collection by category (system, interfaces, bgp…) across many devices; raw output stored as records | `device action=read` | changes anything |
| **STATE_VALIDATOR** | Targeted show commands, read against the expected state and vendor docs: "are all EVPN peers up?" | `device`, `research` | changes anything |
| **LOG_REVIEWER** (the log analyzer) | Multi-vendor log analysis and root cause, every claim cited to a log line. Sensitive: LOCAL logs never leave the host | project files, `scrambler` | sends LOCAL logs to a cloud model |
| **ANALYZER** | Understands configs and outputs, researches vendor docs, compares and validates solutions | `research`, records | changes anything |
| **CONFIG_GENERATOR** | Vendor / OS / version-correct config with rollback and verification steps | catalog, lessons | pushes |
| **CONFIG_PUSHER** | Delivers an approved change: ticket with dry-run diff → the user's `!pushok` out of band → apply, verify, roll back on failure | `change` | approves, or changes a device any other way |
| **DOCUMENTOR** | HLD, LLD, RCA, change records, diagrams from verified inputs | project files | reveals LOCAL data |
| **SHIP_ENGINEER** | Keeps ATLAS and RUDDER working: inventories through the interview, rendering, credential tests, connection troubleshooting (`via=`, `compare`, `debug`) | `inventory_edit`, `adapter`, `device`, `troubleshoot` | sees a secret; writes except through tickets |
| **SANITY_AGENT** (QC) | Reviews every role's result before the user sees it; can order a redo; records lessons | `qc`, `lesson` | is skipped |
| skill **SCRAMBLER** | Sanitize / restore: secrets out, identifying data replaced, map kept privately | `scrambler` | – |

Typical chains: *"why is BGP down on QALEAF1A?"* → STATE_VALIDATOR → LOG_REVIEWER → ANALYZER → SANITY_AGENT.
*"Add a loopback"* → CONFIG_GENERATOR → SANITY_AGENT → CONFIG_PUSHER (ticket, `!pushok`).

## 2. Vendor packs today

What each vendor gets in each layer now. **Built-in** = RUDDER's own code; **delegated** = an existing
open-source MCP server that RUDDER starts and calls (writes only after `!pushok`); **generic** = netmiko with
the allowlist, raw text, no writes.

| Pack | Platforms | ATLAS (connection types) | RUDDER reads | RUDDER writes | Commands on the list | Validators / extras |
|---|---|---|---|---|---|---|
| **ARISTA** | EOS | `EAPI`, `SSH` | built-in **eAPI** (JSON); built-in **SSH** with `\| json` as fallback | built-in **eAPI** config sessions (commit timer, verify, confirm / abort) | 95 | ANTA, Batfish, SuzieQ; pyATS name `eos` |
| **CISCO** | IOS, IOS-XE, NX-OS, IOS-XR, ASA | `SSH`, + `NETCONF`/`RESTCONF` (XE), `NXAPI` (NX), `NETCONF` (XR) | delegated **pyATS** (Genie JSON); built-in **SSH** (`via=ssh`, text) | built-in **netmiko** (archive / checkpoint, verify, rollback); IOS-XR not yet | 170 (XE), 147 (NX) | Batfish, SuzieQ |
| CISCO SD-WAN | SD-WAN Manager (Viptela) | `VMANAGE_API`, `SSH` | built-in **vManage API** (JSON); **SSH** to the Manager's CLI (text) | none | 20 | – |
| CISCO FTD | FTD | `API` | delegated FMC | delegated FMC | – | – |
| **PALO ALTO** | PAN-OS | `SSH`, `XML_API` | built-in **SSH** (text); built-in **XML API** (op commands, JSON) | built-in **XML API** (candidate config, commit job, verify, revert) | 30 | – |
| **FORTINET** | FortiOS | `API`, `SSH` | delegated **fortinet** | delegated fortinet | 5 | – |
| JUNIPER | Junos | `NETCONF`, `SSH` | delegated junos | delegated junos | – | pyATS name `junos` |
| ARUBA · F5 · CHECKPOINT | AOS-CX · BIG-IP · Gaia | `API`, `SSH` | delegated | delegated | – | – |
| MULTIVENDOR | FRR, Cumulus, SR Linux, SR OS, VyOS, RouterOS, SONiC, OS10, EXOS, VRP, EdgeOS | `SSH` (+ `GNMI`) | delegated **multivendor** | delegated multivendor | – | – |
| **GENERIC** | ~200 other netmiko platforms | `SSH` | generic netmiko (allowlist, raw text) | none | – | – |

Delegated servers **installed today**: pyATS, multivendor, fortinet. The others (FMC, junos, aruba-cx, f5,
checkpoint) are named in the catalogue but not installed yet; until they are, those platforms answer "not
available".

Per call the user can pick the method (`via=eapi|ssh|pyats|netmiko|…`), compare two (`device compare`), or set a
device's default (`ADAPTER` field in ATLAS, through the interview).

**Where each vendor lives now**: every platform is in a vendor pack under `navigator_packs/`:

| Pack | Platforms | Methods | Code |
|---|---|---|---|
| `arista` | EOS | eAPI (reads + changes), SSH-JSON | `adapters.py` |
| `cisco` | IOS, IOS-XE, NX-OS, IOS-XR, ASA, FTD, SD-WAN | pyATS, SSH, netmiko writer, vManage API, vManage SSH | `cli.py`, `vmanage.py` |
| `paloalto` | PAN-OS | SSH, XML API reads, XML API writer | `writer.py`, `xmlapi.py` |
| `fortinet` | FortiOS | fortinet MCP server | delegated |
| `multivendor` | FRR, Cumulus, SR Linux, SR OS, VyOS, RouterOS, SONiC, OS10, EXOS, VRP, EdgeOS | multivendor MCP server | delegated |
| `juniper` · `aruba` · `f5` · `checkpoint` | Junos · AOS-CX · BIG-IP · Gaia | their MCP servers (**not installed yet**: `available: false`) | delegated |
| `generic` | Linux, and every other platform netmiko knows (~200) | netmiko over SSH, text | `rudder_devices/adapters.py` |

`atlas_inventory/catalog/platforms_overlay.yml` keeps only controllers and out-of-band types; the host's
`COMMANDS.yml` is now only for overrides. The delegated servers' launch specs (paths on the host) stay in
`rudder_devices/delegated.py`.

## 3. One folder per vendor pack

Same behaviour, reorganised so a vendor is added, reviewed and tested in one place:

```
navigator_packs/
  arista/
    pack.yml          platforms (EOS), aliases, connection types and ports (eAPI 443, SSH 22),
                      ATLAS fields the interview asks (CONN_TYPE EAPI|SSH, EAPI_PORT, VRF),
                      read / write owners, validators (ANTA)
    commands.yml      allowed reads by category (today's ARISTA_EOS list)
    adapters.py       EosEapi, EosEapiWriter, EosSshJson   (moved from rudder_devices/adapters.py)
    knowledge.md      verify-command templates, config-session notes, common pitfalls (for the roles)
    tests/            the pack's own tests (fake eAPI switch)
  cisco/
    pack.yml          IOS, IOS-XE, NX-OS, IOS-XR, ASA, Viptela; SSH / NETCONF / RESTCONF / NXAPI / vManage;
                      pyATS names and testbed defaults (operating_mode, no init config)
    commands.yml      IOS-XE, NX-OS, Viptela lists
    adapters.py       CiscoCliWriter, Viptela vManage   (from cisco.py, vmanage.py)
    delegated.yml     pyATS server: how to start it, default args per OS
    knowledge.md      archive / checkpoint rollback, pnpa on Controller-Managed cEdge, …
  paloalto/ …   fortinet/ …   juniper/ …   multivendor/ …
  generic/            netmiko fallback for every other platform
core (unchanged): ATLAS store / vault / interview, RUDDER tickets / approvals / scheduler / records,
                  NAVIGATOR front door / projects / roles / QC
```

- A **pack loader** reads every `packs/*/pack.yml` at start, builds the platform catalogue and the command
  lists from them (instead of the two big YAML files), and registers the pack's adapters with RUDDER.
- ATLAS's interview asks the **pack's own fields** (e.g. Arista: eAPI or SSH, eAPI port; Cisco: SSH or
  NETCONF, pyATS OS name).
- Every role brief carries the enabled packs' `knowledge.md` notes for that role: a section headed
  `## Verify commands (STATE_VALIDATOR, CONFIG_PUSHER)` reaches only those roles; a heading without roles reaches
  every role. With `commands.yml` (the commands per category that DATA_COLLECTOR and STATE_VALIDATOR run), adding
  a pack teaches every role at once, without changing the roles (#157).
- A pack can be **turned off** per host (`PACKS: [arista, cisco, paloalto]`), and a new vendor is one new folder,
  reviewed and tested on its own.
- Nothing changes for the user: same 16 tools, same tickets and approvals.

**Status**
| Step | State |
|---|---|
| Loader (`navigator_packs/__init__.py`): packs merged into ATLAS's catalogue and command lists at runtime; `NAVIGATOR_PACKS` turns packs on / off; shared adapter plumbing in `rudder_devices/adapter_base.py` | **done** (2026-11-15) |
| **Arista** (`navigator_packs/arista/`): EOS catalogue entry, 95 commands, eAPI reader / writer and SSH-JSON reader, interview fields, knowledge, template examples | **done**; live on QALEAF1A: eAPI read 0.07 s, SSH read 3.0 s, change dry run (config session) correct |
| **Cisco** (`navigator_packs/cisco/`): IOS, IOS-XE, NX-OS, IOS-XR, ASA, FTD, SD-WAN entries; IOS-XE / NX-OS / SD-WAN commands; netmiko writer (`cli.py`), vManage API (`vmanage.py`), pyATS defaults; **new: the SD-WAN Manager over SSH** | **done** (2026-11-15); live on LAB: EDGE1 pyATS 8.7 s (JSON) and SSH 2.1 s; MAN1 API 3.9 s (JSON) and SSH 3.3 s |
| Cisco, more methods: IOS-XE NETCONF / RESTCONF, NX-OS NX-API, IOS-XR commit-confirmed writes | next in the Cisco pack |
| **Palo Alto** (`navigator_packs/paloalto/`): PAN-OS entry, 30 commands, XML API writer (`writer.py`), **new: reads over the XML API** (`xmlapi.py`, CLI → XML op, replies as JSON; `CONN_TYPE XML_API` or `via=xml-api`) | **done** (2026-11-15); live on PA-EDGE: SSH 25.9 s; 12 commands over the XML API in 8.8 s; dry run correct, nothing committed |
| **Delegated and generic packs**: `fortinet`, `multivendor`, `juniper`, `aruba`, `f5`, `checkpoint`, `generic` (the netmiko fallback and Linux; turning it off hides every generic platform). The overlay keeps only controllers / OOB | **done** (2026-11-15); 10 packs, every platform in a pack |

Rules for every pack: all tests pass, the inventory template has one example record per method
(`template_examples` in pack.yml; a test checks it), and the live lab tests are rerun.

**After changing a pack** restart all three: `systemctl --user restart atlas-link rudder-scheduler hermes-gateway`
(atlas-link answers RUDDER's inventory lookups and has its own copy of the catalogue).

**A host's own list wins**: a platform listed in `atlas_inventory/catalog/COMMANDS.yml` uses that list instead
of its pack's `commands.yml`.
