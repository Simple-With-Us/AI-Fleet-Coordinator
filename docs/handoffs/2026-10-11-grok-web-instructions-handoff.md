# Grok Web/iOS Instructions Handoff (2026-10-11)

From:  CLAUDE (cloud session, stopped at weekly quota). Lane:  condense the `GROK-WEB` custom instructions.

## Context
- grok.com / Grok iOS enforces a **4,000-character limit** on custom instructions;  the prior text was 9,110 characters and silently truncated, dropping the Zulip and work-item procedures Grok Web must follow.
- Business rule:  Grok Web (a fleet seat, not the Mac terminal) must read the Zulip inbox, post in work topics, and pick up Linear work items, without depending on a Mac shell or `agent-sync` CLI it cannot reach.
- Acceptance criteria:  instructions fit ≤4,000 chars;  a fresh Grok session still reads Zulip and opens work topics;  the rules survive even if a skill file fails to load (repeated in a "Never Skip" block).

## Done
- Owner condensed the grok.com/iOS custom instructions from 9,110 to 3,997 characters (limit 4,000).  Procedure moved into two Grok skills;  a "Never Skip" section repeats the critical skill rules so they hold if a skill is not loaded.
- Recorded in `docs/grok-web/` (merged in #465, `6148255`):  `INSTRUCTIONS.md` (owner's final text, verbatim), `skills/grok-web-zulip/SKILL.md`, `skills/grok-web-work-items/SKILL.md`, `README.md`.
- Owner uploaded the two skills on grok.com (Skills › New Skill › Upload skill file).

## Decisions / Trade-offs
- **Why two skills rather than one.**  `grok-web-zulip` (read inbox, post in work topics) is used on every fresh session, while `grok-web-work-items` (claim, branch, close out) is only relevant when starting or closing a unit.  Splitting them lets the model load only the procedure it needs for the current task;  a single combined skill would force both procedures into every session's context window and reduce the chance either gets read carefully.  Trade-off:  the owner must keep two skill descriptions in sync and remember both when triaging.
- **Why move the procedure into skills at all.**  The 4,000-character instruction budget was dominated by procedure text, leaving no room for the safety rules (Zulip-only, no Mac shell, post in work topics).  Skills load lazily only when the model judges them relevant, so moving procedures out freed the instructions to hold only critical reminders and identity.  Trade-off:  any rule that lives only in a skill can be silently skipped when the skill does not load — the "Never Skip" block is the mitigation, at the cost of more characters in the always-loaded text.
- **What trade-off falls if a skill fails to load.**  The model loses the detailed procedure (channel ids, claim commands, closeout steps) but retains the critical rules from "Never Skip."  Operationally, a Grok session can still read the Zulip inbox and open a work topic, but claim/closeout steps may be approximated.  Mitigation cost:  the "Never Skip" block spends roughly 400 of the 3,997 characters;  removing it to gain headroom would re-introduce silent-skip risk.  Reversibility:  re-uploading the skill on grok.com is one click, and `docs/grok-web/INSTRUCTIONS.md` can be edited in the same PR.

## Open
1. **Remove two existing Grok skills** if the owner has not:  `sentence-gap` (tells Markdown panes to type the `&nbsp;` entity, which Grok Web must not show) and `fleet-coordination` (assumes a Mac shell;  its Grok copy is also stale, description "Enforce Jay Wedgeworth…").
2. **Verify skill loading:**  in a fresh Grok chat, ask it to read the Zulip inbox and confirm it loads `grok-web-zulip`. If not, move the budget and read-routine lines back into the instructions (3 characters of headroom, so something else must shrink).
3. **Closeout not done:**  no Zulip post in `#agent-sync` and no Linear issue or effort-log entry;  the cloud session had no Zulip credentials or `agent-sync` CLI. A Mac seat should post `DONE` and record it in Linear (the fleet has since cut over from THE BOARD).
4. **Optional:**  add `GROK-WEB` as a seat in `scripts/install-fleet-skills.py` / `scripts/fleet_skill_identity.py` so `docs/grok-web/skills/` is generated with the rest of the pack instead of maintained by hand. Needs a claim;  touches several files.
5. **Sync rule:**  when the owner edits the instructions on grok.com, update `docs/grok-web/INSTRUCTIONS.md` in the same change.

## Notes
- The recorded instructions contain the owner's double space in "Don't  print";  kept verbatim.
- Session env lacked `GH_PAT` and Zulip vars (env edits apply to new sessions only);  pushes worked after the owner reconnected the GitHub connector.
