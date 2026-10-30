# One bot identity for simple-notes and fleet-share

This change is source preparation, not a deployment or credential migration.
Use both commands with `--fleet-bearer-file PATH`, where PATH is that bot's
existing private, one-line fleet bearer file. The option is a path, never a
credential value. Both commands use the same file and hosted gateway.

The file must be owned by the current account, regular, mode 0600 or 0400,
with one link and no symlink in its path. Writable non-sticky ancestors and
ancestors owned by another account are refused. Parent directories may be 0755;
the credential itself must be private. The reader uses directory descriptors
and no-follow opens, reads at most 257 bytes, and does not copy, print or export
the credential. Unsafe existing files must not be used. This change does not
change their modes or distribute their contents.

The file's token format names the expected seat; the gateway's authenticated
`whoami` must confirm that same seat before a tool call. A filename, an
environment variable or `--identity` cannot override it. The gateway checks
expiry, revocation, pause, MCP acceptance and the seat's existing permissions.
This does not mint a bearer or grant new tool/admin permissions.

Only the fixed HTTPS gateway receives the fleet bearer, in its Authorization
header. No environment credential, direct provider credential, personal login,
proxy, redirect, cookie or failed-call fallback is used in this mode.
The Operator HMAC and FleetLink provider credentials stay on the servers.
They authenticate backend services, not the calling bot. Hosted handlers derive
the acting seat from the authenticated gateway connection, never tool arguments.

## Coverage

- Notes create/update/pin/unpin/read/list/status/log use existing hosted tools.
  The client preserves the 300-second CLI wait with status polls against the
  same request id, never by resending a write. A failed poll leaves the queued
  result and request id recoverable. The Mac runner is unchanged.
- FleetLink uploads preserve directory paths, multiple files, streaming bodies,
  existing-share reuse, expiry, password, personal/team access and share options.
  Empty share preparation and reuse are added to FleetLink's MCP through its
  existing account/access policy. Upload grants bind the file path, share slug,
  issuer, size and expiry. A signed PUT carries no fleet bearer, follows no
  redirect, is never retried, and never triggers destructive cleanup after a
  transport failure. Inspect the share before repeating an uncertain write.
- Link/list/info/cat/add/rm-file/update/share/unshare/upload-url/renew/delete/
  whoami and team listing map to the existing hosted tools. All/bots listing
  and forever renewal remain subject to FleetLink's existing server permissions.
- Token/bot/account/admin management, team mutations and skill installation are
  not exposed through the fleet gateway. Fleet-bearer mode refuses these rather
  than borrowing provider/admin credentials. An explicit human/operator session
  remains a separate path. Without the new option, legacy behavior is unchanged;
  runtime launchers have not been switched by this PR.

## Coordinated release gates

The FleetLink PR adds empty/reuse share preparation and upload-password MCP
support. The AFC PR updates its mirrored schemas and clients. Deploy the
FleetLink server change before using the corresponding AFC gateway schemas,
then qualify the actual deployed gateway catalog and each bot's own identity,
provider binding and permissions. No new identity or broad service fallback
should be introduced for an unavailable seat. No credential rotation is needed.

The old Notes service-reader PR is separate and is not this identity solution.
This PR neither merges that work nor provisions its private configuration.
Native/stdin MCP wrappers that do not select this mode still need a separately
qualified launcher cutover; this source change does not claim a whole-fleet
runtime switch. Retained older CLI bundles remain valid rollback targets.
