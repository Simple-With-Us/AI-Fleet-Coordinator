# Seat Inventory On The Server

Muster's Seat Setup panel lists what each coding platform has installed (MCP servers, hooks, plugins, skills, sub-agents and commands) as names and counts.  The Mac runs `scripts/fleet_seat_inventory.py` directly.  The server's side goes through the restricted helper, so the app never needs a new SSH key, a new alias or a broader grant.

The helper gains one read-only verb, `server seat-inventory PLATFORM --plan --json`.  It imports the sibling module `fleet_seat_inventory.py` from the helper's own directory, as `resource-status` imports `fleet_resource_control.py`, and reads names and counts under the account's home.  It takes no action lock, queries no sudo, runs no command and writes no audit row;  `--yes` is refused (and audited, as every refused `--yes` is), and a platform that is not in the module's fixed list is refused.  The allowlist file is not involved:  the verb can read nothing but the fixed source list in the module, and no value from any file, only names and counts.

## Deployment

The server CLI checkout update does not replace the protected broker installation.  This change needs two files under `/usr/local/lib/fleet-resource-control`, installed the way the AFC-607 helper repair was (see `2026-11-03-afc-607-muster-update-route.md`):  a hash-verified backup of the current `fleet-ops-control.py`, the new module installed beside it as a candidate, its hash verified, and an atomic rename for each file.  Install the module first and the helper second, so the helper never imports a module that is not there yet.  Rollback restores the backup through the same candidate-and-rename procedure and removes the module.  Do not run the full installer or change sudoers, SSH, units or settings.

Ship the Muster build with the Mac and Server switch first or last, in either order:  a build that asks for the verb before the helper has it gets the helper's refusal ("unknown server verb"), which the panel shows in words, and a helper that has the verb before the build asks for it does nothing.

## Checks

- `python3 scripts/test_fleet_seat_inventory.py` and `python3 scripts/test_fleet_ops_control.py` (the verb's tests are `SeatInventoryDispatchTests`).
- `python3 -m unittest discover -s scripts/hetzner -p test_install_resource_control.py` (the payload now names the module).
- After deployment, through the Mac's real route:  `ssh fleet-ops-ctl -- server seat-inventory claude --plan --json` returns `result: planned` and an `inventory` whose `host` is `server`;  `--yes` returns `refused`;  an unknown platform returns `refused`.
- The same answer must list this account's own platform files (for `claude`, the plugins, skills and hook events under `/home/jay/.claude`) and not a set of sections that all say the file or folder is not on this host.  That is what proves the helper read the account's home and not another one.
