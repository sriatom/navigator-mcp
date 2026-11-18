# Day 1: bring in your network

The host is set up ([Day 0](DAY0.md)). Now add the devices, their logins, and make the first read and the
first approved change. Start on a lab: [download one](LABS.md) if you have none.

## 1. Create the inventory

From the chat or the host. Every change to an inventory goes: questions → your answers → **preview** →
**your yes** → commit. Every version is kept.

| You have | Do |
|---|---|
| a few devices | chat: *"create a new inventory LAB1"*, then *"add LEAF1, EOS, 172.31.11.11, eAPI, credential LAB_EOS"* |
| a CSV or YAML list | chat: attach it and say *"create inventory LAB1 from this file"*; or on the host: `atlas-interview run bulk_import` |
| nothing yet | chat: *"give me the inventory template"* shows one example record per vendor |

A record needs at least the device name, the platform ("which platforms do you support?"), the management
address, the connection method and a credential tag. Mark production devices `PRODUCTION: true`: changes to
them are refused.

## 2. Add the credentials, on the host

A password is **never** typed in a chat. Create the tag (chat or host), then set the secret on the host:

```bash
atlas-interview run credential_new
atlas-interview secret <draft> LOGIN_PASSWORD        # asked without echo
```

Allow your agent to use the new inventory and tag (host only):

```bash
atlas-interview run consumer_access --consumer <your agent's consumer name>
```

Then, in the chat: *"test the login on LEAF1"*. It logs in and runs `show version`; no secret is shown.

## 3. The first read

*"show bgp summary on all spines in LAB1"*

- Only commands on the platform's allowed list run (*"which commands can I run on EOS?"*).
- Every output is saved as a record.
- Try *"compare via eapi and ssh"* or *"with debug"* to see the connection methods.

## 4. The first change

On a lab device:

*"add interface Loopback99 with description day1-test on LEAF1"*

1. The agent proposes a ticket. RUDDER makes a dry-run diff on the device.
2. The approval channel gets the request with a one-time code and a link to the change page.
3. You answer there: `/pushok CODE` (Slack: `!pushok CODE`).
4. RUDDER applies it, verifies it, and posts the result in the approval channel and your chat thread.

Then remove it the same way: removing is a new change with its own approval.

## 5. A first project

*"new project BGP_CHECK on LAB1: check every BGP session"*

A project keeps the job together: a task file, a run log, every record, and the reports in `OUT/`. It is
CLOUD or LOCAL like the chat model; LOCAL data never reaches a cloud model ([Cloud and local models](LOCAL_MODELS.md)).

Next: [Day 2: daily use](HOW_TO_USE.md).
