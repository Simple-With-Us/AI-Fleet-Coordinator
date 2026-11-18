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
GH_TOKEN="<the scoped read>" gh api user
```

The scoped read resolves the value into the environment variable that CLI already reads,
so the CLI prefers it over whatever it has in its own store, and the value never lands in
an argument list.  The reader is the consumer's own scoped service token through
`scripts/fleet_infisical.py`.  The server's fleet tools already read Fleet-OPS prod, so
this is the same read they already do.

## What is not in this

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

## A token that has been printed is a token to rotate

A CLI login that appears in a transcript, a log or a chat message is exposed, and it
has to be rotated.  That is the reason the write prints a name and a path, and the
reason the extractors read from a store rather than from an argument.
