# Arista EOS: notes for NAVIGATOR's roles

## Reading
- **eAPI first** (`CONN_TYPE EAPI`, HTTPS 443, `/command-api`): every `show` returns JSON. A command without a
  JSON form (some `show running-config` sections) comes back as text automatically.
- **SSH** (`CONN_TYPE SSH` or `via=ssh`): RUDDER sends `<command> | json`, so the output looks like eAPI;
  without JSON it falls back to text. Typical times on cEOS: eAPI ~0.1 s, SSH ~3 s.
- RUDDER always enters `enable` first (as pyeapi does); many show commands need privileged mode.
- Management usually sits in a VRF (lab: `M`); eAPI must listen there:
  `management api http-commands` / `vrf M` / `no shutdown`.
- In containerlab, the image tag can differ from the real version: trust `show version`, not the tag.

## Changing (CONFIG_GENERATOR, CONFIG_PUSHER)
- Lines are plain config-mode lines, one per item, no `configure` / `end`
  (e.g. `interface Loopback97`, `description test`).
- RUDDER uses a **configuration session**: dry run = `configure session S` + lines + `show session-config
  named S diffs` + abort; apply = `commit timer 00:05:00` (the switch rolls back by itself if RUDDER does not
  confirm); confirm = `configure session S commit`; failure = abort.
- A dry-run diff of nothing ("these lines change nothing") means the device already has them.
- `no interface LoopbackN` removes it; descriptions are free text.
- Changes need a `CONN_TYPE EAPI` record; SSH writes are not built.

## Verify commands (STATE_VALIDATOR, CONFIG_PUSHER)
| After a change to | Read |
|---|---|
| an interface | `show interfaces <name>`, `show ip interface brief` |
| BGP | `show ip bgp summary`, `show bgp evpn summary` (EVPN) |
| EVPN / VXLAN | `show bgp evpn summary`, `show vxlan vtep`, `show vxlan address-table` |
| MLAG | `show mlag`, `show mlag config-sanity` |
| routing | `show ip route vrf <vrf> <prefix>` |

## Validators
ANTA (Arista Network Test Automation) can validate EOS directly; Batfish and SuzieQ also know EOS.
