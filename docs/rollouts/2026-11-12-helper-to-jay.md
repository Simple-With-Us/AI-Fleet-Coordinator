# The Server Control Helper Moves Into Jay

The owner's ruling, Thu, Nov 12, 2026:  the fleet's server-side Operator tools are owned by the `jay` account, not root ("all should be in jay really").  This moves the control helper, `fleet-ops-control.py`, and everything installed beside it, from root-owned `/usr/local/lib/fleet-resource-control` to `jay`'s own `~/.local/lib/fleet-resource-control`.

## What Was Already True

The helper process already ran as `jay`.  Checked on the live server on Nov 12:

- The `fleet-ops-ctl` key's line is in `/home/jay/.ssh/authorized_keys` with the forced command `/usr/bin/python3 -I /usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-ssh`, with no `sudo` around it.  No other forced command, unit or script on the server names the install folder.  No `muster-exec` line exists yet, and `/etc/muster` does not exist, so the `--from-muster` route is not installed.
- Muster on the Mac reaches the server only through that alias (`muster/Muster/Model/OpsControl.swift`).
- `jay` may run any command through `sudo` without a password (`sudo -n -l` shows `(ALL : ALL) NOPASSWD: ALL`).  There is no sudoers rule of the helper's own:  its two privileged commands below use that general grant.

So only the code was root's.  Root ownership was never a boundary against `jay`, who could replace it through `sudo`;  the boundary is the forced command, the allowlist and the audit log, and none of those change.

## Each Verb

| Verb | What it touches | Runs as | Root needed |
|---|---|---|---|
| `server start\|stop\|restart` of a `user` unit | `systemctl --user` | jay | No |
| `server start\|stop\|restart` of a `system` unit | `systemctl` with no `sudo` | jay | Unchanged by this move:  the helper never adds `sudo` here |
| `server logs` | `journalctl` (jay is in `systemd-journal`) | jay | No |
| `server update fleet-cli` | `sudo -n /usr/bin/systemctl start --no-block fleet-cli-git-update.service`, after `sudo -n -l` for that exact argv | jay, then root for that one argv | Yes:  it starts a system unit.  Fixed in code (`update_command`) |
| `server update-status`, `server unit-status` | `systemctl [--user] show` | jay | No |
| `server redeploy` | Coolify over HTTP, token through `fleet-secrets` | jay | No |
| `server resource-status` | reads `/run/fleet-coding-resources/status.json` (root's receipt, read only) | jay | No |
| `server resource-policy` | Infisical with jay's 0600 token, then `sudo -n systemctl kill --kill-whom=main --signal=HUP fleet-coding-memory.service` | jay, then root for the HUP | Yes for the HUP:  the controller is a root system service.  Fixed in code (`fleet_resource_control.RELOAD`) |
| `server seat-inventory` | jay's home | jay | No |
| `server eavesdropper-status`, `eavesdropper-settings`, `operator-sessions`, `operator-wakes` | `docker exec --user simpletools eavesdropper ...` (jay is in `docker`) | jay | No |
| `--from-muster` | the same verbs, `/etc/muster` read only, state in jay's home | jay | No |
| `--check-token` | Infisical and Coolify reads | jay | No |

Two commands keep root, each one fixed argv the helper builds itself:  the `fleet-cli` update and the controller HUP.  Both go through `jay`'s existing `sudo` grant, as they did before;  this change adds no sudoers rule and removes none, because the helper had none of its own.  Root also keeps what is not the helper:  the coding-memory controller and its receipt, and the owner's trust anchor `/etc/muster` when it is made.

## What Changed

- `scripts/hetzner/install-resource-control.sh` refuses root and installs as the account that runs it, under `~/.local/lib/fleet-resource-control` of that account (the home folder from the account database, never `$HOME`).  Every folder on the way must be root's or that account's and writable by neither group nor others;  the folders it makes and every file it writes are that account's, 0755 and 0644.  It now also installs `fleet-ops-eavesdropper-settings.json`, which had been copied by hand.
- `scripts/fleet-ops-control.py` changes no behaviour.  Its module rule already accepts a folder that is the calling account's own, with the account's files and no group or other write bit, which is what the new install is.  Its comment now says so.
- `scripts/fleet-server-status.py` reports the broker's files from the new folder.
- The docs name the new path.

## Install And Cut Over

On the server, as `jay`, from a clean copy of merged `main` (not a group-writable lane):

1. `bash scripts/hetzner/install-resource-control.sh`
2. In `/home/jay/.ssh/authorized_keys`, change the `fleet-ops-ctl` forced command's path to `/home/jay/.local/lib/fleet-resource-control/fleet-ops-control.py`, keeping `restrict` and every other word.  Keep a copy of the file first.
3. Check every verb answers through the forced route, from the server and from the Mac's `fleet-ops-ctl` alias.
4. Remove `/usr/local/lib/fleet-resource-control` (keep a copy in `/var/backups`).

This install also ships the merged changes that were never installed as root:  PR 2125 (`reduced_status.py`) and PR 2131 (`fleet-ops-control.py`).

**Roll back** by putting the saved `authorized_keys` back and restoring the saved root folder.
