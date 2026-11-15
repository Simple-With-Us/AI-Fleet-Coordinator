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

## Own-seat runtime proof is not qualified

The previous proposed adapter embedded private operator inventory and used a local
credential-file read. Those routes are removed from the current source. No proof
was executed and no credential content was published. Historical commits remain.

`wise_runtime_proof.py` is now an explicitly unqualified, I/O-free, fail-closed
placeholder: every proof returns false without starting a transport or reading a
credential. This package cannot qualify a deployment. The fixed entry checks QUALIFIED before
constructing Runner, so every operation refuses before recovery arming, freeze,
stop, candidate application or service start. Missing proof cannot begin a transaction.

Restoring an executable adapter requires independent destination pinning from
private inventory, a verified own-seat scoped secret loader, bounded transport,
and review plus tests. No arbitrary environment-selected destination, new binding,
credential copy, daemon or tunnel is a substitute. No runtime activation is ready.

The pure builder independently pins the proof module bytes as well as the eight
established transaction modules. Scratch directories must be owner-private actual
directories; symlink components and broad permissions refuse. Existing outputs
are never overwritten.
