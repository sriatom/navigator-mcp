# Hermes integration (optional)

NAVIGATOR runs under any MCP host. These pieces are for a Hermes host only:

| Piece | Profile | Does |
|---|---|---|
| `navigator-reply/` | the chat bot | Fills `reply_to` and `requested_by` on `change propose` from the chat session (the model cannot set them); a message asking for a LOCAL project switches its thread to the local model first (as `/model local`); posts a project card (folder, CLOUD / LOCAL, inventory, model) after `project create`; remembers the thread's project and fills `project=` on later navigator calls that leave it out |
| `push-approve/` | the approval bot | `/pushok` / `/pushno` (Slack: `!pushok`), with method, channel, approver and requester checks; only approval commands pass in the approval channel; the bot's agent is kept away from the approval files |
| `soul/` | both | Example SOUL.md for the chat bot and for a tool-less approval bot |
| `navigator-guard` (not in this repo) | the chat bot | Keeps LOCAL project data away from cloud models: blocks the host's own file, shell and web tools on a LOCAL project while the thread is on a cloud endpoint (it says `/model local`), lets NAVIGATOR's own tools through, blocks any path under the private data folder, scrubs web queries in LOCAL sessions. It lives only in the profile (`plugins/navigator-guard/`) and loads its rules from the user's skills folder; it fails closed if they are missing |

## Chat model words

The chat bot's profile has two model aliases, `cloud` and `local`. In a thread, `/model cloud` or
`/model local` (any case) picks one. The default is `local` when the profile has a local model.
navigator-reply switches a thread to `local` when you ask for a LOCAL project. The rules are in
`docs/LOCAL_MODELS.md`.

See `docs/APPROVALS.md` for the approval bot.
