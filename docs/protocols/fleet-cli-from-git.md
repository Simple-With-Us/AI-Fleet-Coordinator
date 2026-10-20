# Fleet tools on the server, from git

Since 2026-10-19 the Hetzner server runs `agent-sync`, `board`, `recall`, `lane` and `fleet-mode` straight from a git checkout of AI-Fleet-Coordinator main.  Git is the history, so no release copies are stored.  This replaces the Mac-pushed releases in `coolify-fleet-cli-sync.md` (owner 2026-10-19:  "use git for everything").

## Layout

- `~/.local/share/fleet-cli/repo` is a clone of AFC, checked out detached at the live commit.  Nothing else edits it.
- `~/.local/share/fleet-cli/current` is a symlink to `repo`.  The wrappers in `~/.local/bin` resolve `current` and exec the script inside it, so they did not change.
- `~/.local/share/fleet-cli/git-state.json` holds the live commit, the previous good one, and any commit skipped after a failed smoke test.
- `~/.local/share/fleet-cli/paused` keeps the old receiver from repointing `current` if a Mac push ever arrives.

## How an update happens

1. A merge to main that touches the tools runs `.github/workflows/fleet-cli-server-update.yml`.  It SSHes to the server with a key whose `authorized_keys` entry is `restrict` plus a forced command that only starts `fleet-cli-git-update.service`.  The key can do nothing else.
2. The service runs `scripts/fleet-cli-git-update.py update`.  It fetches main, checks the new commit out, and runs the smoke test (every tool starts with an empty HOME).  A failure returns to the last good commit and skips the bad one until main moves again.  Any wrapper the host lacks is written;  existing ones are never touched.
3. `fleet-cli-git-update.timer` runs the same service hourly, in case a signal is lost.

The workflow runs only when the repository variable `FLEET_CLI_SIGNAL_ENABLED` is `true`.  It reads three repository secrets, which Infisical (AFC `prod` `/fleet-cli`) holds as the source of truth:  `FLEET_CLI_UPDATE_SSH_KEY`, `FLEET_CLI_UPDATE_HOST` and `FLEET_CLI_UPDATE_KNOWN_HOSTS`.

## Linux Secret Bootstrap (AFC-427)

Code installation does not provision authentication.  The server's `fleet-secrets` entry point can use `scripts/fleet_secrets/linux-entrypoint.py`, installed beside the existing `~/.local/share/fleet-secrets/fleet-secrets` executable.  Point `~/.local/bin/fleet-secrets` at that entry point and make it executable.  It is an on-demand helper; no service or session restart is needed.

Provision `~/.config/fleet-secrets/bootstrap.json` through an authenticated, encrypted transport, with directory mode 0700 and file mode 0600.  Its only fields are `INFISICAL_AUTOMATION_CLIENT_ID` and `INFISICAL_AUTOMATION_CLIENT_SECRET`, obtained from the canonical secret manager.  Never include their values in source, arguments, logs, or a bundle.  The loader refuses symlinks, other owners, and broader file permissions.  Each invocation exchanges the machine credentials for a fresh access token in memory, then delegates to the regular CLI; credentials remain out of command arguments and output.  Infisical remains the source of all application secrets.  Revoke this bootstrap by rotating its machine credential in Infisical.

The wrappers for `agent-sync`, `board`, `recall`, and the CODEX stdio MCP do not bootstrap the token themselves:  `wrapper()` in `scripts/fleet-cli-release.py` execs the script with the caller's environment, so a direct Infisical consumer such as fleet knobs still needs `INFISICAL_TOKEN` in that environment.  Until the wrappers carry it, export a token before invoking those tools on the server.

Verify `fleet-secrets doctor`, `agent-sync --as CODEX whoami`, and `AGENT_SEAT=CODEX board whoami`.  Linear should identify the Codex app.  A server retaining the old cutover gate also needs `LINEAR_BOARD_WRITES=1` in its board wrapper.  App Store Connect's existing client reads `ASC_KEY_ID`, `ASC_ISSUER_ID`, and `ASC_KEY_P8` from Fleet-OPS prod `/asc`; a read of `/v1/apps?limit=1` verifies authentication without registering or releasing an app.

## Server Commands

```bash
python3 -I ~/.local/share/fleet-cli/repo/scripts/fleet-cli-git-update.py status
systemctl start fleet-cli-git-update        # update now
python3 -I ~/.local/share/fleet-cli/repo/scripts/fleet-cli-git-update.py rollback
journalctl -u fleet-cli-git-update -n 20
```

`rollback` returns to the previous good commit and skips the current one until main moves.  `python3 scripts/fleet-tools-doctor.py --seats CLAUDE,CODEX` reports the checkout against origin/main.

## Install (once)

```bash
python3 -I scripts/fleet-cli-git-update.py setup
install -m 644 scripts/systemd/fleet-cli-git-update.service scripts/systemd/fleet-cli-git-update.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now fleet-cli-git-update.timer
```

Then add the signal key to `/root/.ssh/authorized_keys` as `restrict,command="/usr/bin/systemctl start --no-block fleet-cli-git-update.service" ssh-ed25519 ...`.
