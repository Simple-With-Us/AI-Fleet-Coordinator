# GHR - the GitHub relay (ghr/)

One scoped GitHub token, narrow audited PR actions for fleet seats. Seats never hold the token:
they authenticate to this service with their own bearer tokens and call four actions; the service
holds the only copy of the GitHub token in its environment, checks each call against per-seat
policy, and writes an audit row for every call, allowed or denied.

This is deliberately separate from the OPS-966 operator broker: that stays disabled and
unwired; this package is its own service with its own token and its own (much smaller) surface.

## Actions

| Action | Endpoint | Body | GitHub call |
| --- | --- | --- | --- |
| open_pr | POST /v1/open_pr | repo, base, head, title, body | POST /repos/{repo}/pulls |
| edit_pr | POST /v1/edit_pr | repo, number, title?, body? | PATCH /repos/{repo}/pulls/{number} |
| reply_thread | POST /v1/reply_thread | repo, number, comment_id, body | POST /repos/{repo}/pulls/{number}/comments (in_reply_to) |
| comment_pr | POST /v1/comment_pr | repo, number, body | POST /repos/{repo}/issues/{number}/comments |

`GET /healthz` needs no auth. Everything else needs `Authorization: Bearer <seat token>`.

PRs and comments are authored as whoever the GitHub token belongs to (a scoped fine-grained PAT
on the owner's account, PR write only). Seats cannot push, merge, or resolve threads through this
service; those stay with the swu-echo app.

## Scope and action catalog (owner direction 2026-10-21)

The relay covers every GitHub task seats need.  It knows the seat from its bearer token and picks
the credential BY TASK:  PR-write authoring (which must be the owner's identity so Kody reviews it,
and which bot Apps can no longer do since the PR:read change) uses the owner's scoped PR-only token;
everything else uses the seat's OWN GitHub App token, which the relay mints and refreshes itself
from App keys in Infisical - seats never handle an App id, private key, or the 1-hour token expiry.
GITHUB_UNIVERSAL_PAT is not used by anyone.  Default deny:  an action is callable only by seats
explicitly allowlisted for it, and App actions only within the seat's mint_permissions ceiling.
Actions are catalogued even where no seat is allowlisted yet;  granting a seat later (Echo included)
is only a policy entry.

| Action | Credential | Status |
| --- | --- | --- |
| open_pr | owner PR-only token | implemented |
| edit_pr (title/body only) | owner PR-only token | implemented |
| reply_thread (inline review reply) | owner PR-only token | implemented |
| comment_pr | owner PR-only token | implemented |
| mint_token (per-repo, per-permission) | seat's App, relay-minted | implemented |
| merge_pr (squash/merge/rebase) | seat's App (contents+pull_requests write) | implemented |
| workflow_dispatch | seat's App (actions write) | implemented |
| resolve_thread | seat's App (pull_requests write) | implemented |
| create_release, rerun_workflow, cancel_workflow, set_labels, create_issue_comment, request_reviewers, set_branch_protection, create_repo | seat's App | registered, refused until implemented |

App-token minting:  GH_APP_<SLUG>_ID / GH_APP_<SLUG>_PRIVATE_KEY read one named key at a time from
Infisical (INFISICAL_CLIENT_ID / _SECRET / _WORKSPACE_ID env, prod /github-apps), JWT signed with
the local openssl binary, installation tokens cached in memory and refreshed 5 minutes early.
Nothing secret is written to disk, logged, or audited;  audit rows hash token values.

## MCP wrapper

`ghr/mcp_server.py` is a stdio MCP server (initialize, tools/list, tools/call) that
exposes the relay actions as MCP tools. A seat runs it with only its own bearer token
(PR_RELAY_SEAT_TOKEN, PR_RELAY_URL); the wrapper forwards over HTTP, so policy, caps and audit
stay server-side. Refusals come back as tool errors, never protocol errors.

## Relay as a permission layer (owner direction 2026-10-21)

Per-seat policy (actions, repos, mint_permission ceilings, rate caps) is a first-class permission
layer of its own:  it can narrow what a credential allows, never exceed it.  A seat's effective
power is the INTERSECTION of the relay policy and the underlying GitHub credential - tightening
either side restricts the seat, and no policy entry can grant what GitHub already denies.  Policy
lives in reviewed config (PR_RELAY_SEATS_JSON sourced from a reviewed file or Infisical), so
permission changes ship without touching GitHub App settings;  every change is a reviewed PR (or an
audited Infisical write), and every call - allowed or denied - is in the audit log.

## Policy

Seats come from `PR_RELAY_SEATS_JSON` at process start:

```json
{
  "swu-echo": {
    "token": "<bearer, 16+ chars>",
    "actions": ["open_pr", "edit_pr", "reply_thread", "comment_pr"],
    "repos": ["Simple-With-Us/AI-Fleet-Coordinator", "Simple-With-Us/*"]
  }
}
```

Every check is an allowlist: an action or repo not named is denied. Token comparison is
constant-time over all seats. A per-seat hourly call cap (`PR_RELAY_RATE_PER_HOUR`, default 120)
limits a leaked seat token's blast radius.

## Audit

One JSON object per line at `PR_RELAY_AUDIT_PATH` (default `pr-relay-audit.jsonl`), fsynced after
every write: timestamp, seat, action, repo, result (ok / denied / bad_request / github_error),
and the GitHub id/URL returned. Titles and bodies are never logged; the row keeps their length
and SHA-256 so a call is attributable without storing text that might contain a pasted secret.
The GitHub token is never logged or returned.

## Bounds and failure shape

Request bodies are capped at 64 KiB, text fields at 32 KiB, GitHub responses at 1 MiB, and every
GitHub call has a 30 s timeout. Errors: 401 unauthenticated, 403 policy denial, 400 bad request,
502 GitHub failure (upstream status included, upstream body is not).

## Configuration and running

| Env var | Default | Purpose |
| --- | --- | --- |
| GITHUB_TOKEN | (required) | Scoped PR-only token; the only credential the service holds |
| PR_RELAY_SEATS_JSON | (required) | Seat tokens, action allowlists, repo allowlists |
| PR_RELAY_AUDIT_PATH | pr-relay-audit.jsonl | Audit log location |
| PR_RELAY_HOST | 127.0.0.1 | Bind address |
| PR_RELAY_PORT | 8471 | Bind port |
| PR_RELAY_RATE_PER_HOUR | 120 | Per-seat hourly call cap |

```
python3 -m ghr.server
```

Stdlib only; no dependencies to install.

## Seat provisioning and deployment

Seat bearer tokens are minted and delivered out of band (Infisical); rotation is a config change
plus restart. Deployment (host choice, process supervision, network reachability for seats) is a
separate decision; nothing here installs daemons or touches the operator broker.

## Tests

```
python3 -m unittest discover -s ghr/tests -t . -v
```

Fakes only: no network, no real token.
