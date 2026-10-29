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
