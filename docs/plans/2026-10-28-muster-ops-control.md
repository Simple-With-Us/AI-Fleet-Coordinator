# Muster Operations:  Live Data, Knob Writes And Process Control

Owner ask (2026-10-28):  advance Muster, expand what it shows, and let it control knobs and start, stop and update processes where that is safe.

## Gaps this closes

1. The Server Status panel (AFC-548) never gets data on the Mac:  nothing schedules `fleet-server-status.py` and nothing carries its report to the Mac.
2. `fleet-worker-health.py` writes a file "for Muster" that nothing schedules and Muster never reads.
3. Muster cannot see the server's disk, memory or load, the state of the fleet's systemd units, or which Mac LaunchAgents are loaded.
4. The Controls tab is read-only, and nothing in Muster can start, stop, restart or update a process.

## Pieces

### A.  Server data pipeline (Python)

- `scripts/fleet-server-status.py` gains:
  - `hostMetrics`:  `{ "loadAverage": [1m, 5m, 15m], "cpuCount": int, "memory": { "totalBytes", "availableBytes" }, "swap": { "totalBytes", "freeBytes" }, "uptimeSeconds": int, "disks": [ { "mount", "totalBytes", "usedBytes", "freeBytes" } ] }` from `/proc/loadavg`, `/proc/meminfo`, `/proc/uptime` and `os.statvfs` over `/` and every mount under `/mnt` (real file systems only).
  - `services`:  `[ { "unit", "scope": "system"|"user", "description", "activeState", "subState", "since", "enabled", "controllable": bool } ]` for the units named in `scripts/fleet-ops-control.json` (`server.units`), read with `systemctl show --property=...` (never `status`), one call per scope.
  - `filesSummary`:  `{ "found": int, "limit": int, "truncated": bool }`.
  - `README.md` and `package.json` changes report drift `"info"` instead of `"changed"`.
  - `schemaVersion` stays 1;  every new key is optional, so the shipped Muster reader keeps working.
- `scripts/systemd/fleet-server-status.service` + `.timer`:  a user unit for `jay`, the account the read-only doctor runs as (`fleet-tools-ro` authenticates as root and then executes `runuser -u jay --`), with `loginctl enable-linger jay`, every 15 minutes, `Persistent=true`, inside the 45-minute freshness window.  The separate `fleet-ops-ctl` key is the root-only control route.
- `scripts/fleet-tools-doctor.py`:
  - On the server with `--json`, the document carries `serverStatus` (the parsed `server-status.json`, only when it is valid JSON with `tool == "fleet-server-status"`, and at most 512 KiB), so the existing read-only forced-command key carries it without a new key.
  - On the Mac with `--remote` and `--write`, a valid `serverStatus` is written to `~/.local/state/fleet-tools/server-status.json` (0600, atomic, through the same `write_report` helper) and removed from the combined report.
  - `MAX_REMOTE_BYTES` grows to fit.

### B.  Mac collectors (Python + LaunchAgents)

- `scripts/launchd/com.jay.fleet-worker-health.plist`:  runs `fleet-worker-health.py` every 900 seconds.
- `scripts/fleet-mac-services.py` writes `~/.local/state/fleet-tools/mac-services.json`:
  `{ "schemaVersion": 1, "tool": "fleet-mac-services", "generatedAt", "host": "mac", "agents": [ { "label", "plistInRepo": bool, "installed": bool, "loaded": bool, "pid": int|null, "lastExitStatus": int|null, "controllable": bool } ] }`.
  "Expected" is every `scripts/launchd/*.plist` label plus `mac.agents` in `scripts/fleet-ops-control.json`.  Loaded state comes from `launchctl print gui/<uid>/<label>` (the `state`, `pid` and `last exit code` lines) and installed from `~/Library/LaunchAgents/<label>.plist`.
- `scripts/launchd/com.jay.fleet-mac-services.plist`:  every 300 seconds.

### C.  Control helpers (Python)

`scripts/fleet-ops-control.json` is the one allowlist.  Nothing outside it can be touched.

```json
{
  "schemaVersion": 1,
  "mac":    { "agents": [ { "label": "com.jay.fleet-tools-doctor", "actions": ["start", "stop", "restart"] } ] },
  "server": { "units":  [ { "unit": "fleet-server-status.timer", "scope": "user", "actions": ["start", "stop", "restart"] } ],
              "updates": [ { "name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system" } ],
              "coolify": { "redeploy": true } }
}
```

`scripts/fleet-ops-control.py`:

