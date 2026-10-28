# Fleet tools on the server, from git

Since 2026-10-19 the Hetzner server runs `simple-chat`, `simple-board`, `recall`, `lane` and `fleet-mode` straight from a git checkout of AI-Fleet-Coordinator main.  Git is the history, so no release copies are stored.  This replaces the Mac-pushed releases in `coolify-fleet-cli-sync.md` (owner 2026-10-19:  "use git for everything").

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

Code installation does not provision authentication.  The server's `fleet-secrets` entry point can use `scripts/fleet_secrets/linux-entrypoint.py`, installed beside the existing `~/.local/share/fleet-secrets/fleet-secrets` executable.  Point `~/.local/bin/fleet-secrets` at that entry point and make it executable.  Re-point the link after every `install-fleet-secrets.sh` run, because the installer restores the plain CLI link.  It is an on-demand helper; no service or session restart is needed.

Provide `INFISICAL_TOKEN` or the complete `INFISICAL_AUTOMATION_CLIENT_ID` and `INFISICAL_AUTOMATION_CLIENT_SECRET` pair through the trusted process environment.  An inherited token takes precedence; an incomplete pair fails with the missing variable names.  The entry point reads no credential file, keeps long-lived credentials confined to login, and passes only the short-lived token to direct consumers.  No credential value or masked preview appears in diagnostics.  Authentication runs from `/` with update checks disabled, a bounded lifetime and interruption cleanup.  Infisical remains the source of application secrets; provision and rotate the machine identity through its canonical secret manager.

The generated wrappers for `simple-chat`, `simple-board`, `recall`, and the CODEX stdio MCP exec with their inherited environment (`wrapper()` in `scripts/fleet-cli-release.py` does not load this entry point or inject a token), so provision approved authentication in that environment.  A direct Infisical consumer such as fleet knobs can exchange the complete automation pair through its fleet-secrets helper;  a consumer that requires a token must receive one through its approved launcher.  Token injection by separately installed server wrappers is unverified here;  a code update alone does not establish it.

Verify `fleet-secrets doctor`, `simple-chat --as CODEX whoami`, and `simple-board whoami` with the seat's bearer locator bound.  Linear should identify the Codex app.  A server needs no write switch:  the bearer-verified seat is the write gate.  App Store Connect's existing client reads `ASC_KEY_ID`, `ASC_ISSUER_ID`, and `ASC_KEY_P8` from Fleet-OPS prod `/asc`; a read of `/v1/apps?limit=1` verifies authentication without registering or releasing an app.

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
