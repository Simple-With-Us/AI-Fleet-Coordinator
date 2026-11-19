# CLI Login Sync

The shared helper copies one explicitly supplied CLI credential into its approved
Infisical Fleet-OPS prod folder.  It uses a scoped service token, never a machine
identity, CLI login session or implicit credential-store lookup.  This source
contract does not establish that a host has the grant or installed payload.

## Scope And Inputs

The reviewed write allowlist accepts GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN
and COPILOT_GITHUB_TOKEN under `/cli-logins/<SEAT>`, or the corresponding
seat-suffixed names under `/cli-logins`.  Widening it requires a code change.
The destination folder must already exist; this helper never provisions folders.

The caller explicitly chooses one source:

| Source | Access |
|---|---|
| `--from env` (default) | The named variable already supplied to this process |
| `--from fleet-secrets --from-path PATH` | Compatibility spelling for one named direct shared-client read through its explicit scoped reader; no application subprocess |
| `--from file --file PATH` | The owner-provided regular input file with mode 0600 |

The source selection never falls back to another source.  In particular, a CLI's
private credential store is not read.  An input environment value must already
belong to this use case; a shared shell variable does not prove that ownership.

```
python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE
```

The destination requires its own `INFISICAL_WRITE_TOKEN`, with read/write access
to exactly the declared Fleet-OPS prod folder and a finite future expiry.
`INFISICAL_FLEET_OPS_PROJECT_ID` declares the destination project.  A separately
selected Infisical source read uses `INFISICAL_TOKEN` and its own exact grant;
neither binding substitutes for the other.  Values and tokens never enter argv,
the journal, output or logs.

## Durable Intent And Recovery

The shared CLI caller and login-sync caller explicitly choose the same protected
state root, `~/.local/state/fleet-infisical-writes`, unless an absolute
`--state-dir` is supplied.  The library itself chooses no state location.
Changing the state location is outside the existing history guarantee; do not use
it to bypass a held operation.

Each explicit attempt allocates a UUID and creation time.  Before dispatch the
shared writer durably records that identity and a typed target/value digest in
the existing protected NonceLedger, then fsyncs Sending before each mutation.
The provider/project/folder barrier spans names and purposes in this store.
The journal contains no credential value or bootstrap token.

An automated retry must retain and supply the **same** original UUID and epoch
milliseconds; allocating a new ID is a new explicit intent, not a global
exactly-once promise:

```
python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE --request-id ORIGINAL_UUID --request-created-at ORIGINAL_EPOCH_MS
```

A genuine synchronous HTTP 200/201 acknowledgment is persisted before readback.
Success additionally requires the exact scoped named value to match.  An accepted
write whose readback fails stays held; its original identity can reconcile
read-only when that acknowledgment and exact readback are both available.
HTTP 202 is not synchronous success.

```
python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE --request-id ORIGINAL_UUID --request-created-at ORIGINAL_EPOCH_MS --reconcile
```

Reconciliation reads no source value and never retries a mutation.  An absent or
proven pre-Sending intent receives a non-rearming cancellation tombstone.
Timeouts, resets and lost acknowledgments remain unknown; current-value equality
alone cannot settle them.  A fresh invocation is blocked behind an unknown
operation and reports the **original** bounded UUID/time/name/purpose for recovery.
Only a failed connection proven to precede every response, a definitive provider
refusal, or a preflight failure is definitely unsent.  Retryable refusal metadata
is retained, and the original ID stays terminal; a correction uses a new explicit
intent after the refusal is known.  There is no automatic replay or row release.

Success prints safe request identity plus the name/path confirmation.  Failure
prints a fixed outcome and recovery identity, never a value, value digest,
credential prefix or provider body.  A cached successful receipt describes a known
past completion and does not assert that external writers left the value unchanged.

## Storage And Deployment Boundaries

The retained ledger requires protected ancestors, a 0700 directory and 0600
single-link regular files without symlinks.  Durable initialization evidence makes
a missing database fail closed instead of silently resetting history.  Never delete
the ledger or its marker to clear an unknown result.  Protect and back up the
caller-owned durable state; loss of the entire state directory cannot be detected
from a new empty location.

Unsettled outcomes survive expiry and restart and are never evicted to make room.
Terminal identities retain their original 24-hour fence; the 1000-row capacity
fails closed.  Metadata, fingerprints and immutable expiry are validated before
pruning, and one UUID cannot bind a second target within retained history.

The guarantee covers cooperating callers sharing this state and lock.  It is not
provider CAS, cross-host serialization, protection against a different state
location, or a guarantee for direct external writers.  The independent
`fleet_knobs.setter` Infisical CLI writes and the Linear board's own REST writer
do not call `put_secret` and are not covered by this protocol.

Writer-capable standalone installs must ship the same shared ledger module and
inert package initializer with the shared client and CLI.  Read-only shared-client
installs never import SQLite, initialize writer state or require that payload.
Review, source CI, installed payload, existing own grants and runtime acceptance
remain separate gates; this document authorizes no credential or service change.
