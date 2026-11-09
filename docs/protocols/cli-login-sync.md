# CLI login sync

A seat's CLI login lives in that CLI's own store on the machine where the seat works:
the GitHub CLI keeps an OAuth token in its keychain, the Copilot CLI keeps one in the
system credential store, and so on.  A seat that also runs somewhere else — a bot on
the shared VPS, a helper on the local VM — needs the same login there, and copying
credential files between hosts is the thing the fleet does not do.

So the login goes where every host already reads from: Infisical, Fleet-OPS, prod,
under `/cli-logins/<seat>`.

## The path

One folder per seat, `/cli-logins/<SEAT>`, and inside it the names the CLIs actually
read out of their environment:

| Name | Read by |
|---|---|
| `GH_TOKEN` | the GitHub CLI, and anything else that speaks to GitHub as the owner |
| `GITHUB_TOKEN` | the same, for a tool that looks for that name |
| `GH_ENTERPRISE_TOKEN` | the GitHub CLI against a GitHub Enterprise host |
| `COPILOT_GITHUB_TOKEN` | the Copilot CLI |

The seat is in the path, so the name is the same on every host and the consumer's
command line is the same everywhere.  A consumer whose own table of secrets is static
— BotFleet's secret map names one secret per bot — reads the other shape, one folder
with the seat in the name (`GH_TOKEN_OPENCODE`), and renames the value to the
environment variable the CLI expects.

## The write

From the machine where the seat works, on demand:

```
python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE
```

The source is one of:

| `--from` | Reads |
|---|---|
| `env` | the environment variable `--name`.  This is what `fleet-secrets run --only` injects, and it is what the Mac's own shell already exports for the GitHub CLI |
| `fleet-secrets` | the value, read with `fleet-secrets get NAME --path <from-path>` |
| `file` | a 0600 file the owner handed over, which is how the owner hands a secret to a seat at all |

The GitHub CLI's own keychain entry and a generic keychain lookup are deliberately not
sources here.  The keychain is reserved for the machine identity,  and a path-less
lookup can reach an item in another account or in the System keychain and can raise an
unlock prompt on the owner's screen.  The approved order is the environment first and
`fleet-secrets get` second.

The value never appears in an argument list, a message or a log.  The command checks
the policy before it reads the value, so a name or a path outside the allowlist never
reaches a child process and never touches the network.  Then it reads the source,
writes it, reads it back, compares the digests, and prints the name and the path:

```
synced GH_TOKEN at /cli-logins/OPENCODE
```

The token is resolved once and used for the folder, the write and the read-back, so a
sync does not log in three times.  The folder is created on demand, and the API's
"already exists" answer is read rather than assumed, so a rejected request is not
mistaken for a folder that is there.

The write is a reviewed class and not a general write.  The path names the seat, the
name is one of the four above, and the allowlist is code, so widening it takes a
reviewed change — the same rule the minted Linear token class follows.  The folder is
created on demand, because an empty folder is not a secret and no seat should need a
human to make one before its first sync.

## The read

On the host that needs the login, the consumer runs the CLI with the credential in its
environment:

```
fleet-secrets run --path /cli-logins/OPENCODE --only GH_TOKEN -- gh api user
```

`run` injects the value as the environment variable that CLI already reads, so the CLI
prefers it over whatever it has in its own store, and the value never lands in an
argument list.  The server's fleet tools already read Fleet-OPS prod, so this is the
same read they already do.

## What is not in this

* No credential file is copied between hosts.  The Mac does not push a file to the
  server, and the server does not pull one from the Mac.
* The GitHub CLI's own keychain entry is not a source,  and neither is a path-less
  keychain lookup.  The keychain is for the machine identity.
* No value goes in the repository, a log, a pull request or a chat message.  The
  repository carries names and paths only.
* The GitHub CLI's own token stays in the GitHub CLI's own store on the machine where
  it was made.  The sync copies the value into the shared store; it does not take the
  keychain entry away.

## The owner-only identity

The write needs a machine identity on the machine where the seat works, and the
read needs one on the host that consumes it.  The Mac has one for Fleet-OPS, and so
does the shared VPS, which is why the same store is visible from both.  The local VM
reads the same store once it has an identity for it; until it does, the local VM's
consumers need the value passed in by whoever runs them.

## A token that has been printed is a token to rotate

A CLI login that appears in a transcript, a log or a chat message is exposed, and it
has to be rotated.  That is the reason the command prints a name and a path, and the
reason the extractors read from a store rather than from an argument.
