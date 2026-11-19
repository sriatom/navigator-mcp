---
title: Safe AI for network operations
hide:
  - navigation
  - toc
---

<div class="nv-hero" markdown>

<img src="logo.svg" alt="" class="nv-hero__logo">

<p class="nv-eyebrow">MCP server for network automation</p>

# Let AI agents work on your network. Safely. { .nv-hero__title }

<p class="nv-hero__lead">NAVIGATOR gives the AI agent you already use one front door to your inventory, your
credentials and your devices. It reads with care, and it changes nothing until a person approves.</p>

<div class="nv-hero__cta" markdown>
[Get started](INTEGRATION.md){ .md-button .md-button--primary }
[How it works](INTRODUCTION.md){ .md-button }
[Try a lab](LABS.md){ .md-button }
</div>

<p class="nv-pip">One command: <code>curl -fsSL https://navigator.atomidea.com/install.sh | bash</code> · or <code>pip install navigator-mcp</code> · <a href="https://pypi.org/project/navigator-mcp/">PyPI</a> · <a href="https://github.com/sriatom/navigator-mcp">GitHub</a></p>

<p class="nv-works">Works with <b>Hermes</b> · <b>NetClaw</b> · <b>OpenClaw</b> · <b>Claude Code</b> · <b>Cursor</b> · any MCP client</p>

</div>

<div class="nv-section" markdown>

## Built for the way network teams work { .nv-h2 }

<div class="nv-grid" markdown>

<div class="nv-card" markdown>
:material-lan:{ .nv-icon }
### Knows your network
One inventory of devices, controllers and out-of-band gear. Credentials are encrypted, and the agent only
ever sees their names.
</div>

<div class="nv-card" markdown>
:material-shield-search:{ .nv-icon }
### Reads with care
Only allowed show commands, through the right connection method for each device. Every output is saved
as a record.
</div>

<div class="nv-card" markdown>
:material-check-decagram:{ .nv-icon }
### Changes only with your approval
Every change is a ticket with a dry-run diff. A person approves it on a separate channel. Then it is
applied, verified, and rolled back if the check fails.
</div>

<div class="nv-card" markdown>
:material-clipboard-check-outline:{ .nv-icon }
### Validates before you approve
Optional Batfish validation models each change against the network before anyone is asked, and again
after the commit.
</div>

<div class="nv-card" markdown>
:material-account-group-outline:{ .nv-icon }
### A team of specialists
Nine roles do the work, from bulk collection to root cause. A QC role checks every result before you
see it.
</div>

<div class="nv-card" markdown>
:material-lock-outline:{ .nv-icon }
### Private data stays private
A project marked LOCAL never sends its data to a cloud model. A local model on your own network reads it
instead.
</div>

</div>
</div>

<div class="nv-section nv-section--alt" markdown>

## Every change follows one path { .nv-h2 }

<p class="nv-sub">The agent can ask for a change. It cannot approve one, apply one early or change the rules.</p>

<ol class="nv-steps">
<li><b>Ask</b><span>in plain words, in Slack, Teams, Telegram or your IDE</span></li>
<li><b>Propose</b><span>dry-run diff on the device, validation, one hash</span></li>
<li><b>Approve</b><span>a person, on a separate approval channel</span></li>
<li><b>Apply</b><span>with a rollback point on every device</span></li>
<li><b>Verify</b><span>confirm, or roll back automatically</span></li>
</ol>

[How RUDDER applies a change](RUDDER.md){ .nv-more }

</div>

<div class="nv-section" markdown>

## One front door for your agent { .nv-h2 }

<p class="nv-sub">Your agent runs the model. NAVIGATOR checks every call before it reaches your network.</p>

![How NAVIGATOR fits with your agent](front-door.svg){ .nv-figure }

<div class="nv-grid nv-grid--3" markdown>

<div class="nv-tile" markdown>
<p class="nv-tile__k">Layer 3 · the front door</p>
### NAVIGATOR
Projects, roles, the QC gate, and the checks on every call.
</div>

<div class="nv-tile" markdown>
<p class="nv-tile__k">Layer 1 · inventory and credentials</p>
### ATLAS
One inventory, an encrypted vault, and access per agent.
</div>

<div class="nv-tile" markdown>
<p class="nv-tile__k">Layer 2 · the change engine</p>
### RUDDER
The only part that reaches a device: approvals, apply, verify, roll back.
</div>

</div>

[Read the architecture](THREE_LAYERS.md){ .nv-more }

</div>

<div class="nv-section nv-section--alt" markdown>

## Teach it a vendor once { .nv-h2 }

<p class="nv-sub">Everything about one vendor lives in one pack: platforms, read commands, connection methods
and change rules. Every role learns it at once.</p>

<p class="nv-vendors"><span>Arista</span><span>Cisco</span><span>Palo Alto</span><span>Fortinet</span><span>Juniper</span><span>Aruba</span><span>F5</span><span>Check Point</span><span>Multivendor</span><span>Generic</span></p>

[Roles and vendor packs](ROLES_AND_PACKS.md){ .nv-more }

</div>

<div class="nv-section" markdown>

## Start in an afternoon { .nv-h2 }

<div class="nv-grid nv-grid--3" markdown>

<div class="nv-card nv-card--link" markdown>
:material-download:{ .nv-icon }
### 1 · Install
One Linux host and one command (`install.sh --hermes` or `--cursor`), or the same steps one by one.

[Install guide](INTEGRATION.md)
</div>

<div class="nv-card nv-card--link" markdown>
:material-cog-outline:{ .nv-icon }
### 2 · Set up
Approval channel, services, HTTPS and the recovery key.

[Day 0 setup](DAY0.md)
</div>

<div class="nv-card nv-card--link" markdown>
:material-test-tube:{ .nv-icon }
### 3 · Try a lab
An Arista EVPN fabric or a Palo Alto firewall lab, ready for containerlab.

[Labs](LABS.md)
</div>

</div>
</div>
