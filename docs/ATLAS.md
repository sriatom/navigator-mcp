# ATLAS: inventory and credentials (layer 1)

ATLAS is the only layer that reads inventory files and CREDENTIALS.yml. Agents see inventories,
platforms, read-command lists and credential **tags**. No tool ever returns a credential value.
RUDDER (layer 2) gets the values it needs over the **ATLAS link**, a local socket, and never reads ATLAS's files.

```
 Hermes / OpenClaw agent ──stdio──▶ atlas-mcp     (tools: list, show, query, interview…; no secrets)
 Hermes / OpenClaw agent ──stdio──▶ rudder-mcp ──▶ ~/.atlas/link.sock ──▶ atlas-link  (credential values)
```

## Parts

| Program | Runs as | Does |
| :--- | :--- | :--- |
| `atlas-mcp` | started by the host for each session (stdio) | the ATLAS MCP tools (README) |
| `atlas-link serve` | **systemd user service `atlas-link`** | the socket RUDDER asks for records and credentials |
| `atlas-interview` | a shell on the host | guided changes, and entering secrets with getpass |
| `atlas-vault` | a shell on the host | credential encryption: `status`, `init`, `encrypt`, `check`, `rekey` |

## Files (navhost)

| What | Where | Mode |
| :--- | :--- | :--- |
| Settings | `~/navigator/bootstrap.yml` (or `ATLAS_BOOTSTRAP`) | paths and names only |
| Inventories, CREDENTIALS.yml | `DATA_PATH` = `/data/private/navigator` | private |
| Versions, audit log, drafts | `STATE_PATH` = `~/.atlas` (`versions/`, `audit.log`) | 700 |
| Link socket | `~/.atlas/link.sock` | 600 |
| Link token for RUDDER | `~/.config/rudder/atlas-link.token` | 600 |
| Vault key | `~/.config/atlas/vault.key` | 600 |
| Service unit | `~/.config/systemd/user/atlas-link.service` (copy of scripts/atlas-link.service) | |
| Platform catalogue | `atlas_inventory/catalog/` (PLATFORMS.yml, COMMANDS.yml) | not secret |
| Templates | `atlas_inventory/templates/` (`template_get` tool) | not secret |

Inventory file: `INVENTORY_<NAME>.yml` with the lists `INVENTORY:` (devices), `CONTROLLERS:` and `OOB:`.
Credential file: `DATA:` entries with `TAG`, `TYPE` (LOGIN, API_TOKEN, SNMP, CERT, SMTP) and the fields for that type.
Ask the bot for `template_get("inventory")` or `template_get("credentials")` for a filled example.

## Changing inventories and credentials

Every change goes through the interview: start, answers, preview, your yes, commit. Each commit keeps the
previous version in `~/.atlas/versions/<file>/` and adds a line to `~/.atlas/audit.log`.

- In chat: attach a `.yml` or `.csv` and say *"create inventory LAB2 from this file"*, or *"add LEAF9 … to LAB2"*.
- On the host: `atlas-interview run inventory_new | record_add | bulk_import | credential_new | consumer_access`.
- Secrets never go into chat: `atlas-interview secret <draft> LOGIN_PASSWORD` on the host.
- Go back: *"history of LAB2"*, then *"restore version …"* (`inventory_restore`, saved as a new version).
- From NetBox or Nautobot: `atlas-interview run source_import --inventory DC` (or *"import DC from NetBox"*) asks
  the URL, the API token's credential tag (TYPE API_TOKEN), an optional filter (site=, tag=, role=...),
  platform names the catalogue does not know, and which credential tag each device uses
  (`CRED_MAP: ROLE:LEAF=FAB, SITE:HQ=LAB_EOS, TAG:lab=LAB_EOS, *=DEFAULT`, first match wins). Later:
  `atlas-interview run inventory_sync --inventory DC`. The preview lists what is added, changed and disabled
  before anything is written. The source owns name, address, platform, vendor, site, role and enabled; the
  credential, connection, port, adapter, FAB and PRODUCTION stay yours. A device gone from the source is
  disabled, never deleted; records you added by hand are left alone.

## The ATLAS link service

RUDDER cannot read or change any device unless the link is running. The service starts at boot
(linger is on for `<user>`, so nobody needs to log in) and restarts itself after a crash.

### Everyday commands

```bash
systemctl --user status atlas-link          # running? last log lines
systemctl --user restart atlas-link         # restart
journalctl --user -u atlas-link -f          # follow the log (one line per credential served: tag and consumer, never values)
~/navigator/.venv/bin/atlas-link status     # socket present? token present per client?
```

### When to restart

The link re-reads `bootstrap.yml`, the inventories, the credentials and the token files on **every request**.
Edits to inventories, credentials, `CONSUMERS` or `LINK.CLIENTS` take effect at once, with no restart.

Restart it after:
- updating the code (`git pull` or `pip install` in `~/navigator`);
- changing `LINK.SOCKET`, or pointing `ATLAS_BOOTSTRAP` somewhere else;
- `atlas-vault rekey` (not strictly needed; it makes sure nothing holds the old key).

`atlas-mcp`, `rudder-mcp` and `navigator-mcp` are not services: the host starts them. After a code update,
send `/reload-mcp` in the chat (or start a new session).

### Install (new host)

```bash
cd ~/navigator
.venv/bin/atlas-link token RUDDER                        # once; never overwrites an existing token
mkdir -p ~/.config/systemd/user
cp scripts/atlas-link.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now atlas-link
sudo loginctl enable-linger "$USER"                      # start at boot without a login
```

To remove it: `systemctl --user disable --now atlas-link`, then delete the unit file.

### Troubleshooting

| RUDDER says | Cause | Fix |
| :--- | :--- | :--- |
| `ATLAS link is not running` | service stopped, or socket missing | `systemctl --user restart atlas-link`, then check `journalctl --user -u atlas-link -n 30` |
| `… is in use by another atlas-link` (in the log) | a hand-started `atlas-link serve` holds the socket | `pkill -f 'atlas-link serve'`, then restart the service |
| `unknown token` | RUDDER's token file differs from the one in `LINK.CLIENTS` | check `atlas-link status`; both must use `~/.config/rudder/atlas-link.token` |
| `client RUDDER may not act for consumer X` | X is not in RUDDER's `CONSUMERS` in `bootstrap.yml` | add it there (takes effect at once) |
| `inventory … is not available to consumer X` | the `CONSUMERS` section limits X | `atlas-interview run consumer_access --consumer X` |
| `timeout` / `unreachable` on every device of one lab, with credentials served in the log | the lab network is down: ATLAS is fine | check the route to the lab (for NETSIM labs, the WireGuard VPN via 192.0.2.130): `ping`, `ip route get <addr>` |

A quick end-to-end check: *"test the login on CORE in SDWAN_LAB"* in chat. The log shows
`credential served tag=… consumer=NAVI`, and the reply says `ok`.
