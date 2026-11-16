# Hetzner host desired state

Fleet GitOps G1 ([AFC-369](https://linear.app/simple-with-us/issue/AFC-369)) declares the Hetzner host files this repo can already source.  The manifest names systemd units, timers, and the one cron file whose bodies already live in this repo.  Check compares those bytes to a live root and exits non-zero on drift.  `--apply` copies the tracked files only.  It does not reload systemd, restart units, enable timers, or delete forbidden files.

The shape matches the Mac manifest in [AFC#1867](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/1867).  Operator, wake-delivery, eavesdropper, session-letter, message-routing, and Muster units are excluded and are not copied.

Secret values stay in Infisical.  This tree references service-token env file paths that the existing units already name.  It does not add values, host addresses, or scanner allowlist comments.

## Covered

| Id | Source | Installed path |
|---|---|---|
| `fleet-health-recover@.service` | `scripts/host/fleet-health-recover@.service` | `/etc/systemd/system/fleet-health-recover@.service` |
| `fleet-housekeep.service` | `scripts/host/fleet-housekeep.service` | `/etc/systemd/system/fleet-housekeep.service` |
| `fleet-housekeep.timer` | `scripts/host/fleet-housekeep.timer` | `/etc/systemd/system/fleet-housekeep.timer` |
| `fleet-tools-doctor.service` | `scripts/systemd/fleet-tools-doctor.service` | `/etc/systemd/system/fleet-tools-doctor.service` |
| `fleet-tools-doctor.timer` | `scripts/systemd/fleet-tools-doctor.timer` | `/etc/systemd/system/fleet-tools-doctor.timer` |
| `fleet-cli-git-update.service` | `scripts/systemd/fleet-cli-git-update.service` | `/etc/systemd/system/fleet-cli-git-update.service` |
| `fleet-cli-git-update.timer` | `scripts/systemd/fleet-cli-git-update.timer` | `/etc/systemd/system/fleet-cli-git-update.timer` |
| `cf-usage-watchdog.service` | `scripts/systemd/cf-usage-watchdog.service` | `/etc/systemd/system/cf-usage-watchdog.service` |
| `cf-usage-watchdog.timer` | `scripts/systemd/cf-usage-watchdog.timer` | `/etc/systemd/system/cf-usage-watchdog.timer` |
| `fleet-coding-memory.service` | `scripts/hetzner/fleet-coding-memory.service` | `/etc/systemd/system/fleet-coding-memory.service` |
| `fleet-rag-nightly.service` | `scripts/systemd/fleet-rag-nightly.service` | `/etc/systemd/system/fleet-rag-nightly.service` |
| `fleet-rag-nightly.timer` | `scripts/systemd/fleet-rag-nightly.timer` | `/etc/systemd/system/fleet-rag-nightly.timer` |
| `lane-placement-watch.service` | `scripts/systemd/lane-placement-watch.service` | `/etc/systemd/system/lane-placement-watch.service` |
| `lane-placement-watch.timer` | `scripts/systemd/lane-placement-watch.timer` | `/etc/systemd/system/lane-placement-watch.timer` |
| `fleet-qdrant` | `scripts/hetzner/fleet-qdrant.cron` | `/etc/cron.d/fleet-qdrant` |

`script_source` on three rows names a tracked script and is not installed.  The housekeep and health-recover scripts disagree between the unit `ExecStart` path and the compliance inventory, so neither script is copied.

## Excluded

Tracked files left out of desired state: `afc-wake-router.service`, `ag-wake-bridge.service`, `zc-wake-bridge.service`, `operator-sheets-drain.service`, `operator-sheets-drain.timer`, `fleet-server-status.service`, `fleet-server-status.timer`, `fleet-vm-status.service`, and `fleet-vm-status.timer`.

Notes in the manifest also keep out the eavesdropper, session letters, message routing, `codex-operator-coordination-check`, `afc-jet-operator-wake`, the Jet wake-bridge review template, the vendor `e2scrub_all` cron, and the retired root crontab watchdog.

## Still missing

`[[gap]]` rows are inventory items with no sourced body.  The reconciler does not invent those files.  The full notes are in `manifest.toml`.  The groups are:

- Units with no file in this repo: `fleet-deploy-guard@` service and timer, `code-main-sync` service and timer, `usage-monitor-replica-status` service and timer, `box-disk-hygiene` service and timer, `lane-auto-retire` service and timer, `ct-deploy-guard` service and timer, `ct-deploy-overlap.service`, `congress-health-recover.service`, `clutch-chat-bridge.service`, `claude-remote-control.service`, `codex-tmux-boot.service`, `opencode-server.service`, `shellular.service`.
- Drop-ins: `usage-monitor-replica-status.service.d`, and the `fleet-rag-nightly` User/Group drop-in the unit comment says lives on the host.
- Cron files: `fleet-backups`, `ct-litestream-l1-only`, `um-litestream-l1-only`, `ct-reattach-proxy`, `vps-storage-watch`, `coolify-maintenance`.
- Not rewritten: Qdrant retention knobs stay literals in the tracked cron, because no sourced template reads them from Infisical.  No WireGuard template is in this repo.  The `fleet-rag-nightly` env example is not the live env file.  Host service-token files stay on the host.
- Absence only: `fleet-backups.bak*` under `/etc/cron.d` is reported and is not deleted.

## Run

From an AFC checkout:

```bash
cd scripts && python3 -m unittest hetzner_gitops.tests.test_diff -v
python3 -I scripts/hetzner-gitops-reconcile.py --live-root /tmp/hetzner-root --json
```

Check with no `--live-root` reads `/` and writes nothing.  Apply refuses until `--live-root` is passed with `--apply`.  Do not point either flag at the real host from a cloud agent.

## Owner steps

1. Read the gap list and the excluded rows before anyone copies files onto the host.
2. On the host, after this commit is in the checkout that host already uses, run the check with `--live-root /` and read the drift.
3. Apply stays owner-gated.  It copies files only.  A daemon reload and any restart stay a separate decision under the GitOps plan.
4. Move the backup cron copies out of `/etc/cron.d` by hand if the forbid row reports them.  This tool will not delete them.
5. Add a unit to the manifest only after its body is already in git.  Do not add Operator, wake, eavesdropper, routing, session-letter, or Muster units.
