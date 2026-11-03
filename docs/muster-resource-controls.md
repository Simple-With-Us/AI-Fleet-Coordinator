# Muster Resource Controls

AFC-598 adds six controls for the shared coding workload pool to Muster's Server Resource Limits view.  The updated app, broker, scoped writer credential, and controller installation prerequisites below are not deployed yet; source changes and mock tests do not establish runtime readiness.

## Controls And Resource Semantics

The source of truth is the complete `CODING_RAM_POLICY` JSON value in Infisical, Fleet-OPS project, environment `prod`, folder `/coding-memory`.  Each edit changes one allowlisted field while preserving the rest of that policy.

| Control | Policy Field | Unit And Bounds | Behavior |
| --- | --- | --- | --- |
| CPU Cores | `cpu_cores` | Integer, at least 1 and at most online CPU count minus 2 | Selects the first N online CPU IDs, including noncontiguous IDs, as a shared CPU set for configured coding units and containers |
| CPU Weight | `cpu_weight` | Integer, 1 through 10000 | Sets relative scheduling priority on configured systemd coding units |
| Pressure RAM Budget | `budget_bytes` | Whole GiB in the editor; bytes in policy | Sets the shared soft RAM target under pressure and in normal mode when borrowing is disabled |
| Critical RAM Budget | `critical_budget_bytes` | Whole GiB; positive and no greater than the pressure budget | Sets the shared soft RAM target during critical pressure |
| Host RAM Reserve | `reserve_bytes` | Whole GiB; at least 2 GiB, with pressure budget plus reserve no greater than total host RAM | Preserves the headroom used by the controller's borrowing calculation and production protection |
| Borrow Idle Capacity | `allow_borrowing` | Boolean; defaults to true when absent | Allows a larger soft RAM target in normal mode while host headroom permits |

CPU cores are an affinity pool shared by coding workloads, rather than a separate allocation per workload.  CPU weight is relative priority when units compete.  Docker receives the CPU set; its existing CPU quota and CPU shares remain intact.

RAM controls write `memory.high` and the existing production `memory.low` protection.  They do not allocate physical RAM, set a hard memory cap, change swap limits, kill processes, or restart or reparent coding jobs.  In normal mode, borrowing can increase the soft target using current coding usage plus available host memory minus the reserve; pressure and critical modes return to their respective policy budgets.  Disabling borrowing keeps the normal soft target at the pressure budget.

An initial policy without CPU fields leaves CPU settings alone.  An absent borrowing field retains the existing true default.  Host-local exclusions and suspended controls continue to take precedence over controller writes.

## Desired Policy And Observed Receipt

Desired values describe the persisted policy.  Observed values come from current kernel CPU controls, per-group RAM high readback, memory usage, controller mode, and target counts; a saved value alone does not establish application.

The root controller atomically publishes a redacted schema-version-1 receipt at `/run/fleet-coding-resources/status.json`.  It contains the full-policy revision, generation time, six desired values, and safe observed numbers or summaries.  Container identities, unit inventory, cgroup paths, environment values, credentials, and private journal records are omitted.

| Status | Meaning |
| --- | --- |
| Applied | A fresh receipt matches the desired revision, configured CPU controls and RAM highs match actual controls, targets are present, and there are no write failures or suspended controls |
| Drift | A usable receipt shows actual CPU controls or RAM highs that differ from desired controls |
| Unavailable | Evidence is missing, stale, malformed, suspended, incomplete, or reports a control write failure; absent CPU intent is also unavailable |

The broker also checks receipt age, required fields, host bounds, and the expected policy revision.  `SavedButApplyUnverified` means the desired policy was saved but reload or observed application was not verified.  The saved policy stays visible separately from the observed result, and an uncertain write is not retried automatically.

## Review And Apply

1. Refresh the direct read-only resource status and use its policy revision for the edit.
2. Change one field and request a plan through the fixed server route.  The broker validates the complete policy and reports the old value, requested value, and expected revision.
3. Review the plan in Muster and complete the existing Touch ID confirmation.  Muster requests a fresh plan afterward and refuses to apply a changed plan.
4. The apply request carries the same expected revision.  The broker serializes its own writers, rereads policy immediately before its single Infisical PATCH, and records intent and outcome without credential values.
5. After persisted readback matches the new revision, the broker sends HUP to the existing controller and waits up to 15 seconds for an applied receipt with that revision.  HUP refreshes policy without restarting coding workloads.

