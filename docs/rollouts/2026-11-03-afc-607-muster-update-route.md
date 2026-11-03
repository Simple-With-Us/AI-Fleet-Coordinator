# Muster Server Update Receipts

Muster's fleet-cli update request reached the restricted SSH broker as the ordinary server account.  The updater is a system service, even though its process runs as that account.  Calling systemctl directly therefore required administrator authorization; the previous helper discarded its diagnostic and returned only an exit status.

The repaired application route permits one fixed privileged command for the allowlisted fleet-cli update: `/usr/bin/sudo -n /usr/bin/systemctl start --no-block fleet-cli-git-update.service`.  Both planning and execution query the existing sudo policy for that exact argv.  The grant on the inspected host is broad OS permission, not a narrow sudoers rule.  The application narrows its use with an immutable action, executable and unit mapping; configuration cannot substitute another privileged update.  Other system controls receive no sudo fallback, and this change neither installs nor modifies sudoers.

Planning checks that the unit exists, is a oneshot service and has no active invocation or pending job.  Execution repeats the state and permission checks.  Failed submissions return fixed error codes derived from bounded private stderr; arbitrary diagnostic text never reaches the result or audit.

A successful submission returns `result: submitted`, `executionState: submitted` and `verified: false`.  Polling against a 12-second monotonic deadline can upgrade it to `result: done`, `executionState: completed` and `verified: true` only after a new InvocationID and new process timestamps identify a successful terminal invocation.  Old success, a busy service, a replaced invocation or unavailable status cannot count as completion.  A newly observed failed invocation returns a verified failure.  This receipt proves the service invocation result; it does not independently prove the checkout matches a later upstream commit.

Every update receipt also carries `baselineStartMonotonic`, the process-start timestamp read before submission.  When an invocation outlasts the initial window, Muster can recheck with `server update-status fleet-cli.<invocationID>.<baselineStartMonotonic> --plan --json`.  This typed route accepts only the fixed fleet-cli prefix, a 32-character lowercase hexadecimal ID and the captured canonical decimal UInt64 baseline, reads only the fixed unit status and never submits a command, queries sudo or writes an audit.  Its result reports submitted, done or failed with the same receipt fields and echoes the baseline; both the original ID and baseline must match in the client.  Rechecking applies the same strict newer-process-start requirement as the initial verifier, so stale timestamps cannot become success on a later read.  A missing, old or replaced ID cannot be substituted with a later run; if no ID was captured, the original outcome remains unverified.  Rechecking a receipt does not start another update or request another confirmation.

Coolify accepting a redeploy also returns submitted, because API acceptance does not prove deployment completion.

## Deployment Boundary

Ship the accompanying Muster result parser first so it understands submitted results and defensively classifies legacy update/redeploy success as submission.  Merge and review the helper separately.  The server CLI checkout update does not replace the protected broker installation automatically.

The existing reviewed `scripts/hetzner/install-resource-control.sh` installs the broker payload under `/usr/local/lib/fleet-resource-control` and requires root.  An authorized installer must run it from the reviewed commit; this rollout document is advisory and does not execute the installer, change keys or change privileges.  Verify installed source hashes and a read-only update plan before using Muster's existing confirmation flow for an actual update, then inspect the receipt and service state.

Validation: `python3 scripts/test_fleet_ops_control.py`; `python3 scripts/test_fleet_resource_control.py`; `python3 scripts/hetzner/test_install_resource_control.py`.

Refs AFC-607.
