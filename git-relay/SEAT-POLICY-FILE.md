# Startup seat-policy file input

Existing `GIT_RELAY_SEATS_JSON` inline input is unchanged when no file setting is
present. For larger policies, set `GIT_RELAY_SEATS_FILE` to one absolute local
path and REMOVE `GIT_RELAY_SEATS_JSON` from the service environment. Both keys
being present, even if one is empty, is an error. There is no fallback or merge.

The file must be a regular file, not a symlink/FIFO/device, owned by the service
UID or root, mode0400 or0600, nonempty UTF-8 and at most4MiB. The descriptor is
opened no-follow/nonblocking/close-on-exec and verified before a bounded read.
The same SeatPolicy parser receives its exact text; actions, identities, tokens,
repositories, shared-auth verifier and App ceilings are not altered or synthesized.
This is startup-only input, not hot reload or a new secret manager.

Errors from file-backed configuration do not expose paths, bytes, seat names or
OS details. Inline diagnostics stay compatible. The file path is not a credential;
the policy file is private credential-bearing configuration and must not be shared.
Keep its directory service-controlled, prevent untrusted edits, and create it with
private permissions before a separately authorized runtime configuration switch.
Do not copy policy JSON back into an environment variable.

This source change does not write policy files, change the service environment or
unit, restart a service, widen any grants, trim seats, or deploy Git capabilities.
Runtime installation, exact policy-byte/semantic comparison, rollback custody,
independent artifact/configuration review and health proof remain separate gates.
Tests use only synthetic policies, including40seats exceeding Linux's per-string
environment limit. No private runtime policy was retrieved or committed.

## Dedicated systemd credential input (source prerequisite)

An alternative fixed selector is `GIT_RELAY_SEATS_CREDENTIAL=seat-policy`. It is
mutually exclusive with both file and inline inputs, including empty settings.
It reads only `seat-policy` inside the exact systemd directory
`/run/credentials/git-relay.service`. Directory descriptors walk each component
without symlinks; root ownership and non-writable ancestors are checked. The
credential directory must be root:root0550 and the nonlinked regular credential
root:root0440, at most4MiB. Their Linux access ACLs must name only root and the
current service UID (read/search on directory, read on file), with no group or
other access. Additional principals or an unavailable ACL fail closed. No
arbitrary credential name, directory or file is accepted. Ordinary file input
keeps its old strict0400/0600 validation. All paths/bytes/ACL data stay out of
configuration-error diagnostics.

The runtime mode bits0440/0550 reflect the systemd ACL mask; they do not by
themselves authorize the DynamicUser to read. This input checks the named-user
ACL as well as performing the actual read. A synthetic local ACL fixture is not
a production systemd/DynamicUser startup test. Such a startup must be qualified
before migration. This source does not add a unit drop-in, create a persistent
policy file, read private runtime bytes, restart a service, or deploy Git tools.

A separately reviewed migration would use a persistent root-only0600 backing
artifact in a root0700 directory and a dedicated LoadCredential binding. systemd
would copy it into a fresh startup namespace and assign the new DynamicUser ACL.
The old StateDirectory namespace or held descriptor is not durable backup.
Restoration must work with MainPID0 and after reboot: exact original policy,
unit/drop-in and code hashes in a root-only fsynced journal, same-filesystem
atomic restore, daemon reload/restart, independent health and old-policy checks.
This loader is only one prerequisite, not that recovery transaction. Never
activate it until private backup custody, supervised rollback, crash/reboot
fault tests and paired Worker activation/reconciliation have also been reviewed
and qualified. No runtime effect is authorized by this document.
