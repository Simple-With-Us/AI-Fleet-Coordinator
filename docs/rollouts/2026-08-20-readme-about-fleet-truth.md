# 2026-08-20 — README / About match how the fleet works

## Context & Objective

GitHub About for `AI-Fleet-Coordinator` was empty.  The README still described a
generic “autonomous multi-agent framework.”  The live fleet is Mac collab
(`mac.jays.services`), Shellular, `#agent-sync`, and coding seats in
`fleet-apps.json` — with **no app-specific Grok Bot seats**.

## Changes Made

Docs and repo metadata only.  No product code.

- `README.md` — how the fleet works now; apps/seats table; THE BOARD +
  Shellular + agent-sync; explicit “no per-app GROK-BOT lanes.”
- `AGENT-SYNC.md` — Overview looks at THE BOARD first; `GROK-BOT` row in the
  seat table; Available list; THE BOARD identity note.
- `EFFORT-LOG-PROTOCOL.md`, `TEMPLATE-AGENTS.md`, both ONBOARDING docs,
  `docs/MAC-LOCAL-PROCESSES.md`, `fleet-apps.json` notes — same picture.
- GitHub About description + homepage (repo metadata, not this commit).

## Decisions & Trade-offs

- Did **not** add `GROK-BOT` to `fleet-apps.json` `seats[]`.  That list is
  coding-lane inventory (worktree suffix + branch prefix).  Grok Bot drives
  Cursor cloud and has no per-app lane.
- Did **not** invent a `GB-*` roster.  AGENT-SYNC only says operational Slack
  tags may use a `GB-` prefix and those are not per-app lanes.
- Homepage set to the public digest site.  THE BOARD is auth-gated.

## Verification State

- `python3 scripts/check-fleet-registry.py` (must exit 0).
- `gh repo view Simple-With-Us/AI-Fleet-Coordinator --json description,homepageUrl`

## Next Steps & Blockers

None.  Live `~/apps/AGENT-SYNC.md` should stay aligned when a Mac seat can
copy this Overview / seat-table / THE BOARD wording.

## Zero-Code Findings

README Core Concepts never mentioned mac-collab, Shellular, or GROK-BOT.
`fleet-apps.json` seats were already the accurate coding-lane list.
