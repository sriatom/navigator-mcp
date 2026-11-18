# The first-time setup, and changing it later

Two interviews set NAVIGATOR up. Both run **on the host's shell**, never as a chat tool: an agent that could
change where approvals go, or read a password, could get around every other rule.

| Interview | Command | Sets |
|---|---|---|
| Host settings | `navigator-setup` | PROJECT_ROOT, SKILLS_ROOT, approvals, retention |
| Inventories and credentials | `atlas-interview` (on the host) or `inventory_edit` (in chat, no secrets) | devices, controllers, credential tags, who may use what |
| Models | `navigator-setup` → `MODELS` in `navigator.yml` | cloud or local host model, the local endpoint |
| Change validation | `navigator-setup` → `VALIDATION` | Batfish checks each change (§4d) |

Order the first time: install ([INTEGRATION.md](INTEGRATION.md) §0) → `navigator-setup` → inventory →
credentials → services → connect the host → a test read → a test change.

## 1. `navigator-setup` (host settings)

```bash
navigator-setup            # asks every question; Enter keeps the value in [brackets]
navigator-setup show       # what is set now
```

| Question | What to answer | Checked |
|---|---|---|
| **PROJECT_ROOT** (required) | the folder that holds project folders, data only (e.g. `/data/projects`) | absolute, exists, readable and writable |
| **SKILLS_ROOT** (optional) | a folder of your own skills, one `<skill>/SKILL.md` each (e.g. `/data/skills`); `-` for none. NAVIGATOR only reads it | absolute, readable, at least one skill |
| **APPROVAL.METHOD** | `telegram` or `slack` (Hermes approval bot), or another platform your host sends to (`teams`, `discord`…, its `/pushok` handler calls `rudder-approve`) | one of the known methods |
| **APPROVAL.BOT** | the approval bot's Hermes profile, **never the chat bot** | the profile exists; not a REPLY_PROFILE |
| **APPROVAL.CHANNEL** | Slack: the private approval channel's ID (`C…`); Telegram: a chat ID or `-` for the bot's home channel | an ID |
| **APPROVAL.APPROVERS** | `*` (anyone in the approval channel; you curate who is in it) or user IDs | IDs |
| **APPROVAL.SELF_APPROVE** | not asked (default true); a note says an approver turns it off with `/selfapprove off` in the approval channel, and only `navigator-setup set APPROVAL.SELF_APPROVE true` turns it back on | printed note |
| **APPROVAL.SHIP_ENGINEERS** | user IDs whose lab changes need no approval (lab inventories only, still audited); `-` for none | IDs, never `*` |
| **APPROVAL.PAGES_KEEP_DAYS** | days to keep finished tickets, their change pages and baseline records (default 90) | 1–3650 |
| **APPROVAL.CLEANUP_AT** | the daily clean-up time, local HH:MM (default 02:00), or `off` | HH:MM |

When approvals change (METHOD, BOT, CHANNEL, APPROVERS, SELF_APPROVE, SHIP_ENGINEERS), a **test code is sent
the new way** and nothing is saved until someone approves it there. Every file is backed up first and every
change is logged in `<STATE_PATH>/settings.log`. Afterwards restart `rudder-scheduler` and the MCP host.

Not asked, set with `navigator-setup set`: `APPROVAL.REPLY_PROFILES` (chat bots that may post results in a
thread), `APPROVAL.WEB_BIND` / `WEB_URL` (change pages on TCP 8790), `APPROVAL.TTL_MIN` (minutes a ticket
may wait), `APPROVAL.MENTION`.

## 2. Inventories

On the host (`atlas-interview run <topic>`) or from the chat (`inventory_edit`, the same questions). Every
change goes: questions → your answers → **preview** → **your yes** → commit; every version is kept
(`atlas-interview history <inventory>`).

