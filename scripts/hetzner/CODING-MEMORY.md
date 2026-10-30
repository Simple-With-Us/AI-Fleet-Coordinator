# Coding Memory Priority

The always-on `fleet-coding-memory.service` gives existing coding workloads a shared soft memory target without restarting or reparenting them.  The policy is `CODING_RAM_POLICY` in Infisical Fleet-OPS prod `/coding-memory`; no host inventory or credential values belong in this repository.

Normal operation lends coding workloads idle memory while retaining the configured host headroom.  Under memory pressure the combined `memory.high` targets are 8 GiB, dropping to 6 GiB during critical pressure.  Recovery requires sustained available memory and low pressure before borrowing resumes.  These thresholds throttle allocation and reclaim memory; they are not a hard aggregate cap or a guaranteed memory allocation.

Production containers receive best-effort `memory.low` protection within an ancestor protection budget.  Coding roots include the configured user slice, native coding services, and shared coding containers; nested groups are counted once.  Docker scopes are rediscovered after recreation.  The controller leaves `memory.max`, swap limits and existing narrower limits intact.

Settings load at startup and refresh on a background thread; SIGHUP requests a refresh.  A protected last-known-good cache keeps the last valid policy during settings outages, and status marks it stale.  The dedicated settings bridge requires its own read-only service token, scoped to prod `/coding-memory`, through systemd `LoadCredential`.  Infisical root `CODING_MEMORY_READ_TOKEN` is the token source of truth; its deployed binding is `/etc/fleet-coding-memory/read-token` (root-owned, mode 0600, parent 0700).  The installer copies code and nonsecret project metadata; it does not copy credentials.  The reader validates the returned secret folder and refuses ambient or bootstrap authentication.

## Install And Verify

Use a reviewed checkout on the intended host, with its dedicated scoped credential binding and nonsecret project metadata already provisioned:

```sh
sudo bash scripts/hetzner/install-coding-memory.sh --install-only
sudo systemd-analyze verify /etc/systemd/system/fleet-coding-memory.service
sudo systemctl enable --now fleet-coding-memory.service
sudo python3 /usr/local/lib/fleet-coding-memory/fleet_coding_memory.py --status
sudo systemctl is-active fleet-coding-memory.service
```

Review the actual coding/production group counts, settings freshness, current usage, target and OOM deltas.  A process lock prevents a second daemon or `--once` run from changing the same controls.  Status is read-only.  Do not create host memory pressure merely to test the controller; focused tests exercise pressure and recovery against synthetic cgroups.

## Roll Back

```sh
sudo systemctl disable --now fleet-coding-memory.service
```

Graceful shutdown restores only controls whose group inode and current value still match this controller's ownership records.  External edits and recreated scopes are preserved.  A crash retains the ownership journal for the next startup; do not delete the state directory to work around a failed restoration.

## Focused Tests

```sh
python3 -m unittest discover -s scripts/hetzner -p 'test_fleet_coding_memory.py' -v
```
