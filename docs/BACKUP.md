# The recovery key, backup and restore

## The keys, in short

| Key | Made by | Where | Who needs it |
|---|---|---|---|
| **CREDENTIALS key** (encrypts every password in `CREDENTIALS.yml`, AES-256-GCM) | NAVIGATOR: 32 random bytes | `~/.config/atlas/vault.key` (mode 600) | `atlas-link`, unattended |
| **Recovery key** (opens the CREDENTIALS key and every backup) | NAVIGATOR: 48 digits in 8 groups of 6, BitLocker style | **only with you**: shown once, NAVIGATOR keeps no copy | you, for backups, restores and a new host |
| HTTP passwords ([HTTPS.md](HTTPS.md)) | NAVIGATOR: 32 random characters | scrypt hashes in `users.yml` | MCP clients |

The recovery key looks like `123456-234567-345678-456789-567890-678901-789012-890123`. Each group is a
multiple of 11, so a mistyped group is caught at once. It holds 128 random bits.

How it works: `recovery init` makes a key pair. The public half is stored as it is. The private half is
stored encrypted with the recovery key (scrypt + AES-256-GCM) in `~/.config/atlas/recovery.json`
(mode 600). The CREDENTIALS key is sealed to the public half (the **escrow**), and so is every backup.
Making a backup or a new escrow needs no secret; **opening** either needs the recovery key. `rekey` (a new
CREDENTIALS key) re-seals the escrow by itself.

## Set it up

```bash
navigator-setup recovery init        # shows the recovery key ONCE, then asks you to type it back
navigator-setup recovery check       # any time: is this the right recovery key?
```

The setup interview (`navigator-setup`) does the same at the end, and offers a first backup. If there is no
CREDENTIALS key yet, `recovery init` makes that too. `--replace` makes a new recovery key. Backups made with
the old one still need the old one.

Keep the recovery key apart from the backups: in your password manager, or on paper.

## Back up

```bash
navigator-setup backup                       # asks for the recovery key and checks it, then writes
navigator-setup backup --out /mnt/usb        # another folder (default ~/navigator-backups)
navigator-setup backup --no-records          # without RUDDER's command records (smaller)
```

The backup command asks you to **type the recovery key first** and refuses a wrong one. A backup is never
made that you could not open. The result is **one encrypted file**,
`NAVIGATOR-BACKUP-<host>-<time>.nvbak` (mode 600). It is safe to copy to OneDrive or a USB disk.

| In the backup | From |
|---|---|
| NAVIGATOR settings: `navigator.yml`, HTTP users, the TLS CA and certificate | `~/.config/navigator` |
| NAVIGATOR state: lessons, scrambler maps, settings and access logs | `~/.navigator` |
| RUDDER settings: `rudder.local.yml` (approvals), `atlas-link.token`, `approve.yml` | `~/.config/rudder` |
| RUDDER state: tickets, audit log, command records | `~/.rudder` (not `delegated/`, which is installed software, nor `locks/`) |
| The CREDENTIALS key and `recovery.json` | `~/.config/atlas` |
| Inventories and `CREDENTIALS.yml` (values stay encrypted inside the backup too) | ATLAS `DATA_PATH` |
| ATLAS state: versions, consumers | `~/.atlas` |

**Not in it:**
- project folders (`PROJECT_ROOT`): back them up with your usual file backups;
- the code: reinstall with pip / uv;
- the hosts' own settings (Hermes profiles and so on).

## Restore

```bash
navigator-setup restore NAVIGATOR-BACKUP-navhost-20261116-101500.nvbak --list           # check it, write nothing
navigator-setup restore NAVIGATOR-BACKUP-navhost-20261116-101500.nvbak --to ~/restored  # everything under one folder
navigator-setup restore NAVIGATOR-BACKUP-navhost-20261116-101500.nvbak                  # back to the original places
```

Restore needs **only the file and the recovery key**, so it also works on a new host: the backup carries
the encrypted private half. Every chunk is checked before anything is written. A changed or cut file is
refused.

Restoring to the original places:
1. Stop the services first: `systemctl --user stop atlas-link rudder-scheduler hermes-gateway`, plus the
   `*-mcp-https` units.
2. Run the restore and answer `yes`. Whatever is at each place now is moved aside to
   `<path>.pre-restore-<time>`; nothing is deleted.
3. Start the services again, and run `navigator-setup recovery check`.

Paths under your home are stored as `~/...`, so a different user name on the new host works.

## Every action is logged

`<STATE_PATH>/settings.log`: recovery key made, backup made (file name), restore (file, where). Never a key.
