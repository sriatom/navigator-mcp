# Day 2: keep it running

Daily use is in [Daily use](HOW_TO_USE.md) and the [cheatsheet](CHEATSHEET.md). This page is for the person
who runs the host.

## Health check

```bash
systemctl --user is-active atlas-link rudder-scheduler     # both: active
navigator-setup show                                       # settings as expected
navigator-setup test-approval                              # the approval path still works
journalctl --user -u rudder-scheduler --since today        # what the scheduler did today
```

In the chat: *"failed changes this week"*, *"what is scheduled?"*, *"local model status"*.

## Upgrade

=== "pip"

    ```bash
    ~/navigator/.venv/bin/pip install --upgrade navigator-mcp
    ```

    Then get the service files and Hermes plugins of the new version:

    ```bash
    V=$(~/navigator/.venv/bin/pip show navigator-mcp | sed -n 's/^Version: //p')
    curl -fsSL "https://github.com/sriatom/navigator-mcp/archive/refs/tags/v$V.tar.gz" \
      | tar -xz -C ~/navigator --strip-components=1 --wildcards '*/scripts/*.service' '*/integrations/*'
    ```

=== "From source (git)"

    ```bash
    cd ~/navigator
    git pull
    .venv/bin/pip install --no-build-isolation -e ".[test]"
    .venv/bin/python -m pytest -q
    ```

```bash
systemctl --user restart atlas-link rudder-scheduler
```

If you copied a Hermes plugin into a profile, copy it again from `~/navigator/integrations/hermes/`.

Then restart the agent host so it loads the new tools (Hermes: `systemctl --user restart hermes-gateway`;
Claude Code and Cursor: reconnect the MCP server).

## Change a setting

| Change | How |
|---|---|
| any host setting | `navigator-setup set KEY VALUE` (`add` / `remove` for lists) |
| approvals | `navigator-setup set APPROVAL...`, saved only after a test code is approved |
| a password | `atlas-interview secret …` on the host, then *"test the login on …"* in the chat |
| who may use an inventory | `atlas-interview run consumer_access --consumer <NAME>` |
| HTTPS users | `navigator-setup user add / reset / role / remove / list` |

After a change: `systemctl --user restart rudder-scheduler`, then restart the agent host.
Full list: [The setup interview](INTERVIEW.md#5-changing-it-later).

## Records and retention

- Finished tickets, their change pages and baselines are kept 90 days (`APPROVAL.PAGES_KEEP_DAYS`), then a
  daily clean-up removes them. Open and scheduled tickets are never removed.
- The audit log is never trimmed: every step of every ticket.
- Back up regularly: `navigator-setup backup` ([Backup and restore](BACKUP.md)).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/mcp` shows **failed** or **connecting** | over SSH: `ssh -o BatchMode=yes navhost echo ok` must print `ok` without a prompt |
| connected but **0 tools** | something printed to stdout before the protocol: keep `-T` and `2>/dev/null`, no `echo` in `.bashrc` for non-interactive shells |
| "no inventory" or "not found" | `NAVIGATOR_BOOTSTRAP` points to the wrong file, or the agent has no access (`consumer_access`) |
| a change waits forever | approve it in the approval channel; the agent never gets the code |
| `!pushok` in Slack does nothing | the approval bot is not in the channel, or the channel is not set as a no-mention channel ([Approvals](APPROVALS.md)) |
| a device refused a line | the ticket is `failed` with `attention`; the agent investigates and proposes an alternative as a new change ([RUDDER](RUDDER.md)) |
| the automation itself misbehaves | *"be the ship engineer: reads on LEAF1 time out"* ([SHIP_ENGINEER](SHIP_ENGINEER.md)) |
