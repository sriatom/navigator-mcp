# Public lab downloads

`evpn_e2e/` and `palab1/` are the public, self-contained copies of the two test labs (relative paths,
admin / admin, mgmt 172.31.11.0/24 and 172.31.12.0/24, example.com). The docs site zips each into
`downloads/<lab>.zip` at build time (`docsite/public.yml` → `downloads`) and runs the leak check on them.
Made by `scripts/sanitize_labs.py` from the private lab folders; edit them here or re-run that script.
