# Fixed scanner-only transaction preparation

This directory builds one pinned scanner replacement into a NEW private scratch
directory. It does not stage root files, change a service, read a credential,
connect to a host, install a policy or activate anything. No held packet is used
as a probe. Source review and CI are required before a separately qualified
immutable release may be staged and activated.

Inputs are exact baseline8762 and candidate8bd0 from scanner PR2235. The manifest
preserves the current scanner metadata0:0/0664 and service-config digest. Exactly
one target is changed. Eight finite transaction modules are hash-pinned to the
established source package. The only runner transform changes its single fixed
executor-unit string. Manifest and package digests are compiled into the fixed
root entry. Drift refuses, never substitutes current main or a historical runner.

The existing supervisor supplies exclusive locking, a no-active-handler freeze,
stopped-service guard, durable before-images and file journal, bounded systemd
execution, exact image/config checks, rollback and failed-exit recovery. Existing
transaction regression tests remain required. The package is not a new daemon.

## Own-seat runtime proof

The server has no existing canonical WISE bearer file. Historical transaction
entries name other seats and are not reused. The proof adapter uses the already
working, host-key-pinned server-to-Mac SSH route as jay. It invokes WISE's exact
immutable five-file client on that Mac, reads ONLY its existing own bearer there,
checks authenticated seat WISE, reads the expected repository/default branch,
then expects the exact default-branch refusal to a mutation-impossible null-OID
patch. Both expected-head and base-blob IDs are null. No credential is copied,
returned, logged, placed on argv, written to a manifest or bound on the server.
No persistent tunnel or extra runtime is introduced. The adapter has a fixed
command and one finite subprocess; unknown output, wrong seat/image or timeout
refuses the deployment proof. The server supervisor independently verifies the
actual process and exact new or rollback image. Healthz alone is not proof.

This is preparation, not a claim that runtime proof succeeded. The fixed adapter
must itself pass normal review and CI. Before activation, validate existing SSH
reachability and the exact proof result without weakening it, final immutable
source and all pins, live baseline/config/metadata, rollback and recovery. A busy
relay or failed proof aborts safely; do not force a restart or use another seat.
Keep the original refused source packet and CT packet frozen until their own
complete qualification. No alternate publication route is introduced.
