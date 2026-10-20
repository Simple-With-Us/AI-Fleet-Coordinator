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
- Nonce ledger is in-memory. Before activation, add restart-safe replay handling or ensure keys rotate across restart; otherwise an unexpired signed request can replay after a restart.
- Token-router concurrent cache refresh can mint duplicates; atomic cache writes prevent corruption but issuance serialization is not yet implemented.
- Client command output is the child's normal output. Only tightly allowed read commands are supported; broader command output needs a separate review.

## Validation

23 broker/client tests pass, including real local HTTP transport tests with fake credentials/tokens. The original routing core has 12 passing offline tests and a separately reported two-App/two-repo live read sample. These are source-level results, not live deployment acceptance.
