# TASK — {PROJECT}

```yaml
ROOT_FOLDER: {ROOT}
OUTPUT_FOLDER: OUT        # deliverables only
MODE: {MODE}              # authoritative copy is .navigator_mode; change only with the mode tool
INVENTORY: {INVENTORY}    # ATLAS inventory for device reads and changes (RUDDER)
CREATED: {CREATED}
```

## Job

{DESC}

- What happened, when (with timezone), and what "done" looks like:
- Known changes / maintenance in the window:
- Expected behaviour (e.g. "BGP should remain stable"):

## Rules

- Vendor AND product/OS AND version must match for every doc, command, config and log
  interpretation. IOS-XE is not NX-OS. Unknown version = ask the user.
- Facts only from this folder, command output, or cited documentation. No guessing,
  no answers from memory. Every claim cites file + line, command output, or doc URL.
- Reused solutions cite where they came from.
- Every deliverable passes SANITY_AGENT (qc_check) before the user sees it.

## Folders

- `LOGS/`    device logs (LOG_REVIEWER)
- `CONFIG/`  current device configurations
- `RAW/`     originals to scramble before they go to a public model (sanitize)
- `RECORDS/` RUDDER read records and change tickets, PUBLIC_QUERIES.md
- `OUT/`     deliverables: reports, generated configs, HLD/LLD, diagrams

## Device reference table

Fill BEFORE analysis. Every hostname, alias and IP seen in logs/configs maps to exactly
one row. Unmapped name = stop and ask.

- Actual device name:
  Alias names:
  Management IP:
  Software:            # vendor + OS + version, e.g. Cisco NX-OS 10.4(5)
  Hardware:            # e.g. Cisco Nexus9000 C93360YC-FX2
  Datacenter / site:
  Role:
