# git-relay - the GitHub relay CLI, and GR-MCP

git-relay knows each seat from its bearer token and picks the credential by task:  Opening a PR
runs under the owner's scoped PR-only token (the only copy lives in the service's
environment), and every other GitHub task runs under the seat's own GitHub App token, which
git-relay mints and refreshes from App keys in Infisical.  Every call is checked against per-seat
policy, and every denial, failure and write is written to the audit log (a successful read is not).

The MCP wrapper is **GR-MCP**:  `git-relay mcp`.  It is the GitHub work's own MCP server.  The
bearer-token arrangement is shared with ST-MCP, so a seat may find these tools on its ST-MCP
connection as well;  GR-MCP is the other route, and the tool names are the same either way.

## git-relay owns per-bot GitHub token minting (owner decision 2026-10-22, Q5)

git-relay is the ONE path in the fleet that mints per-bot GitHub App tokens.  The OPS-966
Operator GitHub token broker (`scripts/agent_sync/operator_github*.py`,
`scripts/agent_sync/github_token_router.py`, `scripts/operator/github_tokens/repo_token.py`) is
NOT a minting path:  it stays disabled by default, and both of its mint primitives now fail
closed naming git-relay, before any App credential is read.  That code is retained for review
history and was not deleted;  it simply cannot mint.

`scripts/agent_sync/tests/test_github_relay_minting_owner.py` is the executable form of this:
it fails if a second unguarded mint site ever appears in the repository, if the broker's guard
is bypassed, or if the broker's real router can reach a mint.

## Status

Deployed on the fleet server as a systemd service behind its own Cloudflare tunnel, with an
Access service token in front of the public hostname.  The relay mints every seat's App token
itself from the App keys in Fleet-OPS.

A seat reaches it through ST-MCP's `gr_*` tools on a bearer connection:  its own `sbt_<seat>_`
bearer, or `gbb_<role>_` for a Grok Bot persona, minted on the ST-MCP + GR-MCP Admin page.  The
Worker forwards that bearer and the relay resolves it through the Worker's verifier.  An OAuth
connection carries no bearer, so its `gr_*` tools answer `not_configured`.

The seat policy is [deploy/seat-policy.json](deploy/seat-policy.json), installed with
`deploy/seat_policy.py` (see Policy).  Moving seats off the interim `gh` and shell path is still
the owner's cutover decision.

## Actions

| Action | Endpoint | Body | GitHub call |
| --- | --- | --- | --- |
| open_pr | POST /v1/open_pr | repo, base, head, title, body | POST /repos/{repo}/pulls |
| edit_pr | POST /v1/edit_pr | repo, number, title?, body? | PATCH /repos/{repo}/pulls/{number} |
| reply_thread | POST /v1/reply_thread | repo, number, comment_id, body | POST /repos/{repo}/pulls/{number}/comments (in_reply_to) |
| comment_pr | POST /v1/comment_pr | repo, number, body | POST /repos/{repo}/issues/{number}/comments |

`GET /healthz` needs no auth. Everything else needs `Authorization: Bearer <seat token>`.

Only opening a PR is authored as the owner through the scoped PR-only token.  Edits, review
replies, comments, merges, workflow dispatches, thread resolves and token mints run under each
seat's OWN GitHub App with a relay-minted installation token - see the action catalog below.  Seats never get a raw token or a `git push`:  the file-write verbs (`wf_file_put`, `wf_files_put`) commit through the API, on a non-default branch only and never under `.github/workflows`;  no seat policy can exceed its GitHub credential.

## Scope and action catalog (owner direction 2026-10-21)

