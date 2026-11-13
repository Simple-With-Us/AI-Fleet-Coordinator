# AGENTS.md fleet-recall stanza (paste into every app)

Owner 2026-09-02. Paste the block below as-is; this note is not part of the stanza.  A real U+00A0 plus a normal space between sentences (owner ruling 2026-10-23), with one exception: the chat panes of Claude Code (the Code tab), MiniMax Code (the MiniMax Code pane) and Z Code (the Z Code pane) take the `&nbsp;` entity plus a normal space.

```
## Fleet recall

At the start of every turn, run one targeted Fleet Recall search (`recall "<topic>"` or MCP `recall_search`) before substantive action or reply, including continuations and mechanical tasks.  A hit is a lead, not authority: open its source before relying on it.  Use only the configured own-seat route; if retrieval is unavailable, record that limitation once and continue independent work without changing credentials, trust or services.

At the end of every turn, consider whether newly learned durable knowledge would help the fleet.  If appropriate, search for duplicates and contribute one useful paragraph with `recall contribute` or MCP `recall_contribute`; otherwise add nothing.  Do not force filler, require a public “no lesson” reply, duplicate existing knowledge, or dump transcripts or secrets.  Keep source facts in their existing home and contribute only the reusable lesson.

Canonical: AI-Fleet-Coordinator/docs/RAG-FLEET-INFRA.md.
```
