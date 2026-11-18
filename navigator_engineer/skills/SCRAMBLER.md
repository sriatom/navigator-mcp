---
name: navigator-scrambler
description: "Scramble config and state output before it goes to a public AI model, and put real names back into the model's answer. default mode removes secrets only; deep mode also replaces public IPs, public BGP ASNs and identifying data. Cisco IOS/IOS-XE/NX-OS/Viptela/ASA, Arista EOS, Palo Alto PAN-OS, FortiOS."
version: 1.0.0
tags: [Networking, Security, Privacy, Sanitize, Scramble]
---

# SCRAMBLER — sanitize / restore

The scrambling is done by code (same input = same output, every run is checked for leaks), not by a model.

## Modes

| Mode | Removed or replaced | Restorable |
|---|---|---|
| `default` | Secrets only: passwords, type-7, enable secrets, TACACS/RADIUS keys, SNMP communities and v3 keys, routing auth keys, PSKs, hashes, certificates / private keys → `<PASSWORD type=7>`, `<KEY>`, `<PSK>`, `<SNMP-COMMUNITY>`, `<PEM-REDACTED>`. Everything else stays as it is | nothing to restore (secrets are never stored) |
| `deep` | default + publicly traceable data: public IPv4 / IPv6 (→ 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 198.18.0.0/15, 2001:db8::/32), public BGP ASNs (→ 64496+), hostnames (→ DC1-SW1), domains, emails, user names, AAA groups, descriptions, VLAN names, SNMP location / contact, org names, banners, serials, MACs, denylist terms | yes, with `restore`, per project |

`deep` options: `level: strict` also maps object / ACL / route-map / VRF / zone / policy names; `map_private`
also maps RFC1918 / CGNAT addresses; `keep_descriptions` keeps description text (names and IPs inside are
still replaced); `terms` adds denylist words (customer, company, city, carrier, person).

## Tools

```
sanitize(project, inp="RAW", mode="default"|"deep", terms=[...])   # files -> <P>/SANITIZED/<ts>-<mode>/
sanitize_text(text, mode="default")                                 # a piece of output, no project needed
sanitize_text(text, mode="deep", project=P)                         # deep needs the project's map
restore(project, text=...)  /  restore(project, inp="ANSWERS/a.md") # fake names -> real, in <P>/RESTORED/<ts>/
scrambler_map(project)                                              # counts only
```

## Rules for the agent

1. Only send files from a `SANITIZED/<ts>-<mode>/` folder whose result said `verify: PASS`. A folder ending in
   `-FAILED` means something real survived: do not upload; add `terms` or use `level: strict` and run again.
2. Send the text of `_MODEL_NOTE.txt` with the files, so the model keeps the tokens exactly as written.
3. Never upload the raw input, `RESTORED/`, or anything from the private map folder.
4. Removed secrets are gone for good: `<PASSWORD type=7>` stays a placeholder after restore. Never guess or
   fill in secret values.
5. The map is per project and permanent: the same device is `DC1-SW1` in every run of that project, so
   answers to earlier uploads can still be restored.
6. LOCAL projects: `restore` writes the restored answer to the project only (for the local model); it does
   not return it in the reply.
