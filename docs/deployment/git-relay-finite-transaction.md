# Existing git-relay finite deployment transaction

Preparation code only. No installer, daemon, new runtime service or production
activation is included. This PR does not stage files, change policy or restart
a service. Existing owner deployment authority is not supplied by this document.

The intended one-operation root runner has pinned root-owned source/manifest,
strict old/new file hashes and exact metadata, an exclusive flock, finite
systemd timeouts and ExecStopPost recovery. A persistent existing-service
ExecStartPre guard blocks mixed images and uncommitted starts after executor
loss or reboot. The supervisor freezes the exact boot/PID/start/executable/
cgroup process, proves one stopped task with no children or pending operation,
then stops it. The default SIGTERM must be queued and observed pending before
systemd can resume it, so no new request handler can start in that interval.
Busy freezes resume unchanged without stopping active provider handlers.

Only confirmed stopped files are atomically replaced and directory-fsynced.
Each file has durable before/after images; recovery prevalidates the complete
inventory and removes only exact manifest-created files. Runtime qualification
requires a replacement PID, all hashes, health, authenticated own-App repository
read and a new-action default-branch refusal that cannot write to the provider.
The old image has its own expected unknown-action refusal. File-journal commit
is reconciled if the process dies before the supervisor-journal commit.

The included tests cover real file/journal composition, crash after every file,
identity/boot/deadline drift, busy handler refusal, rollback/proof failure,
repeated recovery, unsafe paths and a disposable Linux process demonstrating
pending default SIGTERM prevents userspace after SIGCONT.

Before any production activation, independently qualify the fixed entry point,
actual systemd-property rendering, package and manifest digest binding, full
crash ordering (including partial prepare and guard installation), policy
append-only derivation from the actual live inventory, and both runtime probes.
A generated local entry point is explicitly marked UNQUALIFIED_NOT_INSTALLED
until these checks and normal review gates pass. Test doubles are not production
qualification. Never overwrite the live 41-seat policy with a canonical smaller
inventory; preserve every non-action value and original action ordering.

## Interrupted-operation cleanup

Use the same reviewed, root-owned fixed entry point with `recover` after a failed
or interrupted bounded operation. It detects an existing journal, reconciles a
committed file journal, verifies inventory, and removes only its exact guard.
Qualified cleanup is repeatable even if the guard was already durably removed.
An unchanged old image is resumed/verified without stopping active handlers.
An uncommitted changed image must pass the stopped-request-free qualification
before rollback. Drift, busy handlers, missing backups or a different guard
refuse destructive cleanup and leave the guard armed for diagnosis. Do not
remove a guard by hand or start a new transaction over an existing journal.

The compiled manifest pins destination code/policy and entry path, not an entry
hash: embedding the manifest digest in an entry while embedding that entry hash
in the manifest creates an impossible circular hash. Instead the independent
release record pins the final generated entry and package hashes; all are under
root-only anchors. The runner captures its regular root-owned entry bytes at
startup and checks the same hash before arming. No caller supplies a digest.
