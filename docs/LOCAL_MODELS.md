# Local and cloud models

NAVIGATOR has **no model of its own**: your host's agent (Hermes, OpenClaw, Claude Code, Cursor…) does the
thinking, on whatever model you gave it. NAVIGATOR only needs to know two things:

1. **What model the chat runs on**: `cloud` or `local`. Then it can keep a LOCAL project's data away from
   a cloud model.
2. **Where a local model is**, so that it can let that model read LOCAL files when the chat runs on a
   cloud model.

## A project follows the model (#156)

A project's mode uses the same two words as the chat model:

- **CLOUD**: cloud models may see the project's data.
- **LOCAL**: only a local model may see it.

A new project takes the model the chat runs on when it is created: "new project X" on a local thread is
LOCAL. Say "as CLOUD" or "as LOCAL" to choose. LOCAL → CLOUD needs your `/pushok`; CLOUD → LOCAL is
immediate. Projects made with the older words OPEN and LOCKED are read as CLOUD and LOCAL.

## The rule

| Project mode | Chat on a cloud model | Chat on a local model |
|---|---|---|
| CLOUD | the model reads the project files and device output | the same |
| LOCAL | the model **does not** read them: device reads return record paths only, and `local_model action=ask` lets the local model read the files; its answer is written to `<project>/OUT/LOCAL_MODEL_<time>.md` and the chat gets only the file name and size | the model reads the files and device output |

Every project with an inventory keeps to it. LOCAL also makes web searches and lessons generic and shows
only file paths for troubleshooting records, whatever the model.

### Device reads in a LOCAL project

With a cloud host model, `device read` (and `facts`, `compare`) in a LOCAL project returns the status,
timing and **record paths** only, never the outputs. To work on them, the host passes the record path to
`local_model action=ask` (files=[...]) or to `scrambler action=sanitize` (inp=<record>, name=<file name>),
which write their results in the project and return only a path. navigator-guard lets NAVIGATOR's own tools
through for this, because they keep the data on the host; it still blocks the host's own file, shell and web
tools on a LOCAL project.

## Settings (`~/.config/navigator/navigator.yml`)

```yaml
MODELS:
  HOST: cloud                        # what the MCP host's model is: cloud (default) | local
  LOCAL:                             # an OpenAI-compatible endpoint on this host or your LAN
    URL: http://127.0.0.1:11434/v1   # Ollama; vLLM, LM Studio and llama.cpp server work the same way
    MODEL: qwen3.5:9b
    API_KEY_ENV: ""                  # the NAME of an env var holding a key, if the endpoint needs one
    TIMEOUT_SEC: 300
    MAX_INPUT_CHARS: 60000           # how much project text one call may send
    ALLOW_HOSTS: []                  # host names you vouch for as your own machines
```

- `HOST` can also come from the host's MCP config: env `NAVIGATOR_HOST_MODEL=local`. Use it when one
  NAVIGATOR install serves two hosts (for example a Hermes profile on Ollama and another on a cloud model).
- The local URL must resolve to a **loopback, private or link-local** address (or be in `ALLOW_HOSTS`):
  a LOCAL project's data must not leave your network. Proxies are not used.
- No key is stored here. Encrypting keys in host configs is planned (Bitwarden Secrets Manager is one option).

## The tool

```text
local_model action=status                     # host model cloud/local; local endpoint reachable, models
local_model action=ask project=JOB1 task="Which OSPF neighbors are not FULL?" files=["RAW/show-ospf.txt"]
```

`files` are paths inside the project (default `RAW/`). Every call goes in the project's `RUNLOG.md` (for a
LOCAL project on a cloud host without the task text).

## Running the host itself on a local model

For a fully local setup, point the host's own model at the same endpoint and set `HOST: local`:

- **Hermes**: a profile whose `model` is the Ollama endpoint (`provider: custom`, base URL
  `http://127.0.0.1:11434/v1`), and `NAVIGATOR_HOST_MODEL: local` in that profile's `mcp_servers.navigator.env`.
  A profile that switches between cloud and local per thread sets `NAVIGATOR_TRUST_HOST_MODEL: "1"` there
  instead, and the `navigator-reply` plugin tells NAVIGATOR each thread's model.
- **OpenClaw / Cursor / Claude Code**: their own model settings; Claude Code runs on Anthropic's models (or
  Vertex / Bedrock), so it is a `cloud` host.

