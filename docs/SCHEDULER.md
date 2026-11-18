# Scheduled changes (RUDDER)

"Apply this at 2 AM" without anyone awake at 2 AM. RUDDER does it on its own: no host cron and no
model is woken at 2 AM. The same code works under any MCP host.

## How it works

```
 you (chat)            RUDDER (rudder-mcp)                 rudder-scheduler (service)          you (Telegram, navi bot)
 "add Loopback99 on    change_propose(at="02:00")  ──────▶  ticket: scheduled 02:00 EST  ──────▶  🔐 approval request, with the time
  QALEAF1A at 2 AM"    dry-run diff, device locked                                               /pushok CODE  (any time before 01:59:30)
                                                            01:59:30  ──────────────────────▶  ⏰ "starts in 30 s"
                                                                                                 /pushno CODE2 = stop
                                                                                                 /pushok CODE2 = start now
                                                                                                 no answer     = it runs
                                                            02:00:00  apply: drift check, commit (EOS: rollback timer), verify,
                                                                      confirm or roll back  ─▶  ✅ / ❌ result
```

1. **Propose with a time.** The agent calls `change_propose(..., at="02:00")`. RUDDER checks the lines, makes the
   dry-run diff on the device and locks the device until the window, so nothing else changes it in the meantime.
2. **Approve in advance.** The approval request on the **navi** bot shows the diff and the time. `/pushok`
   approves *exactly this change at exactly this time*. (The bot replies "the push will run now"; it runs at
   the scheduled time.)
3. **The notice.** 30 seconds before the start (`NOTICE_SEC`), you get a notice with a new code:
   - `/pushno CODE` stops it. Nothing is changed and the lock is released.
   - `/pushok CODE` starts it at once.
   - No answer: it runs. You already approved it, so the default is yes.
4. **Apply.** Same path as a manual `change_apply`: the device must still give the same diff (no drift), then
   commit, verify, confirm, or roll back. You get the result on the navi bot.

## Safety rules

- The agent **cannot** apply a scheduled ticket early. `change_apply` refuses it; only the scheduler runs it.
- The time is part of the approved hash. Moving it (`change_schedule`) always needs a **new** `/pushok`, and
  a ticket file edited by hand is refused.
- If it is **not approved** by the notice time, it is not applied ("missed"), and you are told.
- If the scheduler was **down** and is more than `LATE_MIN` (10 min) late, it is not applied ("missed").
- If the notice cannot be sent, the scheduler keeps retrying. Without a notice nothing runs, and after
  `LATE_MIN` the change counts as missed.
- PRODUCTION refusal, the deny list, one kind of device per ticket: all the same as for manual changes.

## In chat

| Say | Tool |
| :--- | :--- |
| "on QALEAF1A add `interface Loopback99` / `description test`, apply at 2 AM" | `change_propose(at="02:00")` |
| "schedule ticket C20261112-ab12cd for 2026-11-14 01:30" | `change_schedule` (new `/pushok` needed) |
| "what is scheduled?" | `change_list` (shows `at` and the schedule state) |
| "status of C20261112-ab12cd" | `change_status` |
| "cancel C20261112-ab12cd" | `change_cancel` (any time before it starts) |

Times: `"02:00"` (the next 02:00), `"2026-11-13 02:00"`, `"2026-11-13 02:00 EST"`, or ISO with an offset.
A time with no zone uses `SCHEDULE.TIMEZONE` (America/New_York). `EST` / `ET` mean New York wall time,
so `EST` in July means EDT. At most `MAX_DAYS` (7) ahead.

## Settings (`rudder.yml`)

```yaml
SCHEDULE:
  TIMEZONE: America/New_York
  NOTICE_SEC: 30        # per ticket: notice_sec=10..600
  LATE_MIN: 10
  MAX_DAYS: 7
  POLL_SEC: 5
```

## The service

```bash
systemctl --user status rudder-scheduler        # running?
journalctl --user -u rudder-scheduler -f        # what it did (notice sent, applied, missed, stopped)
systemctl --user restart rudder-scheduler       # after a code update or a rudder.yml change
~/navigator/.venv/bin/rudder-scheduler list     # scheduled changes
~/navigator/.venv/bin/rudder-scheduler once     # one pass by hand (testing)
```

Install on a new host:

```bash
cp ~/navigator/scripts/rudder-scheduler.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rudder-scheduler
```

It needs `atlas-link` (for credentials; the unit starts after it) and the Hermes binary for the
messages. One scheduler serves one consumer (`CONSUMER` in rudder.yml, or env `RUDDER_CONSUMER`). For a
second consumer, run a second unit with `Environment=RUDDER_CONSUMER=LAB_BOT`.
Everything it does is in `~/.rudder/audit.log` and in the ticket (`change_status`).

A restart is safe at any time: the state is in the ticket files. If it stops in the middle of applying a
change, the EOS commit timer rolls the switch back by itself. Cisco and PAN-OS keep their rollback point and
manual rollback command in the ticket.

## Why not the agent host's cron?

Those wake the AI model at the set time with a prompt, so what happens at 2 AM depends on the model and on
the host being up. The approval would also need a different setup on each host. RUDDER's scheduler is plain
code with the same checks as a manual change, and it runs the same on every host.

## Results in the originating chat

A ticket may carry `reply_to` (`<profile>/<platform>:<chat>[:<thread>]`), filled by the MCP host, never by the
model. Every result the scheduler sends to the approval channel (applied, failed, stopped, missed) is then also
posted there with `hermes -p <profile> send`. Only profiles in `APPROVAL.REPLY_PROFILES` may be used (default
none = off). Put host-specific values like that in `~/.config/rudder/rudder.local.yml`, which is merged over
`rudder.yml` (or point `RUDDER_LOCAL` at another file). Approving still happens only on the approval channel.
On Hermes, a small `pre_tool_call` plugin can fill `reply_to` from the session's origin.

## Change pages

With `APPROVAL.WEB_BIND` set (e.g. `192.0.2.240:8790`) the scheduler also serves the read-only change pages on that address, TCP **8790**, directly (no proxy). Open that port in the firewall for the approvers' networks only. See [APPROVALS.md](APPROVALS.md).
