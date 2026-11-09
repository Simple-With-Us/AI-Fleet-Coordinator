# 2026-09-03 — Owner copy: Title Case chrome, never display `&nbsp;`

Type: Fleet change

## Why

Owner, in the ContactLogo.com BotFleet room on Thu, Sep 3, 2026 at 8:57 PM CT:

Title Case for buttons, headings, and titles.  Sentence case (or lowercase when
not a full sentence) otherwise.  Two spaces between sentences everywhere.
The six characters `&nbsp;` must never appear on screen in answers, messages,
ContactLogo.com, Bot settings, routine settings, or any other owner-visible
surface.  Backend may insert a real U+00A0 to keep the gap.

## What landed (docs)

- `FLEET-UI-COPY.md` — binding list now every app; headings section covers Bot
  settings and routine settings; 2026-09-03 never-display-entity rule.
- `skills/sentence-gap/SKILL.md`, `skills/owner-copy/SKILL.md`,
  `docs/SENTENCE-GAP-PORTABLE-SKILL.md` — BotFleet/cloud row uses two ASCII
  spaces; entity only on surfaces that expand it so the owner never sees it.

## What did not land (product)

- ContactLogo.com landing headings/buttons still sentence case.  Board
  `62acf520`, routed to Designer.
- BotFleet JSX/settings/iOS still need a real NBSP helper so the entity cannot
  leak as text.  Desktop `ChatMarkdown` already maps the entity for bot bubbles.
- No TestFlight.  Stop on ContactLogo 1.0.3 still in effect.

## Verification

- Docs-only.  `python3 scripts/check-fleet-registry.py` not required (no
  registry edit).
- Live Mac copy: `~/apps/FLEET-UI-COPY.md` mirrored from this tree.

## Follow-ups

- Designer: ContactLogo.com `62acf520`.
- Builder: BotFleet renderer + remaining settings copy.
- Oracle: propagate into remaining skill installs.
