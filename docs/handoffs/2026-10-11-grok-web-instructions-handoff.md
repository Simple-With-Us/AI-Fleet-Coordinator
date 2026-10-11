# Grok Web/iOS Instructions Handoff (2026-10-11)

From:  CLAUDE (cloud session, stopped at weekly quota). Lane:  condense the `GROK-WEB` custom instructions.

## Done
- Owner condensed the grok.com/iOS custom instructions from 9,110 to 3,997 characters (limit 4,000).  Procedure moved into two Grok skills;  a "Never Skip" section repeats the critical skill rules so they hold if a skill is not loaded.
- Recorded in `docs/grok-web/` (merged in #465, `6148255`):  `INSTRUCTIONS.md` (owner's final text, verbatim), `skills/grok-web-zulip/SKILL.md`, `skills/grok-web-work-items/SKILL.md`, `README.md`.
- Owner uploaded the two skills on grok.com (Skills › New Skill › Upload skill file).

## Open
1. **Remove two existing Grok skills** if the owner has not:  `sentence-gap` (tells Markdown panes to type the `&nbsp;` entity, which Grok Web must not show) and `fleet-coordination` (assumes a Mac shell;  its Grok copy is also stale, description "Enforce Jay Wedgeworth…").
2. **Verify skill loading:**  in a fresh Grok chat, ask it to read the Zulip inbox and confirm it loads `grok-web-zulip`. If not, move the budget and read-routine lines back into the instructions (3 characters of headroom, so something else must shrink).
3. **Closeout not done:**  no Zulip post in `#agent-sync` and no Linear issue or effort-log entry;  the cloud session had no Zulip credentials or `agent-sync` CLI. A Mac seat should post `DONE` and record it in Linear (the fleet has since cut over from THE BOARD).
4. **Optional:**  add `GROK-WEB` as a seat in `scripts/install-fleet-skills.py` / `scripts/fleet_skill_identity.py` so `docs/grok-web/skills/` is generated with the rest of the pack instead of maintained by hand. Needs a claim;  touches several files.
5. **Sync rule:**  when the owner edits the instructions on grok.com, update `docs/grok-web/INSTRUCTIONS.md` in the same change.

## Notes
- The recorded instructions contain the owner's double space in "Don't  print";  kept verbatim.
- Session env lacked `GH_PAT` and Zulip vars (env edits apply to new sessions only);  pushes worked after the owner reconnected the GitHub connector.
