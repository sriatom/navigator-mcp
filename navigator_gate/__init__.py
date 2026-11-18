"""navigator_gate: HTTPS and logins for the three MCP servers when they run over HTTP (decision #147).

  navigator-mcp --serve   https://<host>:8791/mcp      the front door (all tools)
  atlas-mcp --serve       https://<host>:8792/mcp      ATLAS on its own (inventory)
  rudder-mcp --serve      https://<host>:8793/mcp      RUDDER on its own (devices, changes)

- TLS: a self-signed CA made on the host (tls.py); the server certificate is signed by it. Clients trust the
  CA file (ca.pem), not each certificate.
- Logins: users with a role, admin or engineer, and a random password made by NAVIGATOR (users.py). A client
  sends `Authorization: Bearer <user>.<password>` (or HTTP Basic). Engineers cannot commit inventory edits.
- Settings: navigator.yml `HTTP: {BIND, PORTS: {NAVIGATOR, ATLAS, RUDDER}}`; files in ~/.config/navigator/tls
  and ~/.config/navigator/users.yml (mode 600). Managed with `navigator-setup tls ...` / `navigator-setup user ...`.
stdio (an app starting the server itself) is unchanged and needs none of this.
"""
