# Assigned hosted stdio connector

Install code with `bash scripts/install-assigned-hosted-stdio.sh`. The existing
host owner binds a client's command to `~/.local/bin/assigned-hosted-stdio` and
injects only that client's already-authorized bearer as `ASSIGNED_MCP_BEARER`
through supported private custody. Do not put the bearer in command arguments,
MCP configuration JSON, shared plaintext files, logs or chat. No credential is
created or fetched by this installer or adapter. No default identity, endpoint
option, Infisical automation reader, Cloudflare pair or root fallback exists.
If the host cannot securely inject its own credential, binding is blocked.

The endpoint is fixed to `https://agents.simplewithus.com/mcp`. The credential
assigns the identity; omitted tool identity is filled with that assignment and a
conflicting assertion is refused. This local guard is not authentication. The
hosted server independently authenticates the bearer and enforces its seat. Do
not give a client another seat's bearer or automation reader. Shared OS root/jay
access outside this connector remains a separate wider boundary, not solved by
client flags or this transport. Preserve working access during migration.

Hosted tool results pass through unchanged, and the tools/list answer is the hosted
one, including all hosted families and seven GR tools.  The adapter sends MCP protocol
and session headers, accepts bounded JSON and SSE responses, and matches response
IDs.  It never follows redirects or retries a POST.  Transport failure, unreadable error or
response loss during a tool call returns `outcome_unknown`, retryable false. Check
the target state before another write; no UNKNOWN replay or clearing is included.
A server-returned tool error or explicit retry guidance passes through unchanged.

## Requests It Keeps Local

Every Worker request counts against a daily cap, so the adapter sends only what needs
the server.  The hosted handler is stateless and accepts a tools/call with no
initialize.

- `notifications/*` are dropped locally and never sent.
- `initialize` is answered from disk for 10 minutes after a real hosted answer, one copy
per bearer and requested protocol version, so seat instructions and capabilities stay
the hosted ones.  One in the modern `_meta` form is neither answered from disk nor stored.
- A first-page `tools/list` (one with no `cursor`, so a malformed cursor reaches the server)
is served from disk for 10 minutes, per bearer and protocol version, then fetched again.  A
new hosted tool can take that long to appear.
- A server that issues a session gets none of this: that seat's cache is cleared, nothing is
stored, and a connection with a live session is never answered from disk.
- `MCP-Protocol-Version` is the version in the request's own `_meta` when it names one, or
else the negotiated version, or else `2025-06-18`.  An `initialize` that names no version in its `_meta` sends none.

A request in the modern `_meta` form is served on the hosted server's modern path, which refuses
it with HTTP 400 and code -32020 unless its headers match the body.  The adapter sends
`MCP-Protocol-Version`, `Mcp-Method` (the method) and, for `tools/call`, `prompts/get`,
`resources/read`, `tasks/get`, `tasks/update` and `tasks/cancel`, `Mcp-Name` (`params.name`,
`params.uri` or `params.taskId`), in the `=?base64?…?=` form when the value is not plain ASCII,
is blank, or already looks like that form.  The legacy handshake is unaffected.

The cache is `$XDG_CACHE_HOME/assigned-hosted-stdio`, or `~/.cache/assigned-hosted-stdio`.
It holds hosted discovery answers and no bearer, and file names derive from a one-way
hash of the bearer.  A folder or file that is not private to this user is ignored.  A host
that sets neither variable, or whose sandbox cannot write there, runs uncached: it still
drops notifications but sends real initialize and tools/list calls.  Delete the folder to
force a refresh.

Connecting no longer proves the bearer works.  A bad, expired, revoked or paused bearer can
connect from the cache and then fail at the first tool call with code `bearer_refused`,
retryable false.  The call did not run, and that seat's cache is cleared.  Only the hosted
server's own 401 `invalid_token` answer gets this code; any other failure stays
`outcome_unknown`.

Qualification must include actual initialize/tools/list against an empty cache, own whoami
and a denied cross-seat read using the assigned credential, plus expected family names.  Source
synthetic tests do not prove a host's vault binding, actual tool inventory, all
verbs or all-seat readiness. Host installation and supported custody belong to
the host owner, not this code-only change.
