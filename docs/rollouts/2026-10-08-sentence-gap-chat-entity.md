# 2026-10-08 — Sentence gap: the chat entity returns, GitHub and Zulip use U+00A0

Type: Fleet change  (board `86aa2176`)

## Why

Owner, Thu, Oct 8, 2026:  "please fix it for every chat … and make the whole world do the
same if you can."  The owner wants a visibly wider gap, two spaces, between sentences on
every surface, and the gap has to survive the renderer.

Verified the same day:

- Chat UIs that render Markdown collapse two typed ASCII spaces into one.  That includes
  the Claude Code desktop app's Code tab and most agent chat panes.
- A model cannot reliably emit a raw U+00A0 in a chat reply.  It arrives as a plain space.
  The verified reply held 0 NBSPs.
- Typing the HTML entity `&nbsp;` followed by a normal space after each sentence, outside
  code spans, works in the Code tab.  The renderer decodes it.  The owner confirmed it with
  a screenshot.

This reverses the 2026-09-04 ruling that put two ASCII spaces in the Code tab.

## The rule, per surface

| Surface | Mechanism |
| --- | --- |
| Claude Code desktop app, Code tab (chat replies) | `&nbsp;` plus a normal space, outside code spans.  Owner-verified 2026-10-08. |
| Any other Markdown-rendering agent chat pane (Codex, Cursor, Antigravity, Grok, Kimi, MiniMax, DeepSeek, Fx, Muse) | The same.  By owner ruling, not individually verified. |
| Terminal TUI chat (Claude Code CLI, Grok TUI, Codex CLI, opencode, kimi-code, mcode) | Two ASCII spaces.  Unverified. |
| Cloud, BotFleet and OpenMausBot chat | Two ASCII spaces.  The backend maps them to U+00A0. |
| GitHub PR and issue titles, bodies and comments, review comments; Zulip posts | A real U+00A0 plus a space.  Never the entity: GitHub can copy a PR body into a plain-text squash commit, where it would show literally. |
| Commit messages, source files, repo docs read as source, terminal output, Slack | Two ASCII spaces. |
| HTML, JSX, SwiftUI product copy; Apple Notes `--html` | A real U+00A0 plus a space, or `SENTENCE_GAP`. |

The owner must never see the six characters `&nbsp;`.  If a surface shows them, stop using
the entity there, report the surface in #agent-sync, and treat it as unknown until it is
tested.  When a surface needs a mechanism to show two spaces, use it without asking.

Producing U+00A0 through a tool:  write two ASCII spaces after each sentence, then run
`perl -CSDA -pe 's/([.!?])  (?=\S)/$1\x{a0} /g' body.txt > body.nbsp.txt`, and check the
output holds a U+00A0 before `gh pr create --body-file` or `agent-sync post`.

## What landed (docs, in this PR)

- `AGENT-SYNC.md` § Sentence Gap (the authoritative per-surface table), the Zulip summary
  line, the Cursor and load-path notes, the skills catalog row, and the history entry.
- `FLEET-UI-COPY.md`, `TEMPLATE-AGENTS.md`, `docs/protocols/zulip-fleet-guide.md`
  (Writing Rules), and the retired-row text in `docs/protocols/agent-sync-rewrite-map.md`.
- `docs/SENTENCE-GAP-PORTABLE-SKILL.md` and the canonical pack skills `sentence-gap`,
  `owner-copy`, `fleet-coordination`, `land-lane`, `codex-triage`, re-rendered into
  `skills/`, `.claude/skills`, `.cursor/skills`, `.grok/skills`, and every
  `docs/fleet-skills/by-seat/*` pack with `scripts/install-fleet-skills.py --repo-only`.
- `.cursor/rules/sentence-gap.mdc` regenerated from the pack block.
- `scripts/fleet_rag/golden_multihop.jsonl` item A2 now expects `owner-verified 2026-10-08`.

## Not covered, owner or follow-up

- The Cursor Settings › Rules user rule "Sentence gap — two visible spaces" is an owner UI
  action.  It is not a file.
- Chat surfaces other than the Code tab are unverified.  A tester should run the
  self-test in the `sentence-gap` skill on each and record the answer in its table.
- `scripts/agent_sync/wake/wake-contract.md` still says "Two spaces between sentences".
  The wake responder is model-written text posted by the daemon, so it cannot emit U+00A0.
  A daemon-side conversion in `agent-sync post` would make the Zulip rule automatic.
- Several tests in `scripts/test_fleet_skill_identity.py` already fail on main (15 of 49).
  This change adds none.
