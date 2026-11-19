# Install

One page: install NAVIGATOR on a Linux host, set it up, then connect your agent host. Every block can be
copied as it is. Lines that start with `#` are notes.

## Before you start

| You need | Why |
|---|---|
| A Linux host or VM (Ubuntu 22.04+ or similar) that reaches your devices' management addresses | NAVIGATOR logs in to the devices and keeps the credential store ([Where it runs](HOSTING.md)) |
| Python 3.11 or newer | NAVIGATOR is a Python package, [`navigator-mcp` on PyPI](https://pypi.org/project/navigator-mcp/) |
| An approval channel: a private Slack channel with its own Slack app, or a Telegram bot | A person approves changes there, never in the chat ([Approvals](APPROVALS.md)) |
| One agent host: Hermes, NetClaw, OpenClaw, Claude Code, Cursor or any MCP client | The AI agent you talk to |

## 1. Install NAVIGATOR on the host

NAVIGATOR is the Python package [`navigator-mcp`](https://pypi.org/project/navigator-mcp/) (Apache-2.0,
source on GitHub: [sriatom/navigator-mcp](https://github.com/sriatom/navigator-mcp)). Install it with pip, or
from source if you want to change the code.

=== "pip (recommended)"

    ```bash
    sudo apt-get install -y python3-venv curl
    python3 -m venv ~/navigator/.venv
    ~/navigator/.venv/bin/pip install --upgrade pip
    ~/navigator/.venv/bin/pip install navigator-mcp      # with change validation: "navigator-mcp[batfish]"
    ```

    The package holds the servers and the commands: `navigator-mcp`, `navigator-setup`, `atlas-mcp`,
    `rudder-mcp`, `atlas-link`, `atlas-vault`, `atlas-interview`, `rudder-approve`, `rudder-scheduler`,
    `navigator-skills`. The service files and the Hermes plugins are in the repository. Get the ones for the
    version you installed, into `~/navigator`:

    ```bash
    V=$(~/navigator/.venv/bin/pip show navigator-mcp | sed -n 's/^Version: //p')
    curl -fsSL "https://github.com/sriatom/navigator-mcp/archive/refs/tags/v$V.tar.gz" \
      | tar -xz -C ~/navigator --strip-components=1 --wildcards '*/scripts/*.service' '*/integrations/*'
    ```

    The venv sits in `~/navigator/.venv` on purpose: every path on these pages (`~/navigator/.venv/bin/…`,
    `~/navigator/scripts/…`, `~/navigator/integrations/…`) is then the same for both ways.

=== "From source (git)"

    For contributors, or to run the tests:

    ```bash
    sudo apt-get install -y python3-venv git
    git clone https://github.com/sriatom/navigator-mcp.git ~/navigator
    cd ~/navigator
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip setuptools wheel
    .venv/bin/pip install --no-build-isolation -e ".[test]"
    .venv/bin/python -m pytest -q          # runs offline: no devices, no network
    ```

Put the commands on your path. A venv install puts `navigator-setup` and the other commands in
`~/navigator/.venv/bin`, so either add that folder to your PATH:

```bash
echo 'export PATH="$HOME/navigator/.venv/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
navigator-setup --help
```

or link just `navigator-setup` into `~/.local/bin` (on the PATH of most distributions):

```bash
mkdir -p ~/.local/bin && ln -sf ~/navigator/.venv/bin/navigator-setup ~/.local/bin/navigator-setup
navigator-setup --help
```

With only the link, call the other commands by their full path (`~/navigator/.venv/bin/atlas-interview` …).

## 2. Set it up

On the host's shell, never from a chat:

```bash
navigator-setup                       # the first-time interview: folders, approvals, models, HTTPS, recovery key
```

```bash
mkdir -p ~/.config/systemd/user
cp ~/navigator/scripts/atlas-link.service ~/navigator/scripts/rudder-scheduler.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now atlas-link rudder-scheduler
sudo loginctl enable-linger "$USER"   # keep the services running when you log out
```

`atlas-link` holds the credential key. `rudder-scheduler` applies approved changes and serves the change pages.

**Where the settings are read from.** Each layer looks in this order and takes the first it finds:

| | ATLAS | RUDDER | NAVIGATOR |
|---|---|---|---|
| 1. environment variable (the file must exist) | `ATLAS_BOOTSTRAP` | `RUDDER_BOOTSTRAP` | `NAVIGATOR_BOOTSTRAP` |
| 2. the file at the repository root (source install only) | `bootstrap.yml` | `rudder.yml` | `navigator.yml` |
| 3. your config folder | `~/.config/atlas/bootstrap.yml` | `~/.config/rudder/rudder.yml` | `~/.config/navigator/navigator.yml` |
| 4. built-in defaults | data `~/.atlas/data`, state `~/.atlas`, key `~/.config/atlas/vault.key` | | `PROJECT_ROOT` `~/workspace` |

`~/.config/rudder/rudder.local.yml` is merged on top of RUDDER's settings; `navigator-setup` writes the
host settings there and in `navigator.yml`. A pip install with no settings files at all still starts: the
servers answer, for example `inventory action=platforms`.
Details of every question: [Day 0](DAY0.md). Then add your devices: [Day 1](DAY1.md).

## 3. Connect your agent host

Pick your host. Give the agent **only** `navigator`: no other device MCP servers for the same devices, and no
shell access to `~/.config/atlas`, `~/.rudder` or the approval folder.

=== "Hermes"

    Hermes needs two profiles: a chat bot with NAVIGATOR as its MCP server, and a separate approval bot.
    Each one has its own Slack app or Telegram bot. Follow [Hermes, step by step](#hermes-step-by-step)
    below; it covers both profiles, the MCP config, Slack channels and Telegram.

=== "NetClaw"

    NetClaw runs as a Hermes profile, so it is the Hermes setup in NetClaw's profile. Set its name first:

    ```bash
    export NC=<your NetClaw profile name>
    ```

    ```bash
    mkdir -p ~/.hermes/profiles/$NC/plugins
    cp -r ~/navigator/integrations/hermes/navigator-reply ~/.hermes/profiles/$NC/plugins/
    navigator-setup add APPROVAL.REPLY_PROFILES $NC
    echo "$HOME"
    ```

    Add to `~/.hermes/profiles/<your NetClaw profile name>/config.yaml`:

    ```yaml
    mcp_servers:
      navigator:
        command: /home/<user>/navigator/.venv/bin/navigator-mcp
        env:
          NAVIGATOR_BOOTSTRAP: /home/<user>/.config/navigator/navigator.yml
          NAVIGATOR_TRUST_HOST_MODEL: "1"
        connect_timeout: 60
        enabled: true
    plugins:
      enabled:
        - navigator-reply
    ```

    - Add the change rules to NetClaw's SOUL: copy the "Changes" section of
      `~/navigator/integrations/hermes/soul/chatbot-ChatBot-SOUL.md`.
    - Turn NetClaw's own device tools off for the devices NAVIGATOR manages, so every device call goes through
      the front door.
    - Set up the approval bot as a separate profile: steps 5 to 7 of [Hermes, step by step](#hermes-step-by-step).

    ```bash
    systemctl --user restart hermes-gateway
    ```

=== "OpenClaw"

    Add to `openclaw.json` (the MCP section of your OpenClaw version):

    ```json
    { "mcp": { "servers": { "navigator": {
        "command": "/home/<user>/navigator/.venv/bin/navigator-mcp",
        "env": { "NAVIGATOR_BOOTSTRAP": "/home/<user>/.config/navigator/navigator.yml" } } } } }
    ```

    OpenClaw on another machine connects over HTTPS with a login instead ([HTTPS](HTTPS.md)):

    ```bash
    navigator-setup tls init                               # once: the local CA and the certificate
    navigator-setup user add openclaw --role engineer      # prints the password once
    cp ~/navigator/scripts/navigator-mcp-https.service ~/.config/systemd/user/ && systemctl --user daemon-reload
    systemctl --user enable --now navigator-mcp-https      # serves https://<host>:8791/mcp
    ```

    Approvals: OpenClaw's own `/pushok` command handler (gateway code, not a model tool) runs:

    ```bash
    rudder-approve ok CODE --user <sender id> --platform slack --channel <channel id>
    ```

    `navigator-setup` writes the config `rudder-approve` needs when you answer yes to its RUDDER-APPROVE
    question ([Approvals](APPROVALS.md)).

=== "Claude Code"

    On the NAVIGATOR host itself:

    ```bash
    claude mcp add navigator --scope user -e NAVIGATOR_BOOTSTRAP=$HOME/.config/navigator/navigator.yml -- $HOME/navigator/.venv/bin/navigator-mcp
    ```

    From your PC, over SSH (no port to open; `ssh navhost` must work with a key, no password prompt):

    ```bash
    claude mcp add navigator --scope user -- ssh -T -o BatchMode=yes navhost 'NAVIGATOR_BOOTSTRAP=$HOME/.config/navigator/navigator.yml exec $HOME/navigator/.venv/bin/navigator-mcp 2>/dev/null'
    ```

    Replace `navhost` with your host's SSH name. Then in Claude Code: `/mcp` must show `navigator` connected
    with 16 tools. Claude Code has no chat thread for results: approve on the approval channel, then ask for
    the ticket's status.

=== "Cursor"

    Put this in `~/.cursor/mcp.json` (every project) or `.cursor/mcp.json` (one project), then open
    *Settings → MCP*:

    ```json
    {
      "mcpServers": {
        "navigator": {
          "type": "stdio",
          "command": "ssh",
          "args": ["-T", "-o", "BatchMode=yes", "navhost",
                   "NAVIGATOR_BOOTSTRAP=$HOME/.config/navigator/navigator.yml exec $HOME/navigator/.venv/bin/navigator-mcp 2>/dev/null"]
        }
      }
    }
    ```

    Replace `navhost` with your host's SSH name. On the NAVIGATOR host itself, use `"command":
    "/home/<user>/navigator/.venv/bin/navigator-mcp"` with `"env": {"NAVIGATOR_BOOTSTRAP":
    "/home/<user>/.config/navigator/navigator.yml"}` instead.

=== "Any MCP client"

    Over stdio, start this command (on the host, or through `ssh -T navhost '…'`):

    ```bash
    NAVIGATOR_BOOTSTRAP=$HOME/.config/navigator/navigator.yml $HOME/navigator/.venv/bin/navigator-mcp
    ```

    Over HTTPS, connect to `https://<host>:8791/mcp` with a user from `navigator-setup user add` ([HTTPS](HTTPS.md)).

    To post results back in a chat thread, set `reply_to` (`<profile>/<platform>:<chat>[:<thread>]`) and
    `requested_by` (`<user id>|<name>`) on `change action=propose` **from your host code, never from the model**.
    For approvals, call `rudder-approve` from your `/pushok` handler.

### Hermes, step by step

You build two Hermes profiles. Each one is a bot with its own Slack app or Telegram bot:

| Profile | Does | Connects to |
|---|---|---|
| `navigator` | the chat bot you talk to; it has NAVIGATOR as its MCP server and can only *ask* for changes | a Slack app (or a Telegram bot) and your chat channel |
| `approvals` | the approval bot; it posts approval requests and takes `/pushok`, nothing else | its **own** Slack app in a private approval channel, or its own Telegram bot |

Never give the two profiles the same Slack app or Telegram bot. If one app both asked and approved, a model
could approve its own change ([Approvals](APPROVALS.md)).

**1. Create the chat bot profile**

```bash
hermes profile create navigator --description "Network chat bot: NAVIGATOR front door"
hermes -p navigator model             # pick the model and provider for this profile
```

`profile create` also makes a short command, `navigator`, that runs Hermes as this profile. These steps use
`hermes -p navigator` so that it is always clear which profile you change.

**2. Install NAVIGATOR as its MCP server**

```bash
mkdir -p ~/.hermes/profiles/navigator/plugins
cp -r ~/navigator/integrations/hermes/navigator-reply ~/.hermes/profiles/navigator/plugins/
cp ~/navigator/integrations/hermes/soul/chatbot-ChatBot-SOUL.md ~/.hermes/profiles/navigator/SOUL.md
navigator-setup add APPROVAL.REPLY_PROFILES navigator
echo "$HOME"                          # use this path in the config below
```

Add this to `~/.hermes/profiles/navigator/config.yaml`. Replace `/home/<user>` with the path printed above.
If the file already has a `plugins:` section, add `navigator-reply` to its `enabled:` list.

```yaml
mcp_servers:
  navigator:
    command: /home/<user>/navigator/.venv/bin/navigator-mcp
    env:
      NAVIGATOR_BOOTSTRAP: /home/<user>/.config/navigator/navigator.yml
      NAVIGATOR_TRUST_HOST_MODEL: "1"     # navigator-reply tells NAVIGATOR each thread's model
    connect_timeout: 60
    enabled: true
plugins:
  enabled:
    - navigator-reply
```

Give the profile no other device MCP servers. All device access must go through `navigator`.

**3. Connect the chat bot to Slack**

Make a Slack app manifest with every Hermes command in it:

```bash
hermes -p navigator slack manifest --name ChatBot --write ~/netplane-slack-manifest.json
cat ~/netplane-slack-manifest.json
```

1. At api.slack.com/apps: **Create New App → From a manifest**, pick your workspace, paste the file, and
   create it.
2. The manifest turns Socket Mode on. Under **Basic Information → App-Level Tokens**, make a token with the
   `connections:write` scope. This is the app token (`xapp-…`).
3. **Install to Workspace**. Under **OAuth & Permissions**, copy the bot token (`xoxb-…`).
4. Find the IDs you need:
    - **Your Slack user ID**: your profile → ⋮ → *Copy member ID* (`U…`).
    - **The channel ID**: open the channel → its name → the bottom of the *About* tab (`C…`).
5. In the channel: `/invite @ChatBot`.

Put the tokens and IDs in the profile's `.env`. Never put them in `config.yaml` or in a chat.

```bash
nano ~/.hermes/profiles/navigator/.env
```

```ini
SLACK_BOT_TOKEN=<xoxb-… bot token>
SLACK_APP_TOKEN=<xapp-… app token>
SLACK_ALLOWED_USERS=<your user ID>,<a colleague's user ID>   # who may talk to the bot
SLACK_HOME_CHANNEL=<C… channel ID>                           # where the bot posts results and notices
SLACK_FREE_RESPONSE_CHANNELS=<C… channel ID>                 # optional: answer there without an @mention
```

```bash
chmod 600 ~/.hermes/profiles/navigator/.env
```

In other channels the bot answers only a real @mention, picked from Slack's list.

**4. Or connect the chat bot to Telegram**

1. In Telegram, message **@BotFather**: `/newbot`, then a name and a username. It replies with the bot token.
2. Find your numeric Telegram user ID, for example by messaging **@userinfobot**.
3. For a group: add the bot to the group. To let it read every message there, not only commands and replies,
   turn off privacy mode in @BotFather (`/setprivacy` → Disable).

```ini
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_ALLOWED_USERS=<your user ID>                  # who may talk to the bot
TELEGRAM_HOME_CHANNEL=<chat ID>                        # optional: where it posts results
TELEGRAM_GROUP_ALLOWED_CHATS=<group chat ID>           # optional: a group it may work in
```

You can also send `/sethome` in the chat you want as the home channel.

**5. Create the approval bot profile**

```bash
hermes profile create approvals --description "Approval bot: /pushok only, no agent tools"
mkdir -p ~/.hermes/profiles/approvals/plugins
cp -r ~/navigator/integrations/hermes/push-approve ~/.hermes/profiles/approvals/plugins/
cp ~/navigator/integrations/hermes/soul/navi-approval-bot-SOUL.md ~/.hermes/profiles/approvals/SOUL.md
```

In `~/.hermes/profiles/approvals/config.yaml`, enable the plugin:

```yaml
plugins:
  enabled:
    - push-approve
```

Turn the profile's terminal, code, file and web tools off, so its agent can never act on a code
([Approvals, section 12](APPROVALS.md#12-keep-the-approval-bots-agent-away-from-approvals)).
Do **not** add `navigator` as an MCP server here.

**6. Connect the approval bot: Slack or Telegram**

*Slack* (a **second** Slack app, never the chat bot's):

1. Create a **private** channel, for example `#approvals`, and copy its ID (`C…`).
2. Create the app the same way as in step 3, with
   `hermes -p approvals slack manifest --name NAVI --write ~/navi-slack-manifest.json`.
   Make sure it has the bot events `app_mention` and `message.groups`, and the scopes `app_mentions:read`,
   `groups:history` and `chat:write`.
3. In the channel: `/invite @ApprovalBot`. Only the approval bot belongs there, never the chat bot.
4. Put its tokens in `~/.hermes/profiles/approvals/.env`, and let it take `!pushok` there without an @mention:

```ini
SLACK_BOT_TOKEN=<the approval app's xoxb-… token>
SLACK_APP_TOKEN=<the approval app's xapp-… token>
SLACK_ALLOWED_USERS=<the approvers' user IDs>
SLACK_FREE_RESPONSE_CHANNELS=<C… approval channel ID>
```

```bash
navigator-setup set APPROVAL.BOT approvals APPROVAL.METHOD slack APPROVAL.CHANNEL <C… approval channel ID>
```

*Telegram* (a **second** Telegram bot from @BotFather):

```ini
TELEGRAM_BOT_TOKEN=<the approval bot's token>
TELEGRAM_ALLOWED_USERS=<the approvers' user IDs>
TELEGRAM_HOME_CHANNEL=<chat ID where approval requests go>
```

```bash
navigator-setup set APPROVAL.BOT approvals APPROVAL.METHOD telegram
```

Run `chmod 600 ~/.hermes/profiles/approvals/.env`. `navigator-setup` sends a test code the new way and saves
the setting only after you approve it: `!pushok CODE` on Slack, `/pushok CODE` on Telegram.

**7. Start the gateway and check it**

One gateway runs every profile:

```bash
hermes gateway install                # once: installs hermes-gateway as a systemd user service
systemctl --user restart hermes-gateway
hermes gateway list                   # navigator and approvals: ✓ running
```

- In the chat channel, send `/mcp`: it must list `navigator` with 16 tools.
- Then ask: "which navigator tools do you have?"
- Restart the gateway after every change to a profile's `config.yaml` or `.env`.

## 4. Check it

```bash
systemctl --user is-active atlas-link rudder-scheduler     # both: active
navigator-setup show                                       # folders, approvals, models as you want them
navigator-setup test-approval                              # a test code reaches your approval channel
```

Then, in the chat:

- [ ] "which navigator tools do you have?" lists 16 tools.
- [ ] "show version on <a lab device>" returns the output.
- [ ] A test change on a lab loopback reaches the approval channel, and only a person can approve it.

No lab yet? [Download one](LABS.md).