The relay covers every GitHub task seats need.  It knows the seat from its bearer token and picks
the credential BY TASK:  opening a PR uses the owner's scoped PR-only token so Kody can review an
owner-authored PR; every other write, including edits, review replies and comments, uses the seat's OWN GitHub App token, which the relay mints and refreshes itself
from App keys in Infisical - seats never handle an App id, private key, or the 1-hour token expiry.
GITHUB_UNIVERSAL_PAT is not used by anyone.  Default deny:  an action is callable only by seats
explicitly allowlisted for it, and App actions only within the seat's mint_permissions ceiling.
Actions are catalogued even where no seat is allowlisted yet;  granting a seat later (Echo included)
is only a policy entry.

| Action | Credential | Status |
| --- | --- | --- |
| open_pr | owner PR-only token | implemented |
| edit_pr (title/body only) | seat's App, relay-minted | implemented |
| reply_thread (inline review reply) | seat's App, relay-minted | implemented |
| comment_pr | seat's App, relay-minted | implemented |
| mint_token (per-repo, per-permission) | seat's App, relay-minted | implemented |
| merge_pr (squash/merge/rebase) | seat's App (contents+pull_requests write) | implemented |
| workflow_dispatch | seat's App (actions write) | implemented |
| resolve_thread | seat's App (contents write + pull_requests write) | implemented |
| create_release, rerun_workflow, cancel_workflow, set_labels, create_issue_comment, request_reviewers, set_branch_protection, create_repo | seat's App | registered, refused until implemented |

