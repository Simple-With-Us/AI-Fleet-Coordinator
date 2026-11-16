# Seat Inventory On The Server

Muster's Seat Setup panel lists what each coding platform has installed (MCP servers, hooks, plugins, skills, sub-agents and commands) as names and counts.  The Mac runs `scripts/fleet_seat_inventory.py` directly.  The server's side goes through the restricted helper, so the app never needs a new SSH key, a new alias or a broader grant.

The helper gains one read-only verb, `server seat-inventory PLATFORM --plan --json`.  It imports the sibling module `fleet_seat_inventory.py` from the helper's own directory, as `resource-status` imports `fleet_resource_control.py`, and reads names and counts under the account's home.  It takes no action lock, queries no sudo, runs no command and writes no audit row;  `--yes` is refused (and audited, as every refused `--yes` is), and a platform that is not in the module's fixed list is refused.  The allowlist file is not involved:  the verb can read nothing but the fixed source list in the module, and no value from any file, only names and counts.

## Deployment

The server CLI checkout update does not replace the protected broker installation.  This change needs two files under `/usr/local/lib/fleet-resource-control`, installed the way the AFC-607 helper repair was (see `2026-11-03-afc-607-muster-update-route.md`):  a hash-verified backup of the current `fleet-ops-control.py`, the new module installed beside it as a candidate, its hash verified, and an atomic rename for each file.  Install the module first and the helper second, so the helper never imports a module that is not there yet.  Rollback restores the backup through the same candidate-and-rename procedure and removes the module.  Do not run the full installer or change sudoers, SSH, units or settings.

Ship the Muster build with the Mac and Server switch first or last, in either order:  a build that asks for the verb before the helper has it gets the helper's refusal ("unknown server verb"), which the panel shows in words, and a helper that has the verb before the build asks for it does nothing.

## Checks

- `python3 scripts/test_fleet_seat_inventory.py` and `python3 scripts/test_fleet_ops_control.py` (the verb's tests are `SeatInventoryDispatchTests`).
- `python3 -m unittest discover -s scripts/hetzner -p test_install_resource_control.py` (the payload now names the module).
- After deployment, through the Mac's real route:  `ssh fleet-ops-ctl -- server seat-inventory claude --plan --json` returns `result: planned` and an `inventory` whose `host` is `server`;  `--yes` returns `refused`;  an unknown platform returns `refused`.
- The same answer must list this account's own platform files (for `claude`, the plugins, skills and hook events under `/home/jay/.claude`) and not a set of sections that all say the file or folder is not on this host.  That is what proves the helper read the account's home and not another one.

## Deployed

Installed on the server on Saturday, November 7, 2026 at 2:06 PM by the CLAUDE seat, the way this note describes: a hash-verified backup, the module installed first and the helper second, each as a candidate beside its target with its hash checked before an atomic rename.  Nothing else changed: no installer run, no sudoers, SSH, unit or settings change.

- Backup of the helper as it was: `/var/backups/fleet-ops-control-seat-inventory-20261107T200620Z/fleet-ops-control.py` (a root-only 0700 directory), sha256 `516a3a83cd9192e0bff694c05a8b330ceffd6cffeeb759f4b5ebfca1d96b62d5`.  That file differs from `main` only by this verb.
- Installed: `fleet-ops-control.py` sha256 `ea9090452257af5a7b68574e99802dba12c028af047668880b1bebc7d30e9515` and `fleet_seat_inventory.py` sha256 `2d8916158d899ed4df504b37f212b18f4b391d8c8e0e95d56fcfa5678342d10d`, both root:root 0644 in a root:root 0755 directory.
- Checks: the module's 30 tests and the helper's 131 tests pass.  Run directly on the server, `server seat-inventory claude --plan --json` returns `planned` with host `server` and this account's own hooks (7 events), plugins (2) and skills (32), with sub-agents and commands reported as not on this host.  Through the Mac's real route (`ssh fleet-ops-ctl`), the same call returns the same answer, `--yes` returns `refused` (and is audited), and an unknown platform returns `refused`.
- Rollback: install the backup beside the helper as a candidate, verify its hash is the one above, rename it over the helper, then remove `fleet_seat_inventory.py`.
