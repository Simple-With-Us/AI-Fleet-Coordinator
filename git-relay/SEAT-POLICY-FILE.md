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
A seat entry may also carry `"oauth_assert": true`:  the per-seat opt-in that
lets the Worker's hosted-OAuth assertion path (git_relay/assertion.py)
authenticate as that seat's identity.  Absent the flag the seat refuses
assertions exactly as before;  the flag is never rendered from profiles or
synthesized, and the renderer refuses any value but literal true.
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

### Adding one seat to a credential-backed unit

`seat_policy.py install-seat` also works on a unit that already reads its policy
through this credential input. It finds the one `LoadCredential=seat-policy:<file>`
backing file, refuses a credential setting next to the file or inline inputs, and
replaces the backing file under the same transaction as the file input: the lock,
the full-digest check, the additive-only result, the signed proof receipt against the
relay's own audit row, a backup, a health check and a restore on failure. The backing
file must be a private regular file (0400 or 0600, one link, no symlink, in a
directory nobody else can write). The restart makes systemd hand the new copy to the
service. This does not migrate a unit onto the credential input. That migration, its
journal and its rollback stay the separately reviewed step above, and no runtime effect
is authorized by this document.

### Migrating a file-backed unit onto the credential input

`deploy/seat_policy_migrate.py` (`plan`, `apply`, `recover`) is that separately reviewed
migration, as source. It does not run itself and no runtime effect is authorized by this
document. It needs the policy's full canonical digest (`--expect-digest`), a root 0700
journal directory and a root 0700 directory for the backing file. `EnvironmentFile=` overrides
`Environment=`, so a drop-in cannot unset the file setting; `apply` instead replaces the one
`GIT_RELAY_SEATS_FILE=` line in the unit's EnvironmentFile with
`GIT_RELAY_SEATS_CREDENTIAL=seat-policy`, byte-preserving every other line, and adds one
drop-in with `LoadCredential=seat-policy:<backing>`. The old policy file is left untouched.

Order and crashes. The backing file and the drop-in are written first (an extra credential next
to the file input is harmless), the config line last, so a restart or reboot at any phase boundary
still starts the relay. The tests restart the simulated unit after a hard crash at every phase.
Locking. The run holds install-seat's transaction lock on the old policy path and on the backing
path, plus its own journal lock, from the snapshot through the health check, and it restores if the
old policy file changes in that time. A root-only fsynced journal holds the before-images first; a
failed start, health check or served-copy digest check, or any interruption, puts them back.
`recover` needs only the journal, so it works after a crash, a reboot or with the service stopped.
Before it writes, recover checks every journal field it uses (unit, config path equal to the unit's
EnvironmentFile, the drop-in path, digests, integer owner and mode, a pre-migration config shape) and
refuses to roll back a finished migration if a later install-seat changed the backing policy. The
journal is protected only by being a root-only file in a 0700 directory: these checks catch corruption
and mismatches, not someone who can already write as root. A real reboot test on the host is a separate
released run. Adding a seat stays the proof-gated `install-seat`.


## Auth refusals a seat can name

A recognizable seat bearer whose identity is not a shared seat in the loaded policy is refused as `seat_not_in_policy`.  That names the loaded file, which can differ from the reviewed source in `deploy/seat-policy.json`.  Install the reviewed row with `git-relay/deploy/seat_policy.py`.  `install` needs `--unit`, and `--apply` needs the digest the dry run printed.  Adding one seat uses `install-seat` with `--unit`, `--seat`, `--row-json`, `--expect-before`, `--expect-after` and `--receipt`.  The installer restarts the unit.  `bearer_rejected` means the identity is in the loaded policy and the Worker did not confirm the bearer.  `verifier_unavailable` means the Worker could not be asked.  `no_app_credential` means Fleet-OPS prod `/github-apps` has no usable `GH_APP_<SLUG>_ID` or `GH_APP_<SLUG>_PRIVATE_KEY`.  For ZC those names are `GH_APP_SWU_ZC_ID` and `GH_APP_SWU_ZC_PRIVATE_KEY`.  Add the missing key and restart the relay, because an absent name is remembered for the process lifetime.  This document does not install a row or write a secret.
