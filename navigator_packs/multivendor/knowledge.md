# Multivendor platforms: notes for NAVIGATOR's roles

- FRR, Cumulus, Nokia SR Linux / SR OS, VyOS, MikroTik RouterOS, SONiC, Dell OS10, Extreme EXOS, Huawei VRP,
  Ubiquiti EdgeOS: reads go through the **multivendor-cli MCP server** (netmiko, plain text), started by RUDDER.
- These platforms have **no command list yet**: reads are refused until one is added (a `commands.yml` in this
  pack, or the host's COMMANDS.yml), or, in a lab inventory, LAB_ANY_SHOW allows any plain `show …`.
- Records: `CONN_TYPE SSH`, `PORT 22`. Changes through the delegated server are not built yet.
- Install the server on a new host: `scripts/install_delegated.sh multivendor`.
