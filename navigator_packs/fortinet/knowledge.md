# Fortinet FortiOS: notes for NAVIGATOR's roles

- Reads go through the **fortinet MCP server** (FortiGate REST `/api/v2`), started by RUDDER. The allowed
  "commands" are the server's tool names: `fgt_system_status`, `fgt_list_interfaces`, `fgt_get_routes`,
  `fgt_vpn_tunnels`, `fgt_get_policies`.
- Records: `CONN_TYPE API`, `PORT 443`; the credential tag holds the API token (never in chat).
- Changes through the delegated server are not built yet (tickets need a dry-run diff first).
- Install the server on a new host: `scripts/install_delegated.sh fortinet`.
