# Fleet VM status (AFC-597 slice S4, collector half)

`scripts/fleet-vm-status.py` writes one private JSON report per fleet VM, in the spirit of `scripts/fleet-server-status.py` on the server.  Muster reads the report and treats anything older than 45 minutes as unknown, so the timer below refreshes it every 15 minutes.  This slice builds the collector and the units.  The pull transport is a forced-command SSH key per VM, installed by the owner per VM later.  Nothing here is installed on any VM by this change.

## What the report carries

One JSON document: `schemaVersion`, `tool`, `generatedAt`, `hostLabel`, `seats`, `uptimeSec`, `load1`, `load5`, `load15`, `cpuCount`, `memory`, `swap`, `disks`, `processes`, `harnessSessions` and `checkouts`.  A measurement that cannot be taken is omitted, never reported as a zero.  A section that was measured and found empty is an empty list, which is a different claim.

- `hostLabel` comes from `FLEET_VM_HOST_LABEL`, else the first line of `~/.config/fleet-tools/vm-host-label`.  A raw IP address is refused as a label.  Without either source the label is omitted.
- `seats` comes from `FLEET_VM_SEATS` as a comma list.  It is omitted when the variable is unset or empty.
- `disks` covers `/` and the code root.  Each row carries `mount`, `totalBytes`, `usedBytes` and `freeBytes`.
- `processes` groups allowlisted executable basenames only: `muse`, `mcode`, `minimax`, `grok`, `codex`, `claude`, `opencode` and `node`.  Each row carries `name`, `count` and, when the boot time is readable, `oldestStartedAt`.  A process is identified by `/proc/<pid>/exe` and `/proc/<pid>/comm`.  Its argv and its environment are never read.
- `harnessSessions` counts session files (a `.jsonl` name, or a name containing `session`) under `~/.local/share/muse/sessions`, `~/.claude`, `~/.codex` and `~/.grok`, with the newest mtime per harness.  Files are `stat`-ed, never opened, and symlinks are not followed.  A harness folder that is not there is omitted.
- `checkouts` covers each git repo directly under the code root (`FLEET_VM_CODE_ROOT`, else `~/Code` when it exists).  Each row carries `name`, `branch`, `headShort`, `dirtyTracked`, `dirtyUntracked` and `unpushed`.  Git reads are local plumbing only (`rev-parse`, `status`, `rev-list`, `branch -r --contains`, with `--no-optional-locks`).  Nothing is fetched.

## Install on one VM (owner, per VM)

Run these as the login whose home carries the harness folders and the checkouts.  The fleet CLI checkout is assumed at `~/.local/share/fleet-cli/repo`, matching the server unit.

```sh
mkdir -p ~/.config/systemd/user ~/.config/fleet-tools
cp ~/.local/share/fleet-cli/repo/scripts/systemd/fleet-vm-status.service ~/.config/systemd/user/
cp ~/.local/share/fleet-cli/repo/scripts/systemd/fleet-vm-status.timer ~/.config/systemd/user/
printf 'FLEET_VM_HOST_LABEL=<vm-label>\nFLEET_VM_SEATS=<SEAT,SEAT>\n' > ~/.config/fleet-tools/vm-status.env
chmod 600 ~/.config/fleet-tools/vm-status.env
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now fleet-vm-status.timer
python3 ~/.local/share/fleet-cli/repo/scripts/fleet-vm-status.py --json | head -20
```

Linger is required.  Without it the user manager and this timer stop when the last session closes, and the report ages past the 45-minute window.  The written report lands at `~/.local/state/fleet-tools/vm-status.json` with mode 0600, written atomically.

## Pull transport: a forced-command key per VM

The pull side never gets a shell.  Add one line per VM to that login's `~/.ssh/authorized_keys`, with the puller's public key.  The forced command ignores whatever command the client asks for and prints a fresh report.

```
command="/usr/bin/python3 -I $HOME/.local/share/fleet-cli/repo/scripts/fleet-vm-status.py --json",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 <puller-public-key> fleet-vm-status-ro
```

`$HOME` above is a placeholder in prose: write the login's absolute home path in the real line, since `authorized_keys` does not expand it.  A distinct key per VM keeps one VM's revoke from touching another.  The systemd env file is not read by the forced command.  When the pull path is the primary reader, use the alias file for the label (`~/.config/fleet-tools/vm-host-label`), add `--code-root` to the forced command when the checkouts are not under `~/Code`, and either accept that `seats` is omitted on pull or set it with an `environment="FLEET_VM_SEATS=..."` option on the same `authorized_keys` line.  That option only takes effect when the VM's sshd allows it (`PermitUserEnvironment yes`), which is the owner's call per VM.

Revoke by deleting that one `authorized_keys` line.  That is the whole revoke: the key then matches nothing, and no other access was ever granted.  Stopping the local timer is separate and optional (`systemctl --user disable --now fleet-vm-status.timer`).

## Non-goals

No lettering.  No wakes.  No control of anything on the VM.  No Zulip posting.  No prompts, tokens, argv values or environment values anywhere in the report.  The collector writes one file and changes nothing else.
