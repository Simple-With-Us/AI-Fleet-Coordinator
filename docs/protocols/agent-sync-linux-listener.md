# Linux Interactive Listener

`agent-sync daemon install` on Linux writes a systemd **user** unit at `~/.config/systemd/user/agent-sync-listener.service` and, only when absent, a private `~/.agent-sync/listener.toml`.  It preserves existing configuration and never calls systemctl, provisions credentials, or activates a listener.  The sample enables no seats.  Python defaults to `/usr/bin/python3` (Python 3.11 or newer); override with `--python /absolute/path/python3` and `--bin /absolute/path/agent-sync` when needed.

The host-local interactive listener uses `instance = "mac"`.  This legacy name identifies the interactive seat routing partition, not the operating system.  It is independent of the existing Coolify container's `server` partition and must not capture its cloud seats.  Do not expand the partition to enable this host.

The handoff authorizes host installation, hooks, starting the authenticated listener and delivery testing.  Provisioning a persistent scoped Infisical machine identity remains an explicit owner gate.  Linux automation-environment authentication requires AFC PR #703 or its landed equivalent; installation alone does not establish authentication.  The unit reads no credential file:  each seat resolves its credentials from the service environment first and then through `fleet-secrets` / Infisical, exactly as the macOS LaunchAgent does.  Use Infisical as the source of truth; never paste credentials into configuration, commands, or logs.

For a CLAUDE-only host, keep `[daemon] instance = "mac"` and add only:

```toml
[seat.CLAUDE]
bot = "Claude"
creds = "infisical"
wake = "inbox"
```

This captures authorized messages without enabling headless wakes.  Configure the intended wake adapter only with explicit owner authorization and after its probe, pin and status checks succeed.  Run `agent-sync daemon init` using the authorized identity to pin the owner, and verify the host's hook/session registration before expecting live delivery.  For live delivery, use the plugin from the installed runtime: `claude plugin install agent-sync --marketplace /root/.local/share/fleet-cli/current --scope user`.  This preserves unrelated user settings and hooks.  Existing sessions may need a normal session restart before loading newly installed plugin hooks; never interrupt an owner session.  Verify terminal rewake in isolated scratch state with two sessions, then set `[platform.claude-code] rewake_verified_cli = true` in the live config only after the receipt check passes.  Keep Desktop verification separate.

A successful installation or an active service does not prove Zulip authentication, native delivery, or wake receipt.

After configuration and approved authentication are ready, activate within the authorized handoff:

```sh
systemctl --user daemon-reload
systemctl --user enable --now agent-sync-listener.service
agent-sync daemon status --json
```

A user service requires the user's systemd manager.  Session-independent operation may require an owner-approved lingering policy; installation does not change it.  Logs use the user journal (`journalctl --user -u agent-sync-listener.service`).  Status reports the unit's installed and active state separately from the daemon lock and listener snapshot.  A missing user manager reports an unknown service state without exposing its diagnostics.

`agent-sync daemon pause` and `resume` retain their existing control-file semantics; they do not stop or start systemd.  `reload` signals the running daemon and does not refresh the service environment.  After changing the optional environment file, restart within the authorized handoff scope.

To remove the unit, first stop and disable it using `systemctl --user disable --now agent-sync-listener.service`, then run `agent-sync daemon uninstall` and `systemctl --user daemon-reload`.  Uninstall prints these instructions and removes the unit file only; listener state, configuration and any owner-provisioned environment file remain.  `--dry-run` on install or uninstall changes nothing.  macOS retains its existing launchd implementation; other platforms refuse installation.
