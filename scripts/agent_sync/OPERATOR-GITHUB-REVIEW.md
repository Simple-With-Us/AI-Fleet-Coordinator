# OPS-966 Disabled Broker Review Slice

No service is started or registered with the daemon by this change. No runtime settings, listener partition, proxy, credentials, or host have changed. `Broker.enabled` defaults false.

## Not a minting path: git-relay owns GitHub App tokens (2026-10-22, Q5)

Owner decision: **git-relay is the ONE path in the fleet that mints per-bot GitHub App tokens.**
The Operator GitHub token broker is therefore not a minting path, and this slice stays as reviewed
history only. Nothing below was deleted; the code is retained and unchanged, but it cannot mint.

- `Broker.enabled` still defaults to `false`.
- `github_token_router.TokenRouter.token_for` and `repo_token.TokenRouter.token_for` now call
  `refuse_minting()` as their first statement and raise `RoutingError` naming `git-relay`, before
  any App credential is read, any cache file written, or any GitHub call made.  The refusal is a
  module constant, not an environment variable or constructor flag:  there is no override and no
  fallback that re-enables minting here.
- The broker's policy table, nonce ledger, client and target binding are all still in place and
  still tested;  they simply no longer lead to a token.

Enforced by `tests/test_github_relay_minting_owner.py`, which fails if a second unguarded mint
site appears anywhere in the repository, if the guard stops firing, or if the broker's real router
can reach a mint.  For the actions themselves, see `git-relay/README.md`.

## Files

- `github_token_router.py`: previously live-sampled token routing core.
- `operator_github.py`: same-process broker component and bounded HTTP transport.
- `operator_github_client.py`: HTTPS client and tightly target-bound gh read execution.
- `operator_github_policy.py`: the seat allowlist - the trusted server-side policy lookup (pilot: CODEX and ECHO, AI-Fleet-Coordinator only, read ceilings), plus the allowlist-bound caller-key and App-credential loaders.
- `operator_github_state.py`: durable SQLite replay ledger.
- `operator_github_secrets.py`: named-secret adapter (one exact name per call, approved sets only).
- `tests/test_github_relay_minting_owner.py`: minting-ownership guard - git-relay is the only
  minting path, and this broker cannot mint.
- `tests/test_operator_github.py`: 23 focused tests.
- `tests/test_operator_github_policy.py`: allowlist shape, loader binding and wired-broker end-to-end tests.

## Boundary

A trusted server lookup maps an authenticated seat to its App and exact repository permission ceiling. Client input cannot select an App. Separate mint credentials sign a timestamp, nonce, method, path, and hash of the exact body. Existing listener read/routine credentials are not reused. Nonces are bounded, atomically consumed and retained until stale; rate and concurrency limits bound requests. Current key and policy are checked on every call before token cache access. Server returns only that App's token, scoped to one explicit repo and approved permissions. Keys remain server-side.

The client requests a token, refuses mismatched scope and bot identity, and injects it into one child. It does not print tokens or run arbitrary commands on the server. Initial commands are `gh pr list`, numeric `gh pr view/checks`, and `gh repo view`. Other commands, write operations, arbitrary gh API paths, aliases, extensions, and cross-repo selectors fail closed. Bot author/committer environment uses the real bot ID provided by the broker, not Jay.

## Resolved 2026-10-21

