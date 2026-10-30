# Destination storage guard (source only, not enabled by this change)

The client machine's free space does not describe a remote Qdrant disk. A healthy
Qdrant collection does not prove write headroom either. The migration stopped
when a remote WAL allocation failed; a successful ingest report still needs
independent source/content readback before retention can be claimed.

`FLEET_RAG_STORAGE_GUARD` configures an opt-in guard in the Qdrant write client.
`FLEET_RAG_STORAGE_GUARD_REQUIRED=1` makes missing configuration a refusal.
Set required mode in the migration execution environment before restarting work.
A malformed configured policy is always a refusal, never an unguarded fallback.
Unconfigured legacy clients are unchanged and must not be described as protected.

Policy fields:

- `endpoint`, `collection`: exact client destination binding.
- `storage_path`, `device`, `mount`: qualified service storage path on the actual
  destination host, not the client's disk or another mount with more space.
  Configure canonical absolute POSIX paths exactly as returned by the host probe:
  no symlink aliases, trailing slash (except `/`), `.` or `..` components. Noncanonical
  spellings fail policy validation before any write. Resolve symlinks on the
  storage host during qualification, never with the client machine's `realpath`.
  A spelling mismatch stays a refusal rather than weakening the volume binding.
- `reserve_bytes`: explicit operator-approved emergency headroom.
- `write_allowance_bytes`: approved worst-case allowance for one request including
  WAL/index/compaction overhead, concurrent activity and probe-to-write delay.
- `max_request_bytes`: maximum serialized JSON request covered by that allowance.
  It cannot permit a request larger than `write_allowance_bytes`: the effective
  request cap is the smaller of the two values. JSON bytes are a size cap, not an
  estimate of total storage amplification.
- `probe_argv`: trusted program argument array, no shell, returning fresh evidence.
- `probe_timeout_seconds`, `max_age_seconds`: approved bounded probe timeout and
  allowed evidence age. No cached free-space measurement is used.

No byte reserve or amplification ratio is chosen automatically. Before activation,
the storage owner must qualify those values and the authenticated probe route.
`scripts/fleet-storage-probe.py` is a read-only Linux example to run on the
storage host. It checks the deepest actual mount and uses available blocks for
that path. Endpoint/collection labels alone are not service-mount proof: an owner
must first bind the service/container mount to that host path, device and mount.
A client-side invocation against an unrelated local mount is invalid evidence.

Each write HTTP attempt, including retries, checks the destination anew. This
covers upserts, payload changes and deletes, which can themselves allocate WAL.
A low, missing, malformed, future, stale, wrong-volume or failed probe stops before
the next HTTP attempt. No cleanup, restart, resize or spending occurs. Reads do
not run the probe. There is no standing host monitor.

This is an admission check, not an atomic disk reservation. Other writers can
consume space after the probe, and an accepted in-flight request cannot be
cancelled safely by this client. Reserve/allowance must account for that gap.
Server-side admission controls are needed for a hard global guarantee. Failures
leave prior writes possible; do not advance retained state or replay a mutation
blindly. Reconcile the exact failed batch and independently read content back.

This change does not protect unrelated backup/copy jobs or change their policy.
A file copy must separately verify its exact destination filesystem, known
remaining bytes plus reserve and bounded in-flight buffers, and recheck while
copying. Do not relocate or delete databases/backups to enable this guard.
