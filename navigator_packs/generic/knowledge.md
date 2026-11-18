# Generic platforms (netmiko fallback) and Linux

- Every platform netmiko knows that no other pack owns becomes a **generic** platform (scripts/gen_platforms.py,
  defaults in this pack's `generic_defaults`): reads over SSH, raw text, only allowlisted commands, no writes.
- Most generic platforms have no command list: add one (a pack's commands.yml or the host's COMMANDS.yml), or
  use LAB_ANY_SHOW in a lab inventory.
- Linux hosts (`OS LINUX`): reads over SSH (netmiko `linux`), text.
- Turning this pack off (`NAVIGATOR_PACKS` without `generic`) hides all generic platforms and Linux.