App-token minting:  GH_APP_<SLUG>_ID / GH_APP_<SLUG>_PRIVATE_KEY read together - one
`fleet-secrets` call per App - from the /github-apps folder (GIT_RELAY_SECRETS_PATH
overrides) through the `fleet-secrets` CLI only (owner decision 2026-10-21:  no
process-environment secrets, no direct Infisical HTTP in the relay), JWT signed with the local openssl binary
(the key reaches openssl through an inherited pipe fd, never a file), installation tokens
cached in memory under per-(app, repo, permissions) locks (one slow mint never blocks another
seat's) and refreshed 5 minutes early.  Values are cached in memory
per process.  Nothing secret is written to disk, logged, or audited;  audit rows hash token
values.  GIT_RELAY_APP_MINT=0 disables App actions;  otherwise the fleet-secrets executable
must be on PATH at startup.

## MCP wrapper

**GR-MCP** (`git-relay/git_relay/mcp_server.py`, run as `git-relay mcp`) is a stdio MCP server
(initialize, tools/list, tools/call) that advertises itself as `GR-MCP` and exposes the relay
actions as MCP tools. A seat runs it with only its own bearer token
(GIT_RELAY_SEAT_TOKEN, GIT_RELAY_URL); the wrapper forwards over HTTP, so policy, caps and audit
stay server-side. Refusals come back as tool errors, never protocol errors.

## Relay as a permission layer (owner direction 2026-10-21)

Per-seat policy (actions, repos, mint_permission ceilings, rate caps) is a first-class permission
layer of its own:  it can narrow what a credential allows, never exceed it.  A seat's effective
power is the INTERSECTION of the relay policy and the underlying GitHub credential - tightening
either side restricts the seat, and no policy entry can grant what GitHub already denies.  Policy
lives in reviewed config (GIT_RELAY_SEATS_JSON sourced from a reviewed file or Infisical), so
permission changes ship without touching GitHub App settings;  every change is a reviewed PR (or an
audited Infisical write), and every denial, failure and write is in the audit log (a successful read is not).

## Policy

Seats come from `GIT_RELAY_SEATS_JSON` at process start:

```json
{
  "swu-echo": {
    "token": "<bearer, 16+ chars>",
    "actions": ["open_pr", "edit_pr", "reply_thread", "comment_pr"],
    "repos": ["Simple-With-Us/AI-Fleet-Coordinator", "Simple-With-Us/*"]
  }
}
```

A seat's `app_slug` must be that seat's own bot identity:  one bearer = one Zulip bot = one
GitHub App (owner decision 2026-10-22, Q7).  A config entry naming another bot's App is refused
at load, so a bearer can never act through a GitHub App that is not its own bot.

Every check is an allowlist: an action or repo not named is denied. Token comparison is
constant-time over all seats. A per-seat hourly call cap (`GIT_RELAY_RATE_PER_HOUR`, default 3600) bounds a leaked seat
token's blast radius; it is not a throttle for routine work, and GitHub's own 429 with Retry-After is
returned to the caller as a retryable refusal.

### The reviewed seat policy file

[deploy/seat-policy.json](deploy/seat-policy.json) is the production policy.  It holds no secret:
every seat is `auth: shared` and signs in with its own admin-minted bearer.  It lists one entry
for each of the 41 identities in this file (source, not live: the live seats file differs, for example it also carries BF-BUILDER, which is omitted here), each acting as the `swu-` App
whose GitHub description names that seat.  Five slugs are not `swu-<identity>`:  MA is
`swu-rob`, GROK is `swu-grok-build`, MM is `swu-minimax`, JET-CHAT is `swu-jet` and AMP is `swu-amp-agent` (`swu-amp` is reserved for an account).  JET-CHAT is the full Jet login on its own policy entry; the original JET wake-only login has no Git entry (Q7).  Seats share the `fleet` profile
(the same actions, repositories and permission ceiling);  Since the 2026-11-01 every-repo widening (owner instruction) the fleet profile names the two verified namespaces, `Simple-With-Us/*` and `jaywedgeworth22/*`;  Echo's profile survives but resolves to the same set.  The fleet profile carries a 178-action list (8 relay actions, including `git_read`, plus 170 workflow actions, including `wf_pr_close` and `wf_pr_reopen`) and the live 10-key mint ceiling.  `swu-amp-agent` and `swu-wise` keep the host's exact live action ordering through their own pinned profiles (the same set, ceiling and repositories as fleet).  BF-BUILDER is dropped (owner 2026-11-01:  BF-ENGINEER replaced it):  it has no entry, so the relay authenticates no BF-BUILDER bearer.  Every GB seat and every BF bot, including BF-TRADER and BF-ENGINEER, is on the fleet profile.  `git_relay/tests/test_seat_policy_file.py` holds the file to the relay's SeatPolicy, the
Worker's identity lists and each action's permission needs.

On the relay host, as root.  Root runs only a root-owned copy that only root can write, never
the files in a checkout another account can change.  So copy the two files from the merged
commit into a root-owned folder first, and check that the blob ids the script prints equal
`git rev-parse <commit>:git-relay/deploy/seat_policy.py` and the same for `seat-policy.json`.
`UNIT` is the relay's systemd unit, and everything else about the deployment is read from it:
the relay code (its working directory), the policy line (its one EnvironmentFile), the apps
credential (its LoadCredential) and the health address and App-key folder (its environment).

```
python3 -I <copy>/seat_policy.py install --unit UNIT                          # validate with the deployed code, diff, digest
python3 -I <copy>/seat_policy.py install --unit UNIT --apply --expect DIGEST  # back up, write, restart, check health
python3 -I <copy>/seat_policy.py verify-apps --unit UNIT                      # every seat's own App mints read-only and reads
```

`--apply` installs only the policy whose digest the dry run printed.  It restores the previous
file when the restart fails, the relay is not healthy within 30 seconds, or the run is
interrupted.  `verify-apps` runs the deployed minter in a transient unit with the relay's own
credential binding and App-key folder, so it reads the App keys exactly as the service does, and
it never prints a token.  A policy entry does not make a seat usable alone:  the seat also needs
its bearer, minted by the owner on the Admin page.

## Outbound hygiene

Seat-supplied text (PR titles, bodies, review replies) is scanned for credential shapes
(fine-grained and classic GitHub tokens, private-key blocks, Infisical service tokens) and
refused with a 400 before anything leaves for GitHub - a seat cannot paste or smuggle a secret
into content authored under the owner's token.  The audit log applies the same scan:  any detail
value matching a credential shape is stored hashed, whatever key it arrived under.

### Reviewed security-test fixtures

File writes still scan the full decoded content before any GitHub write. The
server-owned `git_relay/reviewed_fixtures.py` records thirteen exact-content
attestations for synthetic credential-rejection fixtures in AFC. Each entry binds
repository, destination path and SHA-256 of the entire file. It is not a caller
option, path-wide exemption, environment setting or trust in existing Git history.
Changing one byte or destination invalidates the attestation; any credential shape
then fails closed. The ordinary scanner and the rejection tests are unchanged.
PR text, messages, binary assets and all unattested files have no exemption.

All thirteen rows were re-verified byte-exact against the corrected rename checkpoint
`81d8bb0cd4f5a63d3dc330b8f42be4a3e93e87a3` (tree
`b548e1a81d4fd18d72206f8c1f22cb41a6d19bcf`, built on main
`ac6d5d44500937274aeddfc794a05fb427158f8c`). The first checkpoint
`4977e6a7970dc0a25638181600137fe06167dcec` is retained as original evidence only,
never as a shipping target: semantic review found out-of-scope changes in it, so the
corrected checkpoint restores the current retirement behavior and wire-protocol
surfaces and drops the bundled seat-policy and cleanup changes. Ten rows carry
digests unchanged from the first re-attestation; three rows
(`scripts/fleet_lanes/tests/test_install_mcp.py`,
`scripts/simple-tools-mcp/test/integration.workerd.mjs`,
`scripts/simple_tools/tests/test_infisical_api_route.py`) are refreshed from the
corrected checkpoint (10 + 3 = the 13 rows in the registry and the table below). The rename-affected destinations are FUTURE targets: they do not exist
on main yet, match nothing, and fail closed until the exact attested bytes land with
the rename; the published rename branch currently omits these ten files, which is the
gap this attestation exists to close:

| Destination | Source Git blob | Whole-file SHA-256 |
| --- | --- | --- |
| `scripts/simple_tools/mcp/fixtures.jsonl` | `a4ff4ac5636654ee20954aa7e14595c125c5d547` | `bea6ca376aefdce65ce0295dbc72cef4a396b051c8add3e6d0174fc917c81829` |
| `scripts/simple_tools/tests/test_operator_github.py` | `db5248bc7e9f61daaffd180b9b7b026643a07a14` | `ef10944dc053c2f4bf664f1ee796257d0deb4d64f1bb786be9ad58ec3ea4b442` |
| `scripts/simple_tools/tests/test_operator_send.py` | `2ef871ceb92213e7208ab2faaa2cb235d478faf8` | `4353cf8e69c53151e3340c4a1bed374e380bac3dfa076e22d8c08eb575963873` |
| `scripts/test-lark-mm-bridge.py` | `287b0c42cb7234deb9fd03d359ed6e52fe40959e` | `f94d6ecb794baab74b2d43c97b7cf5efd8aab0e76bc42e2a6224eb2f4ddb5535` |
| `scripts/test_operator_provenance.py` | `d3c23c8fd8b3ea6b73897c6ab456355a488e3f0a` | `a72e105b054617eecebdd2011dfca341e08dbc9377e378dccc8733aa7fea2c1b` |
| `scripts/simple-tools-mcp/DEPLOY-PHASE0.md` | `cb10a4f133a5e90a1b3bdc77b1df818ad9dac0e9` | `5f85629a6a6979a763063ad3b79fd56b5d77ab93d2cd0e6da4c200485debca3f` |
| `scripts/simple-tools-mcp/test/gr-assert.test.mjs` | `80f27a413e461e9adfc8924c881e29d4d9e2d21c` | `ecfa713b33754e1331a8fee0e07c67271fc186908a52830839f1b530a246dad8` |
| `scripts/simple-tools-mcp/test/gr-front.test.mjs` | `b46549565f659d145f30d6ff1ede3af6515de64d` | `9de759b53f8cb987b16ade0a9cf99ff69d999659519a620b292f4aa748eee3b9` |
| `scripts/simple-tools-mcp/test/gr-workflow-v1.test.mjs` | `9a36132edd4b6614209c2f6dd12a9df13a939c05` | `1caec47629a7a4592051b25efcfd229cfe662471027c13eb3c5d812952816e95` |
| `scripts/simple-tools-mcp/test/integration.workerd.mjs` | `f122a279489d871d7fc3f71c97acd5e2ec9dcfaf` | `2b76ceeed6f6fdf66f6e04523d05cc9652e00377e82bcb55268ceaf143e10f8c` |
| `scripts/simple_tools/tests/test_infisical_api_route.py` | `7f962a62b00f7db6660168db818735142d897278` | `34041aaa826e24b919621c7d5ed6703c2b602beda05449dd9801a13e02d67090` |
| `scripts/simple_tools/tests/test_mcp_extras.py` | `eaf0c913742de23ef19e5c655a01dc3e5ef071c7` | `d31c6f0a7f84f17dd0d480714a5d0be9f05fad97d3a3ef5d519da68b304bc95a` |
| `scripts/fleet_lanes/tests/test_install_mcp.py` | `6b2392ff3238806f7ad251b347e424b8bb99b57d` | `a0b944f012a3fa8c27dfe52cab1c53ef8893ec4f945e6b62eb6c297f42e8853b` |

Every attestation addition/change requires normal source review of the complete
fixture contents and its provenance, evidence that values are synthetic and
rejection behavior is preserved, negative controls for added/changed credentials,
and deployment of the reviewed service code by its operator. A filename, a test
claim or a digest supplied by a seat cannot create an attestation. Do not add real
credentials even if already present in history. Fail closed if review cannot
establish that a fixture is synthetic. No automatic registry generation/update
runs in the service. This change does not deploy itself or resume a paused write.

`mint_token` returns a raw App installation token, so it is NOT exposed over the MCP wrapper
(a stdio transcript is too easy to capture);  seats that need a token for git-over-HTTPS call the
relay's HTTP API directly, where the response goes only to that caller.

## Read-only git

`GET /git/<owner>/<repo>[.git]/info/refs?service=git-upload-pack` and `POST /git/<owner>/<repo>[.git]/git-upload-pack` serve `git fetch` and `git clone` under the action `git_read`, as the seat's own GitHub App, with the seat's bearer in the `Authorization` header.  The relay mints a `contents:read` token for that one repo and keeps it, so the caller never holds a GitHub credential.  Pushing has no route and answers 403.  A refused, denied or failed request writes one audit row (a successful fetch writes none), and each request draws on the seat's hourly quota, at most four transfers run at once and two per seat, and a later one answers 503.  A long want list needs `http.postBuffer` set to 8 MiB, because the route refuses a chunked request.  The code is `git_relay/git_read.py`.

## Audit

Owner ruling Fri, Nov 6, 2026:  successful reads leave the audit log.  A call that ends `ok` on a read action (`git_read` and the `wf_` reads, `SeatPolicy.audits`) writes no row;  every denial, failure and write still writes one, and so does `mint_token`, which issues a credential.

One JSON object per line at `GIT_RELAY_AUDIT_PATH` (default `git-relay-audit.jsonl`), fsynced after
every write: timestamp, seat, action, repo, result (ok / denied / bad_request / unavailable /
github_terminal / github_unknown / duplicate_refused), and the GitHub id/URL returned. Titles and
bodies are never logged; the row keeps their length and SHA-256 so a call is attributable without
storing text that might contain a pasted secret - the same hashing applies to credential-shaped
strings nested anywhere in the detail payload.  Upstream error MESSAGES are never logged (an
error body can carry content);  the row keeps the numeric status only.  Seat-supplied detail keys
can never overwrite the reserved seat/action/result/ts fields.  The GitHub token is never logged
or returned.

## Bounds and failure shape

Request bodies are capped at 64 KiB, text fields at 32 KiB, GitHub responses at 1 MiB (8 MiB for a
paged GET, whose raw rows are far larger than the rows the relay returns), and every
GitHub call has a 30 s timeout.  Workflow-dispatch inputs are a flat object of at most 32 scalar
values (strings capped at 1024 characters).  Neither the GitHub client nor the Infisical client
follows 3xx redirects, so a bearer can never be forwarded to another origin.  Errors: 401
unauthenticated, 403 policy denial, 400 bad request or terminal GitHub refusal, 409 idempotent
replay refusal (an identical request is in flight or its outcome is unknown), 429 GitHub rate
limit (retryable), 502 GitHub outcome unknown (NOT retryable - see the outcome store), 503
app-token minting unavailable.

## Outcome store (write idempotency)

Every non-idempotent write (open_pr, edit_pr, reply_thread, comment_pr, merge_pr,
workflow_dispatch, resolve_thread) is gated by a durable outcome record at
`GIT_RELAY_OUTCOMES_PATH` (default `git-relay-outcomes.json`), keyed by seat + action + canonical
arguments, with explicit pending / ok / refused / unknown states.  A 4xx answer from GitHub is a
definite refusal and releases the key, with two exceptions:  408 (request timeout) can arrive
after GitHub applied the write, so it is treated as outcome UNKNOWN, and 429 is simply
retryable.  A transport failure or 5xx likewise leaves the outcome UNKNOWN:  the relay answers 502 with `"retryable": false`, and an identical
replay is refused with 409 until the seat checks GitHub and changes the request.  A replay of a
successful call returns the stored result without touching GitHub twice.  A crash mid-write loads
as unknown - the store fails closed, never replays.  The registry is bounded:  ok rows are
evicted after a 24 h replay window, refused rows are evicted oldest-first under capacity, and
a store full of held pending/unknown rows refuses new writes with 503 until an operator
reconciles them (fail closed - never evict a held side effect).  Writes go through a
full-write loop, so a short write fails loudly instead of renaming a truncated registry.  `mint_token` is exempt:  it returns a raw
credential (never cached in the store) and a re-mint is safe under the minter's own lock.

## Configuration and running

| Env var | Default | Purpose |
| --- | --- | --- |
| GITHUB_TOKEN | (required) | Scoped PR-only token; the only credential the service holds |
| GIT_RELAY_SEATS_JSON | (required) | Seat tokens, action allowlists, repo allowlists |
| GIT_RELAY_VERIFY_CACHE_S | 3600 | Seconds one relay process reuses a positive `sbt_`/`gbb_` bearer verify, capped at 3600 and never past the bearer's expiry;  0 verifies live on every call.  A pause or revocation can reach that bearer's Git calls, writes included, up to this late (owner ruling Fri, Nov 6, 2026).  The hour runs from the Worker's own live check (`verified_at` in its answer), so the Worker's cache and this one together stay within one hour, and an older positive answer never replaces a newer refusal.  Refusals are remembered for 30 seconds as refusals only. |
| GIT_RELAY_AUDIT_PATH | git-relay-audit.jsonl | Audit log location |
| GIT_RELAY_OUTCOMES_PATH | git-relay-outcomes.json | Write idempotency outcome store |
| GIT_RELAY_HOST | 127.0.0.1 | Bind address |
| GIT_RELAY_PORT | 8471 | Bind port |
| GIT_RELAY_RATE_PER_HOUR | 3600 | Per-seat hourly call cap |
| GIT_RELAY_WRITE_RATE_PER_HOUR | 500 | Per-seat hourly cap on state-changing dispatches (logical writes, a second bound under the call cap; not a GitHub request count) |
| GIT_RELAY_APP_MINT | 1 | Set to 0 to disable App actions and all App minting |
| GIT_RELAY_ENABLE_WORKFLOW_VERBS | off (unset) | Set to 1 to wire the wf_ workflow verbs into HTTP dispatch; any other value keeps them refused as registered-not-wired |
| GIT_RELAY_SECRETS_PATH | /github-apps | Fleet-OPS prod folder the GH_APP_<SLUG>_* pairs are read from;  a set-but-empty value is a startup config error |
| GIT_RELAY_URL | http://127.0.0.1:8471 | GR-MCP (`git-relay mcp`) only:  the relay base URL each tool call is posted to, as `<url>/v1/<tool>` |
| GIT_RELAY_SEAT_TOKEN | (required by `git-relay mcp`) | GR-MCP only:  the seat's own bearer token, sent to the relay as `Authorization: Bearer`;  unset or empty stops the wrapper at startup |

```
git-relay serve                 # the CLI; also the default command
git-relay mcp                   # GR-MCP over stdio
python3 -m git_relay.server     # equivalent, no wrapper needed
```

`git-relay/bin/git-relay` runs straight from a checkout with no install step.  Stdlib only, no
dependencies to install.

## Seat provisioning and deployment

Seat bearer tokens are minted and delivered out of band (Infisical); rotation is a config change
plus restart. Deployment (host choice, process supervision, network reachability for seats) is a
separate decision; nothing here installs daemons or touches the operator broker.

## Tests

```
cd git-relay && python3 -m unittest discover -s git_relay/tests -t . -v
```

Fakes only: no network, no real token.

### Guarded same-path file patches

`wf_file_patch` is a separate operation, not an exemption for whole-file writes.
It accepts up to 32 sorted, disjoint UTF-8 byte-offset edits against a server-fetched
file bound to repository, path, branch head and verified Git blob SHA-1. Every caller
string, including inserted text, remains normally scanned. The full reconstructed
file is scanned too: dedicated token patterns, provider tokens and loaded bearer
values are refused even when present in the base. Only generic assigned-secret and
assigned-credential matches can retain baseline provenance. Their entire containing
line must map to a contiguous, identical original interval. Inserted/copied/replaced
bytes never inherit provenance, even if identical text occurs elsewhere.

Residual risk is explicit: a pre-existing generic assignment may be a real secret
and remains in that same source file. This operation does not certify such content
as secret-free. It cannot relocate the content or admit a newly assembled match.
Security fixtures with dedicated token patterns still require reviewed attestations.

The target must be a non-executable regular file (100644) on a nondefault branch,
not a workflow path. GitHub GraphQL `createCommitOnBranch` atomically checks
`expectedHeadOid`, creates the commit and advances the branch under the same own-App
contents:write credential. FileAddition carries the full server-reconstructed base64
content. Its API cannot preserve other file modes, so those are refused, not changed.
The receipt must bind the sole parent, branch, new head and exact published blob.
Unverifiable post-write receipts/GraphQL partial errors are UNKNOWN and must not be
blindly retried. Registration, least-privilege seat policy and rollout are separate
from source merge. Mutation docs: https://docs.github.com/en/graphql/reference/commits
and file input docs: https://docs.github.com/en/graphql/reference/git .

### Guarded patch bootstrap closure

The existing canonical seat policy predates `wf_file_patch`. The pure
`git_relay.patch_policy.prepare` transform appends only that action to each
existing file-write seat, after matching the full policy digest. It changes no
seat, App, repository, mint permission, private field or existing action order.
Its regression uses the actual canonical policy and proves exact delta,
idempotence, stale-input refusal and no input mutation. It performs no I/O.

Activation must put the reviewed code and this exact transformed live policy in
one bounded administrative deployment transaction with quiescence, pinned
source/live digests, durable before-images, atomic writes and verified recovery.
The old env-only seat installer does not qualify the live seats-file route.
Code-only live/action-denied activation is not completion. No activation is
implemented or implied by the pure transform.

Canonical consolidation remains owned by this change: after successful own-App
patch qualification, apply only the three existing profile action additions and
update the existing canonical digest regression to its computed new digest,
then verify rendered canonical policy equals the transformed policy. Until that
lands, this bootstrap transform and its durable tests record the exact delta.
