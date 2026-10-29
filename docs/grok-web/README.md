# Grok Web/iOS (`GROK-WEB`) Setup

Record of what is installed on grok.com and iOS Grok for the `GROK-WEB` seat.  This is not the Mac terminal Grok (`GROK`) pack in `docs/fleet-skills/by-seat/grok/`.

| File | Installed as | Loaded |
|---|---|---|
| `INSTRUCTIONS.md` | Grok custom instructions (4,000-character limit) | Always |
| `skills/grok-web-zulip/SKILL.md` | Skills › New Skill › Upload skill file | Before any Zulip or recall call |
| `skills/grok-web-work-items/SKILL.md` | Skills › New Skill › Upload skill file | Before claiming, handing off, or opening a PR |

History:  owner condensed the 9,110-character instructions on 2026-10-11.  Every-turn and safety rules (identity, copy, trust, secrets) stay in the instructions;  procedure moved to the two skills.  The "Never Skip" section repeats the most important skill rules on purpose so they hold even when Grok does not load a skill.

Keep the canonical `sentence-gap` and `fleet-coordination` skills off Grok Web:  `sentence-gap` is built around a Mac shell converter and `fleet-coordination` assumes a Mac shell.  Since the owner ruling of 2026-10-23 the gap is a real U+00A0 plus a normal space on every surface, Grok Web included, with two exceptions:  the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the `&nbsp;` entity plus a normal space.  The body of an Apple Note in the Coding folder takes the narrow no-break space (U+202F) plus a normal space, because Apple Notes normalizes a no-break space away on import (AFC-552).  The copy line in `INSTRUCTIONS.md` carries the same rule.

`INSTRUCTIONS.md` is the owner's final text, recorded verbatim.  When it changes on grok.com, update this copy in the same change.
