# Where NAVIGATOR runs

NAVIGATOR has to run **inside the network it manages**: it logs in to switches, routers and firewalls, and it
keeps the credential store and the approval machinery. So it is self-hosted, on a Linux host or VM that can
reach the devices' management addresses (today navhost).

| How a host reaches it | How | Good for |
|---|---|---|
| **stdio on the same host** | The MCP host starts `navigator-mcp` itself (Hermes, Claude Code / Gemini CLI on the host) | Default; nothing listens on the network |
| **stdio over SSH** | The MCP host on another machine runs `ssh -T <host> navigator-mcp` (CLAUDE_CODE.md) | Your own PCs; needs only SSH key access |
| **HTTPS with logins** | `navigator-mcp --serve` (systemd `navigator-mcp-https`): TLS from a local CA, a user and password per person, roles admin / engineer, on **8791** (ATLAS 8792, RUDDER 8793) ([HTTPS.md](HTTPS.md)); over the LAN, or Tailscale / WireGuard from other sites | Web-based or remote MCP hosts, other sites |
| Public cloud or a hosted automation service (e.g. Zapier) | **Not suitable**: no route to the lab networks, and credentials and approvals would leave your network. Zapier's MCP offering exposes Zapier's own app actions; it does not run your server | – |

Ports: none for MCP over stdio; **TCP 8791 / 8792 / 8793** for the MCP servers over HTTPS when turned on
([HTTPS.md](HTTPS.md)); TCP 8790 for the read-only change pages (HTTPS, same CA) ([APPROVALS.md](APPROVALS.md)).
