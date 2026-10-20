# 2026-10-20 — MiniMax Code pane: a real U+00A0, not the entity

Refs AFC-421, PR #733.

## Summary

`#732` landed the MiniMax Code row in the `sentence-gap` pack and the portable doc while this
branch was open.  This adds the same row to the five surfaces that still told a seat to use
the `&nbsp;` entity in any Markdown chat pane, and re-renders the skill trees.  The only
canonical file edited here is `FLEET-UI-COPY.md`, and only its sentence-gap row:  the rest of
its drift from the live `~/apps` copy is deliberately left for the owner.

## Why

Owner ruling 2026-10-20:  in the MiniMax Code chat pane the wider sentence gap is a real
U+00A0 followed by a normal space.  Two ASCII spaces collapse there, and the `&nbsp;` entity
renders as the six literal characters, which is worse than no gap at all.  The 2026-10-18
entry for this pane, entity plus a space, was wrong.

Claude Code keeps the entity in its Code tab (owner-verified 2026-10-08 with a screenshot);
the two panes differ, so the catalog, the coordination table, the land-lane prose line and the
Cursor rules file each needed the MiniMax case pulled out of the generic Markdown-pane
default.

## Files

Hand-edited surfaces:

- `skills/sentence-gap/SKILL.md` — the catalog `description:`, the first line a seat reads
  when it picks the skill, now names the MiniMax mechanism and drops the retired Monet
  attribution.
- `skills/fleet-coordination/SKILL.md` — the per-surface table gains a MiniMax Code row.
- `skills/land-lane/SKILL.md` — the prose line now picks the mechanism by pane.
- `.cursor/rules/sentence-gap.mdc` — separates MiniMax's verified behaviour from the other
  Markdown panes and keeps the report-it rule.
- `FLEET-UI-COPY.md` — the tracked mirror, sentence-gap row only.

Generated, by `python3 scripts/install-fleet-skills.py --repo-only`:

- 81 rendered skill files, being the three skills `sentence-gap`, `land-lane` and
  `fleet-coordination` across 27 trees:  9 repo-tracked platform copies (`.claude`, `.clutch/dsh`,
  `.codex`, `.config/muse`, `.cursor`, `.fx`, `.gemini`, `.grok`, `.minimax`), 16 seat upload
  packs under `docs/fleet-skills/by-seat/`, the canonical `docs/fleet-skills/`, and `skills/`.
- The three `docs/fleet-skills/*.zip` upload packs, re-packed by the same command.

Also changed:  `scripts/test_fleet_skill_identity.py`, whose `test_cursor_is_not_monet`
asserted on the retired Monet attribution that this change removes;  it now asserts the
generic protocol name and the absence of `Monet`.

## Verification commands actually run

All three run from the repo root at `4d230726` on Linux, working tree clean before and after.

```console
$ python3 scripts/install-fleet-skills.py --repo-only
Found 21 installable fleet skills in docs/fleet-skills:
  ...
Repo-tracked platform copies:
  CURSOR        .cursor/skills  (21 skills)
  ... 9 platform copies
Upload packs: docs/fleet-skills/by-seat/<seat>/
  ... 16 seats
$ echo $?
0
```

21 installable skills, 9 platform copies, 16 seat packs, exit 0.  `git status --porcelain`
was empty afterwards, so the committed trees match a fresh render rather than drifting from
it.

```console
$ cd scripts && python3 -m unittest test_fleet_skill_identity
.................................................................................
----------------------------------------------------------------------
Ran 81 tests in 2.150s

OK
```

```console
$ python3 scripts/check-fleet-registry.py
note: live copy checks skipped (no FLEET_BOARD_HOME, /home/runner/apps has no AGENT-SYNC.md)
fleet registry check OK (17 apps)
```

Exit 0.  The `note:` line is expected on this runner:  the live-copy half of that check reads
the Mac board home, which does not exist here, so only the tracked registry half ran.

Byte-level check that each packed upload is current, since a stale zip would ship the old row
regardless of what the tree says:

```python
import zipfile, pathlib
for name in ("sentence-gap", "land-lane", "fleet-coordination"):
    z = zipfile.ZipFile(f"docs/fleet-skills/{name}.zip")
    entry = [n for n in z.namelist() if n.endswith("SKILL.md")][0]
    assert z.read(entry) == pathlib.Path(f"docs/fleet-skills/{name}/SKILL.md").read_bytes()
```

All three equal.

Not run here:  the live-copy assertions in `check-fleet-registry.py`, and the live
`~/apps/AGENT-SYNC.md` sync, both of which need the Mac.

## Follow-ups

- `FLEET-UI-COPY.md` still drifts from the live `~/apps` copy in the first-visit theme default
  and the date-time lines.  That needs the owner rather than a silent sync and is not in this
  pull request.
- Fleet-OPS #152 regenerated `generated/AGENT-SYNC.md` after its build check had been failing,
  which is why the live `~/apps/AGENT-SYNC.md` kept the old row.  The sync job has since applied
  it.
