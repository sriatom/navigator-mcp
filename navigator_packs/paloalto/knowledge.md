# Palo Alto PAN-OS: notes for NAVIGATOR's roles

## Reading
- **SSH** (`CONN_TYPE SSH`, default): CLI text through netmiko (`paloalto_panos`), ~3–5 s per login.
- **XML API** (`CONN_TYPE XML_API`, or `via=xml-api` on an SSH record): the same allowed commands as
  `type=op` calls, replies as JSON; faster for many commands and easier to compare. Keywords are nested, a
  value (interface, address, quoted text) is element text; odd commands are mapped in `pack.yml`.
- The API key comes from the credential tag (`API_KEY` / `TOKEN`), else from keygen with the login (sent in
  the POST body, never in a URL). `API_PORT` on an SSH record sets the API port (default 443).
- Advanced Routing Engine (ARE) firewalls use `show advanced-routing …`; legacy ones `show routing …`.

## Changing (CONFIG_GENERATOR, CONFIG_PUSHER)
- Lines are configure-mode **`set` / `delete`** only, e.g. `set address h-10.1.1.1 ip-netmask 10.1.1.1/32`.
  Lines touching management access, admins, HA or the management profile are refused.
- RUDDER refuses a change while the candidate has someone else's uncommitted changes.
- Dry run: lines into the candidate over SSH, diff candidate vs running over the API (canonical XML,
  secrets as a short hash), then the candidate is reloaded from running: nothing left behind.
- Apply: save `rudder-<ticket>.xml` (rollback point), push, commit, wait for the job (~50 s), verify;
  rollback = load that file and commit. PAN-OS has no commit timer, so RUDDER rolls back itself.

## Verify commands (STATE_VALIDATOR, CONFIG_PUSHER)
| After a change to | Read |
|---|---|
| an address / policy | `show running security-policy`, `show running nat-policy` |
| interfaces | `show interface all`, `show interface <name>` |
| routing | `show routing route` (or `show advanced-routing route`), `show routing protocol bgp summary` |
| VPN | `show vpn ike-sa`, `show vpn ipsec-sa` |
