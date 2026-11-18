# RUDDER: the change engine

RUDDER is layer 2 of NAVIGATOR. It is the only part that talks to devices. Reads go straight through.
A configuration change goes through one fixed path, written in code, that the chat agent cannot skip:

```
propose ──▶ /pushok by a person, outside the chat ──▶ apply ──▶ verify ──▶ confirm
                                                                   └──▶ or roll back
```

The agent can ask for a change. It cannot approve one, apply one early or change the rules. There is no
approve tool anywhere.

## The path of one change

### 1. Propose

The agent calls `change action=propose` with the devices and the exact lines. Before anyone is asked,
RUDDER does all of this:

| Step | What RUDDER does |
|---|---|
| Deny list | Every line is checked. Lines that could lock you out or wipe the box are never pushed: `reload`, `erase`, `delete`, `format`, `boot`, `username`, `aaa`, `enable secret`, SSH and management API settings, `write` / `copy`, `bash`, `run`, `do`, `checkpoint`, `rollback`, `line vty`, and more. A host can add its own (`DENY_LINES`). |
| PRODUCTION refused | A device marked PRODUCTION in the inventory is refused (`ALLOW_PRODUCTION` is off). |
| Per-device lock | Each device is locked to this ticket. A second change to the same device is refused until the first one ends. |
| Baseline | The running config of each device is saved as a record before anything else, with its hash. |
| Dry-run diff | The diff is made **on the device itself**: a config session on Arista, the candidate config on PAN-OS, a diff against the running config on Cisco. Nothing is committed. |
| Validation (when on) | RUDDER checks the change against a model of the network, such as Batfish ([below](#change-validation)). A fail stops the change here. |
| One hash | Devices, lines, baselines, diffs, the validation result and the schedule time (if any) are bound into one hash. The approval is for that hash only. |
| Approval request | A one-time code goes to the approval channel only. RUDDER keeps only hashes of the code. |

### 2. Approve, out of band

A person answers `/pushok CODE` (or `!pushok CODE` on Slack) in the approval channel. That channel belongs to
a **separate approval bot**, never the chat bot, so the bot that asks can never approve. The approval
plugin checks the method, the channel, the approver and (with `SELF_APPROVE: false`) that the approver is not
the requester. `/pushno CODE` rejects. Details: [Approvals](APPROVALS.md).

A ticket waits `TTL_MIN` minutes (default 30). Then it expires: nothing changes and the locks are released.

### 3. Apply

`rudder-scheduler` sees the approval and applies within seconds, through the same code as `change action=apply`.

1. **Drift check.** RUDDER makes the dry-run diff again. If any device now gives a different diff, someone
   changed it since the approval. The ticket stops as `drifted` and nothing is applied.
2. **Commit with a rollback point**, per device:

   | Platform | Rollback point |
   |---|---|
   | Arista EOS | `commit timer` (default 5 min): the switch rolls back by itself unless RUDDER confirms in time |
   | Cisco IOS / IOS-XE / NX-OS | a checkpoint file or NX-OS checkpoint, taken just before the lines |
   | PAN-OS | the running config saved on the firewall, then an API commit |

3. **Verify.** The verify commands given with the change must show what was expected. On Arista, the same
   lines must also change nothing any more. With validation on, the live config is also validated again, still
   inside the rollback window.
4. **Confirm or roll back.** A pass confirms and removes the rollback point. A fail rolls back. On Arista, a
   RUDDER crash between commit and confirm is rolled back by the switch's timer. On Cisco and PAN-OS the
   ticket keeps the exact manual rollback command, and the device is marked for a person to check.

RUDDER stops at the first device that fails. Devices not yet touched stay untouched.

The result goes to the approval channel and back to the chat thread the change came from.

## Change validation

An optional check of every change against a model of the network, run by RUDDER itself. The chat agent never
carries the result, so it cannot skip the check or fake a pass. It is off by default; turn it on with
`navigator-setup` ([The setup interview](INTERVIEW.md#4d-change-validation-optional)).

| Mode | What checks the change |
|---|---|
| `none` | nothing (default) |
| `batfish` | a Batfish service on a local or private address, for example `docker run -d -p 127.0.0.1:9996:9996 batfish/allinone` |
| `netclaw` | the Batfish service a NetClaw install already runs (found from its `BATFISH_HOST` / `BATFISH_PORT`) |

**Before the approval.** RUDDER builds two snapshots: the saved running configs (before) and the same with each
changed device's candidate config (after). With Batfish:

| Result | When |
|---|---|
| **fail** | a config Batfish cannot parse, a new undefined reference (for example a route-map that does not exist), or a BGP session that was compatible and no longer is |
| **warn** | a new line Batfish does not recognize, or a BGP session that is gone |
| info | how many routes change |

A **fail blocks the change**, like a line the device refuses: no approval is opened, the locks are released,
the approval channel and the chat thread are told, and the change page shows FAILED validation. A pass or a
warn goes into the approval message and the ticket page, and is part of the approved hash.

**After the commit.** Inside the rollback window, RUDDER validates the live config again. A fail rolls the
device back. `VALIDATION.POST_PUSH false` turns this second check off.

**The candidate config per platform**

| Platform | Candidate config |
|---|---|
| Arista EOS | a config session with the lines, read and then aborted |
| Cisco IOS / IOS-XE / NX-OS | best effort: the running config plus the lines (`no` lines left out) |
| PAN-OS, SD-WAN Manager | `skipped` for now |

**If the validator is down:** `VALIDATION.ON_UNREACHABLE block` (default) stops the change; `warn` lets it go
on with a warning. The configs go only to the validator on your own network, never through the chat model, so
this works for LOCAL projects too.

Install: `pip install "navigator[batfish]"` for the Batfish client.

## When the device refuses a line

If the device rejects a line in the dry run ("invalid command"), the change has **failed**:

- no approval is opened and nothing is changed;
- the ticket is kept as `failed` with `attention`; its page shows FAILED and the refused line;
- an alert goes to the approval channel and to the chat thread.

The agent must then find out why (platform, interface type, version), find lines the device accepts and
record it with `change action=investigate` (the finding, the alternative and the new ticket). The
alternative is a **new** change with its own approval.

RUDDER remembers a refused line for an hour. If a new proposal for the same devices leaves that line out,
the ticket lists it under `left_out`, and the approval message says so. So the agent cannot quietly drop part
of what you asked for.

## Scheduled changes

"Apply at 02:00" is approved in advance. The time is part of the approved hash, so moving it needs a new
`/pushok`. Only the scheduler runs it; the agent cannot apply it early. Thirty seconds before the start the
approval channel gets a notice with a code to stop it (`/pushno`); no answer means it runs. If it was not
approved in time, or the scheduler was down for more than 10 minutes, it is not applied ("missed").
Details: [Scheduled changes](SCHEDULER.md).

## Tickets

- **RUDDER ticket IDs** look like `C20261114-ab12cd`. Every change is one, even one that needs no approval.
- **An outside ticket ID** (a change request number such as `CHG0012345`) can be set on a project with
  `ticket_id`. Every change in that project carries it and can be found by it.
- **Finding tickets:** `change action=list` filters by date, device, inventory, requester, platform, state,
  outside ticket ID and project. In chat: "failed changes this week", "changes for CHG0012345".
- **Change pages:** with pages on, each ticket has a read-only web page (TCP 8790, HTTPS with the local CA).
  It shows who asked, each device's full diff, the lines, the verify commands and the progress. It cannot
  approve or change anything, the code is never on it, and secret-looking values in the diff are masked.
  Each link is random per ticket.
- **Retention:** a finished ticket, its page and its baseline records are kept `PAGES_KEEP_DAYS` (default 90),
  then a daily clean-up (`CLEANUP_AT`, default 02:00) removes them. Open and scheduled tickets are never
  removed.

## Audit log

Every step of every ticket is one line in the audit log: proposed (with its hash and requester), approved or
rejected and by whom, applied, verified, rolled back, failed, investigated, expired, cancelled, removed by
retention. The audit log is never trimmed.

## Credentials: the agent never sees a password

RUDDER has no passwords of its own. It asks layer 1, ATLAS, for a credential through the ATLAS link, a local
socket with a token per client. Credentials are stored encrypted, referred to by tag only, and are never
returned to the chat. Secrets are typed on the host's shell, never in chat. See [ATLAS](ATLAS.md).

## LOCAL projects stay private

A change can belong to a project. In a LOCAL project, device output and configs do not go back to a cloud
model: the chat gets record names, and a local model reads the files. The configs and diffs pass through
RUDDER, not through the chat model. The approval request does carry the diff to your approval channel, so
keep that channel private; with change pages on, a long diff stays on your network and the message has only
a summary and the link. See [Cloud and local models](LOCAL_MODELS.md).

## Who may skip /pushok

| Setting | Effect |
|---|---|
| `APPROVAL.SELF_APPROVE` | `true` (default): the person who asked may approve their own change, fine for a lab or a one-person team. `false`: another approver must approve; the requester can still reject. |
| `APPROVAL.SHIP_ENGINEERS` | user IDs of people whose changes run without `/pushok`, **only** in lab inventories, never scheduled, never PRODUCTION. The requester comes from the host, never from the model. The change is still a ticket: audited, announced on the approval channel as "runs WITHOUT approval", reported and searchable. |

Both are set only on the host's shell with `navigator-setup`, and a change to how approving works is saved
only after a test code sent the new way was approved there.

## Planned, not built yet

These are agreed and on the plan. None of them works today.

- **Reachability checks stated with a change.** For example "10.1.1.0/24 must still reach 10.2.2.5:443",
  checked by the validator before the approval.
- **Validation for PAN-OS and SD-WAN Manager** (they are `skipped` today).
- **Maybe later: your own check script** as a validation mode, given the configs before and after the change.
- **ServiceNow.** Optional, in two levels. *Link only*: read the change request and write back the diff,
  the validation result, the apply result and the close code. *Gate*: the push also needs the change request
  in Implement and inside its planned window, **plus** `/pushok`. The planned window becomes the scheduled time.
- **NetBox / Nautobot.** An inventory import in ATLAS, one-time or on demand, with a preview of what would be
  added, changed or removed. RUDDER keeps reading the inventory from ATLAS.
- **Self-approval in chat.** Setup prints a note instead of asking. An approver can turn self-approval off with
  `/selfapprove off` in the approval channel. Turning it back on stays shell-only.
