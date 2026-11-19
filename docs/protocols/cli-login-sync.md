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

The seat is in the path, so the name is the same on every host and the consumer's
command line is the same everywhere.  A consumer whose own table of secrets is static
— BotFleet's secret map names one secret per bot — reads the other shape, one folder
with the seat in the name (`GH_TOKEN_OPENCODE`), and renames the value to the
environment variable the CLI expects.

## The write

> **Status (Tue, Nov 17, 2026).**  The sync script lived in the retired reader package
> (`scripts/fleet_secrets/sync_cli_login.py`) and is gone with it.  Nothing in this
> repository replaces it today, so a `/cli-logins/<SEAT>` folder is written by hand through
> the Infisical CLI or the API, with a service token scoped to exactly that one folder and
> write permission.  The rules below still hold for whoever writes it.

The source of the value is one of:

| Source | Reads |
|---|---|
| the environment | the environment variable.  This is what the Mac's own shell already exports for the GitHub CLI |
| Infisical | the value, read through the consumer's own scoped service token |
| a file | a 0600 file the owner handed over, which is how the owner hands a secret to a seat at all |

The GitHub CLI's own keychain entry and a generic keychain lookup are deliberately not
sources here.  The keychain is reserved for the consumer's own scoped service token,  and a
path-less lookup can reach an item in another account or in the System keychain and can
raise an unlock prompt on the owner's screen.  The approved order is the environment first
and a scoped Infisical read second.

The value never appears in an argument list, a message or a log.  The write checks the
policy before it reads the value, so a name or a path outside the allowlist never reaches
a child process and never touches the network.  Then it reads the source, writes it, reads
it back, compares the digests, and prints the name and the path:

```
python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE --request-id ORIGINAL_UUID --request-created-at ORIGINAL_EPOCH_MS
```

A genuine synchronous HTTP 200/201 acknowledgment is persisted before readback.
Success additionally requires the exact scoped named value to match.  An accepted
write whose readback fails stays held; its original identity can reconcile
read-only when that acknowledgment and exact readback are both available.
HTTP 202 is not synchronous success.

```
GH_TOKEN="<the scoped read>" gh api user
```

The scoped read resolves the value into the environment variable that CLI already reads,
so the CLI prefers it over whatever it has in its own store, and the value never lands in
an argument list.  The reader is the consumer's own scoped service token through
`scripts/fleet_infisical.py`.  The server's fleet tools already read Fleet-OPS prod, so
this is the same read they already do.

Success prints safe request identity plus the name/path confirmation.  Failure
prints a fixed outcome and recovery identity, never a value, value digest,
credential prefix or provider body.  A cached successful receipt describes a known
past completion and does not assert that external writers left the value unchanged.

* No credential file is copied between hosts.  The Mac does not push a file to the
  server, and the server does not pull one from the Mac.
* The GitHub CLI's own keychain entry is not a source,  and neither is a path-less
  keychain lookup.  The keychain is for the consumer's own scoped service token.
* No value goes in the repository, a log, a pull request or a chat message.  The
  repository carries names and paths only.
* The GitHub CLI's own token stays in the GitHub CLI's own store on the machine where
  it was made.  The sync copies the value into the shared store; it does not take the
  keychain entry away.

## The writer token and the reader token

The write needs a service token scoped to exactly that one `/cli-logins/<SEAT>` folder
with write permission, and the read needs a read-only token for the same folder.  Both
are scoped service tokens (`service-tokens`), one per consumer, stored in that
consumer's own store:  the owner's login keychain on the Mac, a 0600 file inside a 0700
directory on a server.  There is no machine identity and no shared reader any more.
The local VM reads the same folder once it holds a token for it; until it does, the local
VM's consumers need the value passed in by whoever runs them.

The guarantee covers cooperating callers sharing this state and lock.  It is not
provider CAS, cross-host serialization, protection against a different state
location, or a guarantee for direct external writers.  The independent
`fleet_knobs.setter` Infisical CLI writes and the Linear board's own REST writer
do not call `put_secret` and are not covered by this protocol.

A CLI login that appears in a transcript, a log or a chat message is exposed, and it
has to be rotated.  That is the reason the write prints a name and a path, and the
reason the extractors read from a store rather than from an argument.
