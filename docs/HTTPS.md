# HTTPS and logins for the MCP servers (8791–8793)

An app on the **same machine** (or over SSH) starts NAVIGATOR itself over stdio and needs none of this.
For an app on **another machine** that should connect by URL, run the servers over HTTPS with a login:

| Server | Port | URL | systemd user unit | What |
|---|---|---|---|---|
| `navigator-mcp` | **8791** | `https://<host>:8791/mcp` | `navigator-mcp-https` | the front door: every tool (use this one) |
| `atlas-mcp` | **8792** | `https://<host>:8792/mcp` | `atlas-mcp-https` | ATLAS on its own: inventories |
| `rudder-mcp` | **8793** | `https://<host>:8793/mcp` | `rudder-mcp-https` | RUDDER on its own: device reads and changes |
| change pages | 8790 | `https://<host>:8790/c/...` | inside `rudder-scheduler` | read-only ticket pages, same CA, no login (the link is the key) ([APPROVALS.md](APPROVALS.md)) |

Most setups need only 8791. The servers run on FastMCP's streamable HTTP inside uvicorn (the same stack
as a FastAPI app), with NAVIGATOR's gate in front (`navigator_gate`).

## What protects them

- **TLS with a local CA.** `navigator-setup tls init` makes a CA on the host (ECDSA P-256, 10 years) and a
  server certificate signed by it (825 days) for the host's names and addresses. Clients trust **the CA file**
  (`~/.config/navigator/tls/ca.pem`), once; a renewed certificate is trusted without touching the clients.
  The CA key (`ca.key`, mode 600) never leaves the host.
- **A login on every request.** Users have a **role**, `admin` or `engineer`, and a **random password made by
  NAVIGATOR** (32 characters), shown once, stored only as an scrypt hash in `~/.config/navigator/users.yml`
  (mode 600). It is **not** a device credential: ATLAS's credentials stay in the ATLAS vault.
  Clients send `Authorization: Bearer <user>.<password>` (HTTP Basic also works).
- **Roles.** `admin`: every tool. `engineer`: every tool except committing inventory changes
  (`inventory_edit` on 8791, `interview_commit` on 8792); engineers do not even see those tools. Changes to
  devices still need `!pushok` in the approval channel, whoever asks.
- **Failed logins.** Ten from one address in five minutes block that address for five minutes (HTTP 429).
- **Refused starts.** A server will not start without a user, without a certificate, or (with `--no-tls`) on
  anything but 127.0.0.1.
- **Access log.** `<STATE_PATH>/http-access.log`: time, server, user, role, address, tool, result. Never the
  arguments, never a password.

## Set it up (on the NAVIGATOR host)

```bash
navigator-setup tls init                       # add --name navigator.lab for another DNS name or IP
navigator-setup user add pat --role admin      # prints the password ONCE: put it in your password manager
cp ~/navigator/scripts/*-mcp-https.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now navigator-mcp-https          # and atlas-mcp-https / rudder-mcp-https if wanted
sudo ufw allow from <lan-cidr> to any port 8791 proto tcp comment 'NAVIGATOR MCP (HTTPS)'
navigator-setup http show                      # URLs, the CA fingerprint, client settings
```

The interview (`navigator-setup`) asks at the end whether to turn HTTPS on; yes makes the CA, the
certificate and a first admin user. Settings in `navigator.yml` (defaults shown):

```yaml
HTTP:
  BIND: 0.0.0.0                                # an address to listen on; 0.0.0.0 = all
  PORTS: {NAVIGATOR: 8791, ATLAS: 8792, RUDDER: 8793}
```

| Later | Command |
|---|---|
| another user | `navigator-setup user add <name> --role engineer` |
| a lost password | `navigator-setup user reset <name>` (the old one still works for up to 10 minutes) |
| change a role / remove a user | `navigator-setup user role <name> admin` / `navigator-setup user remove <name>` |
| list users | `navigator-setup user list` |
| a new certificate (new name, or before it expires) | `navigator-setup tls renew [--name ...]`, then restart the `*-https` units |
| check the certificate | `navigator-setup tls show` |

Every change is written to `<STATE_PATH>/settings.log` (never a password).

## Connect a client

Copy `ca.pem` to the client machine first. Check: `curl --cacert ca.pem https://<host>:8791/mcp` must
answer **401** (TLS works, login required).

On Windows, `curl.exe` needs `--ssl-no-revoke` (a local CA has no revocation list):
`curl.exe --ssl-no-revoke --cacert ca.pem -X POST https://<host>:8791/mcp` → 401. Without the CA it fails
with `SEC_E_UNTRUSTED_ROOT`, as it should. To trust the CA for every program on a Windows PC, import it into
the user's "Trusted Root Certification Authorities" (`certutil -user -addstore Root ca.pem`); Node-based
clients (Claude Code, Cursor) use `NODE_EXTRA_CA_CERTS` either way.

**Claude Code**

```bash
claude mcp add --transport http navigator https://<host>:8791/mcp --header "Authorization: Bearer <user>.<password>"
```

Start Claude Code with `NODE_EXTRA_CA_CERTS=<path to ca.pem>` (Windows: set it as a user environment variable).

**Cursor** (`~/.cursor/mcp.json`; `NODE_EXTRA_CA_CERTS` set the same way):

```json
{ "mcpServers": { "navigator": { "url": "https://<host>:8791/mcp",
                                  "headers": { "Authorization": "Bearer <user>.<password>" } } } }
```

**Hermes on another machine** (`config.yaml`, with `SSL_CERT_FILE=<path to ca.pem>` in the gateway's env):

```yaml
mcp_servers:
  navigator:
    url: https://<host>:8791/mcp
    headers: {Authorization: "Bearer <user>.<password>"}
```

Keep the password out of shared files: most hosts can read it from an environment variable (e.g.
`"Authorization": "Bearer pat.${NAVIGATOR_PASSWORD}"` where the host supports it).

## Not built yet

- OAuth / single sign-on: MCP clients' OAuth support differs; the static header works in all of them today.
- Per-user identity in tickets: tickets still name the chat user; the HTTP user is in the access log.
- Encrypting secrets in host configs (Bitwarden is an option).
