# Cisco: notes for NAVIGATOR's roles

## Reading
- **pyATS** (default for IOS, IOS-XE, NX-OS, IOS-XR, ASA): RUDDER starts the pyATS MCP server and gets
  Genie-parsed JSON where a parser exists, text otherwise. A run keeps one login (connection cache).
- **SSH** (`via=ssh`, or `ADAPTER: ssh` on the record): plain text through netmiko, ~2 s; useful when pyATS is
  slow or a parser is missing. `device compare … via=ssh,pyats` shows both side by side.
- **Controller-Managed cEdge (SD-WAN)**: pyATS must not enter config mode and must skip the operating-mode
  probe; the pack's pyATS defaults do that (`init_config_commands: []`, iosxe `operating_mode: true`). Without
  them unicon sends `pnpa service discovery stop` and logs in twice (~25 s).
- **SD-WAN Manager (vManage)**: `CONN_TYPE VMANAGE_API` reads the whole overlay over REST (GET only);
  per-device commands need the device's `SYSTEM_IP`. `CONN_TYPE SSH` (or `via=ssh`) reads the Manager's own
  CLI as text.

## Changing (CONFIG_GENERATOR, CONFIG_PUSHER)
- Lines as in `show running-config`: parents at column 0, children indented one space
  (`interface Loopback10` / ` description test`). Full command words, no abbreviations.
- RUDDER computes the diff from the running config, saves an archive / checkpoint, applies over SSH,
  verifies, and rolls back on failure. IOS-XR changes are refused for now (they need commit confirmed).
- SD-WAN edges in Controller-Managed mode take changes through the Manager's templates, not the CLI
  (not built).

## Verify commands (STATE_VALIDATOR, CONFIG_PUSHER)
| After a change to | Read |
|---|---|
| an interface | `show interfaces <name>`, `show ip interface brief` |
| BGP | `show ip bgp summary` / `show bgp all summary` |
| OSPF | `show ip ospf neighbor` |
| SD-WAN | `show sdwan control connections`, `show sdwan omp peers`, `show sdwan bfd sessions` (edge); Manager API reads for the overlay |
