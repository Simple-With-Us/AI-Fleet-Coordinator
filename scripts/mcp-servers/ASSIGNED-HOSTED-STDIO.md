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

Hosted tools/list and result payloads pass through, including all hosted families
and seven GR tools. The adapter forwards MCP initialization protocol and session
headers, accepts bounded JSON and SSE responses, and matches response IDs. It
never follows redirects or retries a POST. Transport failure, unreadable error or
response loss during a tool call returns `outcome_unknown`, retryable false. Check
the target state before another write; no UNKNOWN replay or clearing is included.
A server-returned tool error or explicit retry guidance passes through unchanged.

Qualification must include actual initialize/tools/list, own whoami and a denied
cross-seat read using the assigned credential, plus expected family names. Source
synthetic tests do not prove a host's vault binding, actual tool inventory, all
verbs or all-seat readiness. Host installation and supported custody belong to
the host owner, not this code-only change.