Touch ID belongs to the app's confirmation flow.  The server independently enforces the fixed command grammar, allowlist, policy schema, host bounds, and expected revision.

## Installation Prerequisites: Not Deployed Yet

These are installation and verification requirements, rather than a claim that deployment has occurred:

- Install the updated Muster build and configure its fixed `fleet-ops-ctl` SSH alias with batch mode, strict host-key checking, and the dedicated control key.  Keep host addresses and installation inventory outside this repository.
- Run the reviewed `scripts/hetzner/install-resource-control.sh` as root on Linux with no arguments.  It installs the fixed broker payload under `/usr/local/lib/fleet-resource-control` as protected root-owned code, with mode-0755 directories and mode-0644 files.  Credentials, SSH keys, and runtime state remain untouched; the installer does not provision credentials or configure SSH.
- Install the reviewed operations helper, `fleet_resource_control.py`, and its Python dependencies as root-owned code that other users cannot replace; enable `server.resourcePolicy` in the updated allowlist.  The dedicated key must have a restricted, fixed `/usr/bin/python3 -I /usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-ssh` forced command and must not permit a shell, PTY, forwarding, or agent access.  Follow the existing [control-route installation contract](protocols/fleet-tool-parity.md#the-control-route-fleet-ops-ctl-owner-step); use the SSH endpoint where the host's SSH daemon enforces that forced command.
- Provision a dedicated Infisical service token that reads and updates only Fleet-OPS `prod:/coding-memory` and the existing `CODING_RAM_POLICY` value.  The broker requires its dedicated private mode-0600 credential and project metadata files; it does not use ambient credentials or endpoint overrides.  Record credential scope and expiry in the service-token inventory without its value.
- Keep the controller's existing scoped read-only systemd credential separate from the broker writer.  Install the reviewed `fleet_coding_memory.py` and sibling `coding_resources.py` together, retain the existing host exclusions and credential files, and provide the public receipt directory with mode 0755 and receipt mode 0644.
- Confirm the broker's fixed controller-HUP command is permitted in its installed execution context.  Confirm status and plans over the actual forced route, rejection of invalid verbs and targets, and an observed receipt with the expected revision before reporting the feature deployed.

Read-only status can project the controller receipt without a writer credential.  Planning or applying a policy change requires the dedicated scoped writer to read and validate the authoritative policy.

## Revisions, Concurrent Writers, And Rollback

A policy revision is SHA-256 over the canonical complete validated policy JSON: sorted keys, compact separators, and no inserted optional defaults.  Both broker and controller hash the same representation, so changing any policy field invalidates an older plan.

The broker lock serializes requests through this broker only.  This Infisical transport has no compare-and-swap operation; an external writer can still race the final revision read and PATCH.  The revision checks reduce stale-plan writes but do not make external writers transactional, and neither uncertain writes nor policy rollback run automatically.

To reverse a saved setting, review and apply the earlier value against a fresh revision.  Runtime ownership is separate: the private CPU journal records each field's baseline, last owned value, target identity, and pending write receipt.  Removing an optional CPU field restores only that field when its current value still matches the controller's last write; the other configured field remains owned.  An external edit wins and is preserved, including a narrower CPU set.

Graceful shutdown and removal or exclusion of a managed target use the same matching-value and cgroup-identity guards.  Partial writes are read back and journaled; an unsuccessful restoration retains only the still-owned field for a later safe retry.  Existing persistent systemd CPU properties are preserved, and no rollback changes CPU quotas, hard RAM limits, swap limits, or coding-job lifecycle.

## Verification

CI runs both controller suites, the offline installer suite, and the resource broker suite.  They use temporary files and mock command runners, rather than production workloads or live credentials.

```sh
python3 -m unittest discover -s scripts/hetzner -p 'test_fleet_coding_memory.py' -v
python3 -m unittest discover -s scripts/hetzner -p 'test_coding_resources.py' -v
python3 -m unittest discover -s scripts/hetzner -p 'test_install_resource_control.py' -v
python3 scripts/test_fleet_resource_control.py
```
