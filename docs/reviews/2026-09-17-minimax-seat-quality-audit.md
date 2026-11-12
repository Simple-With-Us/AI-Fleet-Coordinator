# MiniMax (`MM`) seat quality audit — honest snapshot

Thu, Sep 17, 2026.  Seat: GROK.  Board `aeab69fc` (owner request 2026-09-07, CLAUDE planned, AG claimed, no work started until this pass).

This is a **fleet-integration** audit, not a model-quality eval.  I did not run MiniMax Code, score generation, or sit a MiniMax session.  Claims below are from the Mac seat surface, THE BOARD, and merged AFC PRs.

## What is in place

- Runtime dir `~/.minimax` exists.  `memory/user.md` is present (the documented fleet pointer).  `skills/` has 23 entries.
- Seat identity is documented: Slack `[MM]`, branch prefix `minimax/`, `AGENT_SEAT=MM`.  Former tag `MINIMAX` is retired (AFC #194, 2026-09-05).
- Onboarding PRs on AFC are merged: #185 (twelfth seat), #187 (recall slot), #194 (MM/DSH acronyms), #224 (digest icons).
- BotFleet MiniMax connectability and quota follow-ups from mid-September are already **completed** on the board (`0b92ea0b`, `e820540b`).  Those are BotFleet product lanes, not this AFC audit.

## Quality issues (observed, not guessed)

1. **Tag drift on live board rows.**  Several still-open effort-rows are authored `MINIMAX` (retired) rather than `MM`: Congress.Trade `04edf97a` (Senate relay), `8c1d702f` (residential proxy), BotFleet `c33279e2` (settings-rev2-C).  Writeback will keep reprinting the retired tag until those rows are corrected in place.  This is the most visible seat-quality miss on THE BOARD today.
2. **Doc vs disk on the rules file.**  `FLEET-PROTOCOL.md` and `ONBOARDING-NEW-AGENT.md` still say MiniMax has **no** global rules file and that the pointer lives only in `~/.minimax/memory/user.md`.  On this Mac, `~/.minimax/AGENTS.md` exists (chmod 600, dated Sep 4).  Either the doc is stale or that file is an unofficial extra.  Not opened here (may contain local config).  Follow-up: reconcile the doc with the file, or delete the file if it is leftover.
3. **`permissionMode: bypassPermissions` is still the documented default.**  Nothing prompts.  Destructive-ops pause is on the seat.  That is a quality risk for a bounded-implementation seat, not a bug in the product — it is working as designed and easy to over-run.
4. **Open product work is not this audit.**  BotFleet P1 `da75e2da` (HTTP-lane capability expansion for MiniMax / OpenAI-compat / Grok HTTP) is still open.  Do not treat this audit as closing that lane.

## What I did not check

- Live MiniMax session quality, Computer Use, `mmx` CLI generation, or citation faithfulness.
- Whether MiniMax honors fleet skills (sentence-gap, board-first, secret handoff) in an actual run.
- Channel YAML under `~/.minimax/` beyond confirming the files exist.

## Recommendation

Treat MiniMax as **onboarded and usable**, with two cheap hygiene follow-ups: retag the leftover `MINIMAX` board rows to `MM`, and reconcile `~/.minimax/AGENTS.md` vs the "no rules file" docs.  A real generation-quality audit still needs a MiniMax session and is **not** claimed done here.
