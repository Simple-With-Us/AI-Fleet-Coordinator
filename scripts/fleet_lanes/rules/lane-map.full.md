## Lane Map: where every checkout lives (owner 2026-10-07)

Full text: docs/protocols/lane-map.md in AI-Fleet-Coordinator.  Every git checkout has one of four homes:

- ~/Code/<App> is the human's integration tree.  Never edit it.
- ~/apps/lanes/<prefix>/<seat>-<slug> is your task lane, a linked worktree of that tree.  Its branch is your branch prefix plus the slug (claude/fix-thing, minimax/fix-thing, ag/fix-thing).
- ~/apps/lanes/_review/<prefix>/pr-<n> is a read-only check of someone else's PR.
- A folder a tool manages for itself (~/apps/lanes/_managed/..., or the tool's own default worktree folder).

Never clone a fleet repo, or add a worktree of one, in /tmp, /private/tmp, /var/tmp, $TMPDIR or /var/folders.  Scratch files and throwaway test repos there are fine.

Create a lane with `~/apps/lane new <app> <slug>`.  It needs AGENT_SEAT set to your seat tag; if it is unset, ask and never guess your seat.  Folder names use the whole seat name (antigravity, minimax, grok-build), never the short ag or mm.  Old flat lanes (~/apps/<prefix>-<seat>) stay where they are until they retire; do not create new ones.
