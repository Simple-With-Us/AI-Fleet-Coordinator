## Lane Map: where every checkout lives (owner 2026-10-09)

Full text: docs/protocols/lane-map.md in AI-Fleet-Coordinator.  <Repo> is the repo's folder name under ~/Code, written exactly as it is there (AI-Fleet-Coordinator, Congress.Trade, congress-trading-shared).  Every git checkout has one of these homes:

- ~/Code/<Repo> is the human's integration tree.  Never edit it, and never put a worktree inside it (no <repo>/.claude/worktrees).
- ~/apps/lanes/<Repo>/<seat>-<slug> is your task lane, a linked worktree of that tree.  Its branch is your branch prefix plus the slug (claude/fix-thing, minimax/fix-thing, ag/fix-thing).
- ~/apps/lanes/<Repo>/review-pr-<n> is a read-only check of someone else's PR (review-pr-<n>-<seat> when another seat already has that PR).
- A tool that names its own worktrees files them in the same tree: Claude desktop at ~/apps/lanes/<Repo>/<slug>-<hex>, Codex desktop at ~/apps/lanes/_codex/<slug>/<Repo>.  Their worktree location settings point at ~/apps/lanes and ~/apps/lanes/_codex.

Never clone a fleet repo, or add a worktree of one, in /tmp, /private/tmp, /var/tmp, $TMPDIR or /var/folders.  Scratch files and throwaway test repos there are fine.

Create a lane with `~/apps/lane new <app> <slug>`.  It needs AGENT_SEAT set to your seat tag; if it is unset, ask and never guess your seat.  Folder names use the whole seat name (antigravity, minimax, grok-build), never the short ag or mm.  Lanes in the old places under ~/apps/lanes (<prefix>/ such as fleet or trading, _managed, _review) stay where they are until the layout migration moves them, and flat ~/apps/<prefix>-<seat> lanes are not moved at all and retire normally; do not create new ones in any old place.
