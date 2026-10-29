# git-relay - the GitHub relay CLI, and GR-MCP

git-relay knows each seat from its bearer token and picks the credential by task:  Opening a PR
runs under the owner's scoped PR-only token (the only copy lives in the service's
environment), and every other GitHub task runs under the seat's own GitHub App token, which
git-relay mints and refreshes from App keys in Infisical.  Every call is checked against per-seat
policy and written to the audit log, allowed or denied.

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

Deployed on the fleet server as `git-relay.service`, run from `/opt/git-relay` and bound to
127.0.0.1:8471, behind its own Cloudflare tunnel (`cloudflared-git-relay.service`) and an Access
service token on the public hostname.  The App keys live in Fleet-OPS `/github-apps` and the relay
mints every seat's App token itself.

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
audited Infisical write), and every call - allowed or denied - is in the audit log.

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
constant-time over all seats. A per-seat hourly call cap (`GIT_RELAY_RATE_PER_HOUR`, default 120)
limits a leaked seat token's blast radius.

### The reviewed seat policy file

[deploy/seat-policy.json](deploy/seat-policy.json) is the production policy.  It holds no secret:
every seat is `auth: shared` and signs in with its own admin-minted bearer.  It lists one entry
for each of the 38 identities the Worker can mint a bearer for, each acting as the `swu-` App
whose GitHub description names that seat.  Three slugs are not `swu-<identity>`:  MA is
`swu-rob`, GROK is `swu-grok-build` and MM is `swu-minimax`.  JET-CHAT has no entry, because it
is a second login on Jet's bot and one bot has one App (Q7).  Seats share the `fleet` profile
(the same actions, repositories and permission ceiling);  Echo keeps its own narrower repository
set.  `git_relay/tests/test_seat_policy_file.py` holds the file to the relay's SeatPolicy, the
Worker's identity lists and each action's permission needs.

On the relay host, as root.  Root runs only a root-owned copy that only root can write, never
the files in a checkout another account can change.  So copy the two files from the merged
commit first, and check that the blob ids the script prints equal `git rev-parse
<commit>:git-relay/deploy/seat_policy.py` and the same for `seat-policy.json`:

```
install -d -m 0755 /opt/git-relay/deploy
install -m 0644 git-relay/deploy/seat_policy.py git-relay/deploy/seat-policy.json /opt/git-relay/deploy/
python3 -I /opt/git-relay/deploy/seat_policy.py install                          # validate with the deployed code, diff, digest
python3 -I /opt/git-relay/deploy/seat_policy.py install --apply --expect DIGEST  # back up, write, restart, check health
python3 -I /opt/git-relay/deploy/seat_policy.py verify-apps                      # every seat's own App mints read-only and reads
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

`mint_token` returns a raw App installation token, so it is NOT exposed over the MCP wrapper
(a stdio transcript is too easy to capture);  seats that need a token for git-over-HTTPS call the
relay's HTTP API directly, where the response goes only to that caller.

## Audit

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

Request bodies are capped at 64 KiB, text fields at 32 KiB, GitHub responses at 1 MiB, and every
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
| GIT_RELAY_AUDIT_PATH | git-relay-audit.jsonl | Audit log location |
| GIT_RELAY_OUTCOMES_PATH | git-relay-outcomes.json | Write idempotency outcome store |
| GIT_RELAY_HOST | 127.0.0.1 | Bind address |
| GIT_RELAY_PORT | 8471 | Bind port |
| GIT_RELAY_RATE_PER_HOUR | 120 | Per-seat hourly call cap |
| GIT_RELAY_APP_MINT | 1 | Set to 0 to disable App actions and all App minting |
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