Small local models are slow on a CPU (a 9B model took about 90 s for a short log on navhost) and weaker at tool
use; a common split is a cloud host model for CLOUD work and `local_model` for LOCAL files.

### Words: CLOUD and LOCAL

The same two words name the **chat model** (where the bot's own thinking runs, set per chat thread) and
the **project mode** (who may see a project's data). In Hermes: `/model cloud` and `/model local` (any
case). The default chat model is LOCAL when the profile has a local model.

| Setting | Where | Default |
|---|---|---|
| The chat model | the Hermes thread: `/model cloud`, `/model local` | LOCAL, if the profile has a local model |
| The project mode | the project folder (`.navigator_mode`) | the chat model when the project is created |
| What NAVIGATOR takes the chat's model to be | `host_model=` on the call, trusted only when the host's MCP config sets `NAVIGATOR_TRUST_HOST_MODEL=1`; otherwise `NAVIGATOR_HOST_MODEL` or `MODELS.HOST` | `cloud` |

**How NAVIGATOR knows the chat's model.** On Hermes, the `navigator-reply` plugin fills `host_model=` on
project, device, troubleshoot and local_model calls from the thread's real model (its last API request),
whatever the model wrote. The profile's MCP config sets `NAVIGATOR_TRUST_HOST_MODEL=1`, so NAVIGATOR
believes it. A host that cannot say which model it runs leaves the trust off; NAVIGATOR then uses the fixed
setting and errs on the cloud side.

| | Chat on CLOUD | Chat on LOCAL |
|---|---|---|
| **CLOUD project** | The bot sees device output and files and may search the web directly. Fastest | Everything works; nothing leaves the host; slower |
| **LOCAL project** | Device reads return status and record paths only (#152); navigator-guard blocks the host's own file, shell and web tools on the project; analysis through `local_model ask` or `scrambler sanitize`. The card warns | Everything works on the host: navigator-guard lets the bot read the project's files, and NAVIGATOR returns device output because the chat is local (`host_model`) |

### Hermes: LOCAL projects switch the thread by themselves

With the `navigator-reply` plugin, a message that starts or opens a LOCAL project ("new LOCAL project
X", "create project X as local", any case) on a thread that is on the cloud model runs `/model local`
before the message is handled, so the chat itself runs on the local model (the profile's
`model_aliases.local`). Nothing changes for CLOUD projects. `/model local` and `/model cloud` work by hand,
in any case.

After `project create` the thread gets a card:

```
🔒 Project DEMO3 (new)
• Folder: /data/projects/DEMO3
• Mode: LOCAL   • Inventory: EVPN_LAB
• Model: gemma4:12b (local) — switched automatically for LOCAL
```

A LOCAL project on a cloud model gets a warning line with `/model local`. Replies on the local model are
much slower (tens of seconds per step on navhost's CPU).

### Small local models and tool arguments

gemma4:12b sometimes leaves out `project=` or guesses argument names. The `navigator-reply` plugin
remembers each thread's project (from `project create`, or any successful call that named one) and fills
`project=` in when a navigator call has none; an explicit project is never replaced. The `inventory` tool
takes `inventory=` as well as `name=`.

## Try it

1. Run `navigator-setup` (or `navigator-setup set MODELS.LOCAL_URL http://127.0.0.1:11434/v1 MODELS.LOCAL_MODEL qwen3.5:9b`),
   or add the `MODELS` block above to `~/.config/navigator/navigator.yml` by hand,
   and restart the services that run NAVIGATOR (on navhost: `systemctl --user restart atlas-link rudder-scheduler hermes-gateway`).
2. Ask "local model status": `configured`, `reachable` and `has_model` must all be true.
3. Run tests N1–N9 in TESTING_CHAT.md (or CC9 in TESTING_CLAUDE_CODE.md).

`navigator-setup` asks for them (MODELS.HOST, MODELS.LOCAL_URL, MODELS.LOCAL_MODEL) and saves them only if the URL is a local address that answers with that model; change one later with `navigator-setup set MODELS.<KEY> VALUE`.

## What is not built

- NAVIGATOR does not route a whole conversation between models; the host picks its model.
- Roles (QC, ANALYZER…) run as the host's subagents on the host's model. A host that can give a subagent its
  own model (Hermes `delegate_task` with a model override) can run them locally; NAVIGATOR does not do it.
- The scrambler is rule-based and calls no model.