- Verbs:  `mac start|stop|restart LABEL`, `server start|stop|restart UNIT`, `server update NAME`, `server redeploy RESOURCE_UUID`.
- Every verb takes `--plan` (prints what it would run, as JSON, and changes nothing) or `--yes` (runs it).  Without either it refuses.
- `--json` output:  `{ "ok": bool, "verb", "target", "commands": [[argv...]], "result": "planned"|"done"|"refused"|"failed", "detail" }`.
- Mac:  `launchctl kickstart [-k] gui/<uid>/<label>`, `launchctl bootout gui/<uid>/<label>`, and `launchctl bootstrap gui/<uid> <plist>` when the agent is not loaded.
- Server:  `systemctl [--user] start|stop|restart <unit>` and `systemctl start <update unit>`.  `redeploy` checks the uuid against the latest `server-status.json` Coolify list, then calls Coolify's `GET /api/v1/deploy?uuid=<uuid>` with the Coolify token from `fleet-secrets` (`Fleet-OPS /coolify COOLIFY_API_TOKEN`), read into memory and never printed.  No Coolify uuid or host is committed:  the uuid comes from the live report.
- The Mac reaches the server verbs through `ssh <alias> -- <verb ...>` over a second forced-command key (`fleet-ops-ctl`), whose forced command is `fleet-ops-control.py --from-ssh`.  That mode reads `SSH_ORIGINAL_COMMAND`, splits it with `shlex`, accepts only the `server` verbs, and re-checks the allowlist.  The key-install steps are in `docs/protocols/fleet-tool-parity.md`.
- Every `--yes` run appends one JSON line to `~/.local/state/fleet-tools/ops-audit.jsonl` (0600):  time, host, verb, target, result.  No output from the commands themselves is recorded.

`python3 -m fleet_knobs set KEY VALUE [--plan|--yes] [--json]` (in `scripts/fleet_knobs`):

- Refuses an unknown key, any `secret=True` key, and any value that the knob's `Spec` rejects (kind, choices, range, pattern).
- Writes through the Infisical CLI with a write-capable service token:  `infisical secrets set KEY=VALUE --env prod --path <knob folder> --projectId <id>`, with the token passed as `INFISICAL_TOKEN` in the child's environment only.  The token is read from the Mac keychain item `fleet-knobs-write` (service `fleet-knobs-write`, account `INFISICAL_TOKEN`) or from `FLEET_KNOBS_WRITE_TOKEN`.  It is never printed or written.  If neither is present, the write is refused with `"no write token"`.
- After a write it refreshes the last-known-good copy, so Muster shows the new value at once, and appends an audit row to `~/.agent-sync/listener/knobs.jsonl` (key, old and new value for non-secret knobs, time, seat).

### D.  Muster (SwiftUI)

- **Server Status panel:**  a Host card (load against the cpu count, memory and swap bars, a disk bar per mount, uptime), a Services card (unit, state, since, with Start, Stop and Restart buttons when the unit is `controllable`), a Coolify Redeploy button on each application row whose uuid came from a Coolify label (`uuidSource: "label"`;  services and databases cannot be redeployed yet), and a "partial list" note when `filesSummary.truncated`.
- **Worker Health card:**  in the Fleet Hosts tab, read from `worker-health.json`, with the same 45-minute freshness rule as the collector.
- **Operations tab:**  Mac LaunchAgents from `mac-services.json` (expected against loaded, PID and last exit, with Start, Stop and Restart), server updates (Run update), and the audit log's last 50 rows.
- **Controls tab:**  an Edit button on each non-secret knob opens a sheet with a type-aware editor (toggle, picker, stepper, or text with the pattern).  Save runs `fleet_knobs set --plan`, shows the plan, then asks for Touch ID, then runs `--yes`, and reloads.
- **Every action** runs `--plan` first, shows the plan in a confirmation sheet, asks for Touch ID (`LAContext.evaluatePolicy(.deviceOwnerAuthentication)`), then runs `--yes`, then re-reads the data.  Only one action runs at a time.  The app still holds no credentials.

## Owner steps

- Create the write-capable Infisical service token for the knob folder and store it in the Mac keychain as `fleet-knobs-write`.  Record it in the Infisical Service Tokens registry.
- Install the `fleet-ops-ctl` forced-command key on the server (steps in `fleet-tool-parity.md`).
- Load the two new LaunchAgents and enable the server timer (`systemctl --user enable --now fleet-server-status.timer`).

## Tests

- Python:  `python3 scripts/test_fleet_server_status.py`, `python3 scripts/test_fleet_tools_doctor.py`, `python3 scripts/test_fleet_mac_services.py`, `python3 scripts/test_fleet_ops_control.py`, `cd scripts && python3 -m unittest discover -s fleet_knobs/tests -t .`.
- Swift:  new decoding fixtures and model tests in `muster/MusterTests`, run by the `muster-macos-build` CI job.
