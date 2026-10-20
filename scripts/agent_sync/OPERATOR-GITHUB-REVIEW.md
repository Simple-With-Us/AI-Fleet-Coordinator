# OPS-966 Disabled Broker Review Slice

No service is started or registered with the daemon by this change. No runtime settings, listener partition, proxy, credentials, or host have changed. `Broker.enabled` defaults false.

## Files

- `github_token_router.py`: previously live-sampled token routing core.
- `operator_github.py`: same-process broker component and bounded HTTP transport.
- `operator_github_client.py`: HTTPS client and tightly target-bound gh read execution.
- `tests/test_operator_github.py`: 23 focused tests.

## Boundary

A trusted server lookup maps an authenticated seat to its App and exact repository permission ceiling. Client input cannot select an App. Separate mint credentials sign a timestamp, nonce, method, path, and hash of the exact body. Existing listener read/routine credentials are not reused. Nonces are bounded, atomically consumed and retained until stale; rate and concurrency limits bound requests. Current key and policy are checked on every call before token cache access. Server returns only that App's token, scoped to one explicit repo and approved permissions. Keys remain server-side.

The client requests a token, refuses mismatched scope and bot identity, and injects it into one child. It does not print tokens or run arbitrary commands on the server. Initial commands are `gh pr list`, numeric `gh pr view/checks`, and `gh repo view`. Other commands, write operations, arbitrary gh API paths, aliases, extensions, and cross-repo selectors fail closed. Bot author/committer environment uses the real bot ID provided by the broker, not Jay.

## Not completed / deployment blockers

- No wiring into daemon lifecycle or config yet. This keeps the feature unconditionally off until boundary review.
- Infisical-only App loader, independent caller-key loader and live policy loader need runtime integration. The listener entrypoint currently drops machine auth after loading Zulip variables; do not assume it can retrieve App keys later.
- Separate caller credentials need approved provisioning and per-seat distribution. No credentials created here.
- Need live verification of Coolify app/host, current service version, TLS origin, proxy routing, deployed machine-identity permissions and rollback command. Repo docs identify app `l40rxd4rbj1pnogmbtetzmsf` and the existing listener Dockerfile, but these have not been live-verified by this lane.
- Need Codex/Claude review before adding lifecycle hooks, config, write-command support or deployment.
- Git credential-helper exact-repo routing is not implemented in this initial slice. Git commands are refused, not silently routed.
- Client currently accepts explicit origin/caller/key-file arguments; runtime distribution must pin the approved HTTPS origin and provide each seat only its own credential.
- Durable nonce code added below; persistent mount and fail-closed startup verification are still deployment blockers.
- Token-router concurrent cache refresh can mint duplicates; atomic cache writes prevent corruption but issuance serialization is not yet implemented.
- Client command output is the child's normal output. Only tightly allowed read commands are supported; broader command output needs a separate review.

## Validation

23 broker/client tests pass, including real local HTTP transport tests with fake credentials/tokens. The original routing core has 12 passing offline tests and a separately reported two-App/two-repo live read sample. These are source-level results, not live deployment acceptance.

## Review follow-up: durable replay and narrow retrieval

The broker now requires an explicit nonce ledger and refuses issuance when it is absent or unavailable. NonceLedger uses SQLite BEGIN IMMEDIATE with a unique seat/nonce key and synchronous=FULL, committing consumption before issuance. The state directory must be private and on persistent storage shared by every instance accepting the same caller credential. This implementation supports one listener container and durable local storage, not separate replica-local volumes. Backups, restored old databases and lost volumes must never silently reuse the same caller credentials: disable minting and resolve replay state or revoke those credentials first. Rate limits remain process-local. No lifecycle hook or deployment is enabled.

NamedSecrets reads one exact name per call, with explicit approved seat/App sets, no folder export, no imports or reference expansion, and no universal-auth bootstrap exchange. A supplied bearer provider must use a separate scoped runtime identity. This adapter is not yet wired into the daemon. The effective Infisical permissions and refresh/revocation behavior must be verified before activation; source allowlists alone do not restrict a broad bearer.

## Proposed credential provisioning (approval required, nothing created)

- Fleet-OPS prod /operator-github/callers: OPERATOR_GITHUB_CALLER_<SEAT>_HMAC, separate random 256-bit value per approved seat. The seat token is uppercase with underscores only, explicitly mapped by policy, never inferred from AGENT_SEAT. Example: OPERATOR_GITHUB_CALLER_CODEX_HMAC.
- Broker machine identity: read only those approved caller names and the approved GH_APP_<SLUG>_ID / GH_APP_<SLUG>_PRIVATE_KEY pairs in /github-apps. No writes, no other folders/projects, no Zulip/admin/bootstrap credentials. Deployment must prove denied reads outside that set. If name-level ACL cannot be enforced in the installed Infisical version, isolate runtime copies in a dedicated project/path before proceeding, with explicit approval.
- Each seat's existing machine identity gets read permission only for its own caller name, never the sibling caller names or App PEMs. Do not distribute the broker machine identity. The local client loads its own caller secret into a private file or process memory; never commits it, puts it in a command argument or posts it to Zulip.
- Pin the public HTTPS broker origin and caller seat in trusted local config. Current client key-file mechanism needs runtime integration; no caller-name environment value is authority.
- Independent server-side policy maps the authenticated seat to exactly its App, explicit repos and permission ceiling. Policy and current key are checked before token cache use. Refuse unknown/revoked callers. No read-record/routine HMAC reuse and no PAT fallback.
- Provision only a small approved pilot set first. Proposed initial pilot CODEX and ECHO; exact repos/ceilings still require confirmation. Do not provision every fleet seat by default.
- Revocation: remove the caller from active policy first, revoke its secret read grant and caller key, verify old signed request denied before cache access. Rotation is a separate approved operation.
- Runtime bootstrap: supply the independently scoped identity through the host's protected secret mechanism, load no broad bootstrap credential into the listener. Choose/verify concrete short-lived token renewal and deletion behavior before activation.

Codex identified the broker target as Coolify l40rxd4rbj1pnogmbtetzmsf, entrypoint python3 /app/scripts/agent-sync daemon run --wait-lock, with runbook docs/protocols/agent-sync-listener.md. Mac is a separate listener, not the broker target. This is a coordination report, not this lane's live host verification. Need storage mount, TLS route, identity scope/renewal, current deployment and rollback verification before wiring/activation.
