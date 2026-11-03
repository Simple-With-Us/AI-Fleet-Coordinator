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

## Doctor carry: `fleet-tools-doctor.py --vms` (AFC-597 slice S5, Mac side)

The puller is the doctor.  `scripts/fleet-tools-doctor.py --vms` reads the VMs from `~/.config/fleet-tools/vm-hosts.json` on the Mac, ssh's to each one, and writes each answer to `~/.local/state/fleet-tools/vms/<label>.json` (0600, atomically, the same writer as the server's report).  Muster's Virtual Machines panel reads those files and runs nothing.  A missing config is the feature off: no ssh runs and nothing is written.  Without `--vms` the doctor never touches a VM.

The config is one JSON object.  Each entry names a `label`, an ssh `destination` and optionally a `user`.

```json
{"vms": [{"label": "vm-name", "destination": "vm-name-ro", "user": "puller"}]}
```

Create it with `chmod 600`.  The label becomes a filename, so the doctor validates it strictly (letters, digits, dot, dash and underscore, starting with a letter or digit, at most 64 characters) and refuses an entry whose label could escape the folder.  The destination must be a plain alias or host and the user a plain login name, so a config value can never become an ssh option.

Per VM, the pull is one `ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes <user@destination>` with nothing sent: the forced command on the VM prints the fresh report whatever is asked, exactly as the server's doctor key does.  The answer is refused, and the previous report stays untouched, when the ssh fails or times out (60 seconds), when the answer is larger than 512 KiB, when it is not JSON, or when it is not a `fleet-vm-status` report at schemaVersion 1.  A refused or unreachable VM never fails the doctor run and never blocks the other VMs.  The old file then ages past Muster's 45-minute window and the VM reads as unknown, which is the staleness doing its job.

To have the pull ride the existing schedule, add `--vms` to the doctor run the LaunchAgent already makes with `--write`.  That plist change is an owner step and is not made by this slice.

Per-VM `authorized_keys` line (same shape as above, one key per VM, on the login whose home carries the collector checkout):

```
command="/usr/bin/python3 -I /home/<login>/.local/share/fleet-cli/repo/scripts/fleet-vm-status.py --json",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 <puller-public-key> fleet-vm-status-ro
```

The ssh alias the config's `destination` names must resolve to that key (an `~/.ssh/config` host stanza with its own `IdentityFile` per VM keeps the keys separate).  Muster never sees the alias, the key or the VM's address: it only ever reads the pulled files.

Revoke, in order: delete the VM's entry from `~/.config/fleet-tools/vm-hosts.json` (the doctor stops pulling at the next run), delete the VM's `authorized_keys` line (the key then matches nothing, and no other access was ever granted), and delete `~/.local/state/fleet-tools/vms/<label>.json` on the Mac so the panel stops showing it.  Deleting the whole config file turns the feature off for every VM at once.  Stopping the timer on the VM is separate and optional, as above.

## Non-goals

No lettering.  No wakes.  No control of anything on the VM.  No Zulip posting.  No prompts, tokens, argv values or environment values anywhere in the report.  The collector writes one file and changes nothing else.
