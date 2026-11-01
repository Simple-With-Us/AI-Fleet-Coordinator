# Coding Memory Priority

The always-on `fleet-coding-memory.service` gives existing coding workloads a shared soft memory target without restarting or reparenting them.  The policy is `CODING_RAM_POLICY` in Infisical Fleet-OPS prod `/coding-memory`; no host inventory or credential values belong in this repository.

Normal operation lends coding workloads idle memory while retaining the configured host headroom.  Under memory pressure the combined `memory.high` targets are 8 GiB, dropping to 6 GiB during critical pressure.  Recovery requires sustained available memory and low pressure before borrowing resumes.  These thresholds throttle allocation and reclaim memory; they are not a hard aggregate cap or a guaranteed memory allocation.

Configured production containers with finite hard limits receive best-effort `memory.low` protection within an ancestor protection budget.  Coding roots include the configured user slice, native coding services, and shared coding containers; nested groups are counted once.  Docker scopes are rediscovered after recreation.  The controller leaves `memory.max`, swap limits and existing narrower limits intact.

## Host-local exclude list

A host can opt a unit out without an Infisical policy change via `/etc/fleet-coding-memory/exclude-units` (override with `--exclude-file`, mainly for tests). One systemd unit name per line; blank lines and lines starting with `#` are ignored, and names are validated as unit names (no `/` or whitespace). Example:

```text
# BotFleet harness manages its own heap; do not set memory.high on it.
botfleet-server.service
```

Trust rules: the file is used only if it is a regular file opened with `O_NOFOLLOW` (symlinks are rejected), owned by uid 0, and not group- or world-writable. Otherwise it is ignored entirely and one warning line goes to stderr per refresh. A missing file means an empty list and is silent.

The file is reloaded on the same cadence as the policy (startup, background refresh, SIGHUP). Excluded units are skipped when `coding_units` are resolved, and an excluded unit's cgroup is never used as a coding root when reached via another route (for example docker discovery or user-slice nesting); exact and descendant cgroups are filtered. Limitation: if a configured coding root is an ancestor of an excluded unit (for example a whole slice), the excluded unit still shares that ancestor's limit — the controller does not carve out nested exceptions.

Hand-back: if the controller previously wrote a control (`memory.high`/`memory.low`) on a cgroup that is now excluded, it restores the recorded baseline the same way it does for groups that disappear. If the current value differs from what the controller last wrote, the external edit wins: ownership is dropped without writing. Status JSON reports an `excluded_units` count.

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

Review the actual coding/production group counts, settings freshness, current usage, target and OOM deltas.  Status also counts production groups outside that supported protection contract.  A process lock prevents a second daemon or `--once` run from changing the same controls.  Status is read-only.  Do not create host memory pressure merely to test the controller; focused tests exercise pressure and recovery against synthetic cgroups.

## Roll Back

```sh
sudo systemctl disable --now fleet-coding-memory.service
```

Graceful shutdown restores only controls whose group inode and current value still match this controller's ownership records.  External edits and recreated scopes are preserved.  A crash retains the ownership journal for the next startup; do not delete the state directory to work around a failed restoration.

## Focused Tests

```sh
python3 -m unittest discover -s scripts/hetzner -p 'test_fleet_coding_memory.py' -v
```
