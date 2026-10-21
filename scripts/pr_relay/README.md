# pr_relay

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

## Expansion: all of GitHub, tiered (owner direction 2026-10-21)

The relay grows from PR actions to every GitHub action seats need, so seats stop holding App
ids/secrets and stop handling 1-hour installation-token expiry: the relay mints and refreshes
tokens itself. Non-PR actions are registered in policy but REFUSED until the owner picks the
backing credential per tier; nothing holds GITHUB_UNIVERSAL_PAT or any wider token yet.

| Tier | Actions | Backing credential | Status |
| --- | --- | --- | --- |
| 1 | open_pr, edit_pr, reply_thread, comment_pr | Owner's scoped PR-only token (env GITHUB_TOKEN) | live |
| 2 | push, merge_pr, workflow_dispatch | Relay-held App key per seat app (Infisical); relay mints + caches installation tokens (1 h, refresh 5 min early) | registered, disabled |
| 3 | repo_admin | Owner-chosen admin credential, TBD | registered, disabled |

Relay-wide enable switch: PR_RELAY_ENABLED_ACTIONS (comma list) intersects with each seat's own
allowlist; both must name an action, and a tier without an implementation fails closed even when
enabled. Per-seat policy, rate caps and the audit log apply unchanged to every tier.

## MCP wrapper

`scripts/pr_relay/mcp_server.py` is a stdio MCP server (initialize, tools/list, tools/call) that
exposes the relay actions as MCP tools. A seat runs it with only its own bearer token
(PR_RELAY_SEAT_TOKEN, PR_RELAY_URL); the wrapper forwards over HTTP, so policy, caps and audit
stay server-side. Refusals come back as tool errors, never protocol errors.

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
cd scripts && python3 -m pr_relay.server
```

Stdlib only; no dependencies to install.

## Seat provisioning and deployment

Seat bearer tokens are minted and delivered out of band (Infisical); rotation is a config change
plus restart. Deployment (host choice, process supervision, network reachability for seats) is a
separate decision; nothing here installs daemons or touches the operator broker.

## Tests

```
cd scripts && python3 -m unittest discover -s pr_relay/tests -t . -v
```

Fakes only: no network, no real token.