- **#791 credential-project caveat (Kodus):** `service_identity.operator_credentials` pinned the Operator read to the AFC project, but `ZULIP_OPERATOR_EMAIL` / `ZULIP_OPERATOR_API_KEY` / `ZULIP_OPERATOR_SITE` live in Fleet-OPS `/zulip_operator` (verified present there, names only). The read now uses the standard Fleet-OPS project setting (`INFISICAL_FLEET_OPS_PROJECT_ID`) with the Operator's own folder. Deployment note: the listener's Infisical identity already reads Fleet-OPS `/zulip`; it must also be granted `/zulip_operator`.
- **Seat allowlist:** `operator_github_policy.py` is the code-reviewed policy table and loader binding. Pilot seats CODEX and ECHO, `Simple-With-Us/AI-Fleet-Coordinator` only, read-only ceilings (contents, pull_requests, metadata). Widening any seat, repo, or ceiling is a code change here.
- **gh target binding:** `bound_command` in the client binds every execution to the explicit `--repo` routing target and refuses URLs, aliases, extensions, arbitrary API paths, cross-repo selectors and non-numeric PR selectors. `child_environment` strips all ambient GH_/GITHUB_/GIT_ variables and injects only the minted token plus the broker-provided bot identity. Git (non-gh) commands remain refused, not silently routed.

## Not completed / deployment blockers

- No wiring into daemon lifecycle or config yet. This keeps the feature unconditionally off until boundary review.
- Runtime integration of the loaders: the listener entrypoint currently drops machine auth after loading Zulip variables; do not assume it can retrieve App keys later. The wiring contract is specified below.
- Separate caller credentials need approved provisioning and per-seat distribution. No credentials created here.
- Need live verification of Coolify app/host, current service version, TLS origin, proxy routing, deployed machine-identity permissions and rollback command. The operator's own inventory identifies the application, and the existing listener Dockerfile, but these have not been live-verified by this lane.
- Need Codex/Claude review before adding lifecycle hooks, config, write-command support or deployment.
- Git credential-helper exact-repo routing is not implemented in this initial slice. Git commands are refused, not silently routed.
- Client accepts explicit origin/caller arguments; the caller key resolves environment-first per the binding contract, and runtime distribution must pin the approved HTTPS origin and provide each seat only its own credential.
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

Codex identified the broker target as the Coolify application in the operator's own inventory, entrypoint python3 /app/scripts/agent-sync daemon run --wait-lock, with runbook docs/protocols/agent-sync-listener.md. Mac is a separate listener, not the broker target. This is a coordination report, not this lane's live host verification. Need storage mount, TLS route, identity scope/renewal, current deployment and rollback verification before wiring/activation.

## Codex-dependent pieces: interface contract (blocked on Codex's listener host)

The code side of both pieces is specified here so any lane can implement against it; the host side needs Codex (listener host owner, at usage cap until ~2026-10-26).

**Listener host wiring.** In the daemon entrypoint (`scripts/agent-sync daemon run`), after Zulip variables load and BEFORE machine auth is dropped: construct `adapter = NamedSecrets(seats=operator_github_policy.SEATS, apps=operator_github_policy.APPS)`, build `Broker(key_for=caller_key_loader(adapter), policy_for=load_policy, router=TokenRouter(app_credentials_loader(adapter), <private cache_dir>), bot_identity=<GitHub /app lookup>, enabled=False, nonce_ledger=NonceLedger(<durable mount>))`, and mount `BrokerHttp` behind the listener's existing TLS route at exactly the `/operator/v1/github/` prefix. Flipping `enabled` requires the owner's go plus the rollout checks already listed above (TLS origin, proxy routing, durable mount verification, rollback command).

**Caller-identity binding.** Each approved seat resolves exactly one credential: `fleet-secrets get OPERATOR_GITHUB_CALLER_<SEAT>_HMAC --path /operator-github/callers --project fleet-ops` (owner decision 2026-10-21: the CLI is the only credential path); there is no environment or key-file fallback. Pinned local config names the broker HTTPS origin and the seat. The client's `--origin`/`--caller` arguments must come from that pinned config, never from chat input. The broker authenticates the seat by HMAC and maps seat-to-App through `operator_github_policy` only, so a stolen caller key mints only within that seat's own ceiling.

**Who could take them:** the wiring change itself is lane-agnostic once Codex confirms the entrypoint shape above against the real daemon (it owns the listener host); Codex or Echo's lane can then land it. Host access, TLS route and durable mount stay Codex-owned deployment steps.