| Topic | For |
|---|---|
| `inventory_new` | a new inventory (name, lab or production, default credential tag) |
| `record_add` / `record_edit` / `record_remove` | one device, controller or out-of-band record |
| `bulk_import` | many records from a CSV / YAML you have |
| `bulk_edit` | one field on many records (e.g. `ADAPTER=pyats` on all IOS-XE) |
| `credential_new` | a credential tag (the value is set separately, below) |
| `consumer_access` | **host only**: which agent (consumer) may use which inventories and tags |

"Give me the inventory template" (in chat) shows one example record per vendor and connection method. A
record needs at least DEVICE_NAME, OS (a platform from the catalogue: "which platforms do you support?"),
the management address, the connection type and a credential tag. Mark production devices `PRODUCTION: true`:
changes to them are refused unless the host allows it.

## 3. Credentials

Passwords are **never** typed in a chat. After `credential_new` (or a record that needs a secret), on the host:

```bash
atlas-interview secret <draft> LOGIN_PASSWORD        # asked with getpass, or --file
atlas-interview secret <draft> ENABLE_SECRET
```

Then, in chat: "test the login on QALEAF1A" (`credential action=test`: login + `show version`, no secret
shown). The vault is encrypted (AES-256-GCM); only `atlas-link` holds the key ([ATLAS.md](ATLAS.md)).

### rudder-approve (hosts other than Hermes)

After the approval questions, the interview asks whether a host other than Hermes' push-approve plugin
(OpenClaw, a Teams or Discord bot…) records `!pushok` with `rudder-approve`. The default is yes when
`APPROVAL.METHOD` is not telegram or slack. Then it asks:

| Question | Answer (default from the APPROVAL settings) |
|---|---|
| `APPROVE.PLATFORMS` | where approvals may come from, e.g. `teams` |
| `APPROVE.CHANNELS` | `platform=ID` pairs, e.g. `teams=19:abc@thread.tacv2`; `-` = any channel |
| `APPROVE.APPROVERS` | `*` or user IDs |

It writes `~/.config/rudder/approve.yml` ([APPROVALS.md](APPROVALS.md) section 13).

## 4. Models: cloud and local

The interview asks three questions after the approvals ([LOCAL_MODELS.md](LOCAL_MODELS.md)):

| Question | Answer |
|---|---|
| `MODELS.HOST` | the model your MCP host itself runs: `cloud` (Claude, Gemini, GPT…) or `local` (Ollama…) |
| `MODELS.LOCAL_URL` | a local OpenAI-compatible endpoint for LOCAL work, e.g. `http://127.0.0.1:11434/v1`; `-` for none |
| `MODELS.LOCAL_MODEL` | the model's name there, e.g. `qwen3.5:9b` |

Setup checks them before saving: the URL must be a **local address** (loopback, private, link-local), the
endpoint must **answer**, and it must **have that model**. Otherwise nothing is saved. `HOST: local` needs a
local URL. Later: `navigator-setup set MODELS.LOCAL_MODEL qwen3:30b-a3b` (also `MODELS.HOST`,
`MODELS.LOCAL_URL`, `MODELS.TIMEOUT_SEC`; `MODELS.LOCAL_URL ""` removes the local model).

They are saved in `~/.config/navigator/navigator.yml` as:

```yaml
MODELS:
  HOST: cloud                                  # what your host's model is: cloud | local
  LOCAL: {URL: "http://127.0.0.1:11434/v1", MODEL: "qwen3.5:9b"}
```

- **Cloud model** (Gemini on Vertex, Claude, OpenAI…): configured in the **host** (Hermes profile model,
  Claude Code login). NAVIGATOR never calls it itself and stores no key for it.
- **Local model** (Ollama, vLLM, LM Studio, llama.cpp): an OpenAI-compatible URL on a local address. Check with
  "local model status" in chat.
- A host on the local model sets `NAVIGATOR_HOST_MODEL=local` in its MCP config; then LOCAL files may be
  read directly. A Hermes profile that switches per thread sets `NAVIGATOR_TRUST_HOST_MODEL=1` instead; its
  `navigator-reply` plugin tells NAVIGATOR each thread's model.
- `MODELS.HOST` is not the chat model. On Hermes each thread picks its own with `/model cloud` or
  `/model local` (default LOCAL when the profile has a local model); the interview does not set that.
  See [LOCAL_MODELS.md](LOCAL_MODELS.md#words-cloud-and-local).

## 4b. HTTPS for remote clients (optional)

The interview's last question: let MCP clients on other machines connect over HTTPS with a login
(ports 8791-8793)? **yes** makes the local CA, the server certificate and a first **admin** user, and prints
that user's password once. Details, more users and connecting Claude Code / Cursor / Hermes: [HTTPS.md](HTTPS.md).

## 4c. The recovery key and a first backup

Next the interview makes the **recovery key** of the CREDENTIALS key: 48 digits in 8 groups of 6, shown
once. You type it back to confirm. If there is no CREDENTIALS key yet, it makes that too. Then it offers a
first **full encrypted backup**. The backup asks for the recovery key again and checks it, so you know you
hold the right one. See [BACKUP.md](BACKUP.md).

## 4d. Change validation (optional)

Should RUDDER check each change against a model of the network before it is approved
([RUDDER](RUDDER.md#change-validation))?

| Question | Answer |
|---|---|
| `VALIDATION.MODE` | `none` (default), `batfish` or `netclaw` (the Batfish service of a NetClaw install) |
| `VALIDATION.HOST` | batfish / netclaw: the Batfish host, a **local or private address** (default `127.0.0.1`; netclaw finds it from `BATFISH_HOST`) |
| `VALIDATION.PORT` | batfish: its port; `-` for pybatfish's default (9996) |
| `VALIDATION.ON_UNREACHABLE` | if the validator is down: `block` the change (default) or `warn` and go on |

No Batfish yet? Start one on the host and install the client:

```bash
docker run -d --name batfish --restart unless-stopped -p 127.0.0.1:9996:9996 batfish/allinone
~/navigator/.venv/bin/pip install "navigator[batfish]"
```

The settings are saved only after a **test snapshot gets an answer** from the validator. A fail before the push
blocks the change; after the commit the live config is checked again and a fail rolls the device back.
Not asked, set with `navigator-setup set`: `VALIDATION.POST_PUSH false` (no check after the commit),
`VALIDATION.TIMEOUT` (seconds, default 300).

## 5. Changing it later

| Change | How |
|---|---|
| any host setting | `navigator-setup set KEY VALUE`, `add` / `remove` for lists; or run the interview again |
| test the approval path | `navigator-setup test-approval` |
| change validation | `navigator-setup set VALIDATION.MODE batfish VALIDATION.HOST 127.0.0.1` (also `PORT`, `ON_UNREACHABLE`, `POST_PUSH`, `TIMEOUT`); `VALIDATION.MODE none` turns it off |
| recovery key, backup, restore | `navigator-setup recovery init/check`, `navigator-setup backup`, `navigator-setup restore FILE` ([BACKUP.md](BACKUP.md)) |
| HTTPS users and certificate | `navigator-setup user add/reset/role/remove/list`, `navigator-setup tls init/renew/show`, `navigator-setup http show` |
| an inventory or a record | chat: "add a device …" / "change the ADAPTER of EDGE1 to ssh" (preview, yes) |
| a password | host: `atlas-interview secret …`; then "test the login on …" |
| who may use an inventory | host: `atlas-interview run consumer_access --consumer <NAME>` |
| models | `navigator-setup set MODELS.HOST local`, `MODELS.LOCAL_URL …`, `MODELS.LOCAL_MODEL …`; then restart the MCP host |
| rudder-approve (non-Hermes hosts) | `navigator-setup set APPROVE.PLATFORMS …`, `add APPROVE.CHANNELS platform=ID`, `add APPROVE.APPROVERS …` (writes `~/.config/rudder/approve.yml`, mode 600); `rudder-approve check` |

After any change: `systemctl --user restart rudder-scheduler` (and `atlas-link` after an inventory or pack
change), then restart or `/reload-mcp` the host.
