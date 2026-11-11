# Fleet RAG: a multi-hop and corpus-wide golden set — 2026-09-17

**Author:** Claude, owner-requested.  **Scope:** measurement only.  This adds one new file,
`scripts/fleet_rag/golden_multihop.jsonl`, and this doc.  `golden.jsonl`, `eval.py`, and every
retrieval code path are unmodified, and nothing here was run against the live box (a separate job
owns it right now; the coordinator runs this set afterward).

## Why this exists

The existing `golden.jsonl` (75 rows) is entirely single-hop lookups — "where does the vector DB
run," "which Coolify token deploys."  It cannot tell whether retrieval fails on a question that
needs two chunks connected through a shared identifier (a board row and the PR that resolved it;
an incident and the lesson it produced) or a question whose answer is spread across many chunks
("what keeps causing failed TestFlight ships").  Those are exactly the cases GraphRAG-style
techniques — an identifier link graph, query decomposition — are supposed to help with, so a golden
set that never asks them can't measure whether building either is worth it.

## How `expect_text_contains` is matched (read from `eval.py`, unchanged)

`matches(row, hit)` in `scripts/fleet_rag/eval.py` scores one **hit** at a time, where a hit is one
retrieved chunk (`recall_search` runs with `per_doc=1` by default, so in practice one chunk per
distinct `doc_id` in the result list — not a grouped, concatenated document).  A row counts as a
hit at rank *r* the first time a returned hit satisfies **every** expectation the row carries at
once: `expect_doc_id_prefix` (a `str.startswith` check on `hit["doc_id"]`), `expect_text_contains`
(a plain substring check — `needle.lower() in hit["text"].lower()` — against any one string in a
list, or the single string if it's not a list), and `expect_source` (exact match on `hit["source"]`),
whichever of the three are present on the row.  There is **no cross-hit or cross-chunk credit**: if
an answer genuinely needs two different chunks, the harness cannot express "credit if both of these
show up in the top-k" — it can only ask whether one specific hit contains one specific string.  This
governed every row below (see *Harness limitation* at the end).

## Method

Read `scripts/fleet_rag/experiments/.cache/fleet-agents-corpus.jsonl` (42,500 lines, one point per
line: `id`, `source`, `app`, `category`, `seat`, `doc_id`, `chunk_index`, `heading`, `title`, `url`,
`path`, `created_at`, `text`) streamed into a local, read-only SQLite index — never loaded wholesale
into context.  For every candidate question: find a real chain of 2-3 chunks connected by a shared
identifier (a board id, a PR number, a commit sha, a file/doc name cited by one chunk inside the
other) *first*, then write the question a person would plausibly ask about it in plain English —
never the reverse.  Identifiers were deliberately left out of the question text itself wherever a
real person asking it wouldn't know or use them (board ids, PR numbers); a couple of dates survive
in two questions (the sentence-gap reversal, the 2026-08-21 outage) because that's how the fleet
actually talks about those two specific incidents.

Every row's `expect_text_contains` was checked by an automated script against the SQLite copy,
replicating `matches()`'s exact rule (lowercase, substring, no regex) against the *specific*
`doc_id` the question is meant to resolve to — not just "appears somewhere in the corpus."  Text was
read at the raw-string level (`repr()`) before picking a needle, because several promising phrases
turn out to cross a real line-wrap `\n` inside the source markdown; a needle that spans one would
never match `hit["text"]`, which preserves those newlines.  Every chunk that was actually cited was
also read in full and scanned for anything that looks like a live credential before being used —
see *Secrets check* below.

## Group A — 16 multi-hop questions

Each connects 2-3 chunks across different `doc_id`s (mostly different sources: board, effort-log,
memory, apple-note, doc, skill, agent-contribution) via a shared board id, PR number, commit sha, or
an explicit cross-reference one chunk makes to the other by name.  `expect_text_contains` is always
the most discriminating string from the chunk that completes the *last* hop — the one a
single-hop-only retriever is least likely to surface for the question as phrased.  Full chains
(every `doc_id` and the linking identifier) are recorded per-row in `note`, not just summarized here.

Three examples:

1. **BotFleet APNs task-switch bug → what actually shipped.**  Query: *"What actually changed in
   the BotFleet fix for the bug where a background push notification could switch your active bot
   task without you tapping anything?"*  Chain: `board/39f7be5ceb2e44a5afb58d12cb50eb32` (the P1
   finding) → `effort-log/BOTFLEET` chunk 65 (`= board/e4d01a79790c421196587c60e000540b`, the
   completion row naming PR #312 / commit `cf51b991`), linked by board id `39f7be5c`.
   `expect_text_contains`: `"background APNs refreshes without navigation"` (only in the second hop).

2. **A ruling that reversed itself.**  Query: *"Does Claude Code's desktop chat still need the
   &nbsp; entity trick for the two-space sentence gap to actually show, or do two literal spaces
   work now?"*  Chain: `doc/local/apps/FLEET-UI-COPY.md` (2026-08-19, owner-verified: chat needs the
   entity, literal spaces "tested and confirmed NOT to work") → `doc/local/.claude/CLAUDE.md`
   (2026-09-04, owner-verified: desktop app Code tab now uses two literal ASCII spaces, and names
   FLEET-UI-COPY.md as the canonical detail doc).  Same question, opposite answer, later date — a
   genuine in-corpus reversal, not a hypothetical one.  `expect_text_contains`:
   `"owner-verified 2026-09-04"`.

3. **A board finding says a defect is new; a later doc says it isn't.**  Query: *"Was the finding
   that AI-Fleet-Coordinator itself has no CI or branch protection actually new when Claude's
   worktree-landing campaign flagged it, or had someone already filed it?"*  Chain:
   `board/c1160c9630f04eaaaa366b572b7e7ff3` (KIMI's original finding, filed 2026-08-20/21) →
   `doc/local/apps/HANDOFF-claude-2026-09-06-worktree-landing-campaign.md` chunk 14 ("the finding
   was NOT new... it has sat open for 17 days"), cross-linked in both directions (the board row's
   own follow-up comment cites the handoff doc by filename).  `expect_text_contains`:
   `"sat open for 17 days"`.

## Group B — 8 corpus-wide / thematic questions

Each names a specific set of supporting chunks in `note` (not "the whole corpus, generally") — most
span 3-4 distinct `doc_id`s across two or more sources.  `expect_text_contains` targets the single
chunk judged most likely to be the canonical answer among the supporting set (e.g., the operator
doc over an individual incident report), since the harness can only credit one hit.

Example: *"Why does Coolify keep showing 'running:unknown' for services like qdrant-st, and why
doesn't the fleet's iOS app just treat that as healthy?"* draws on `doc/local/apps/COOLIFY.md`
(the canonical status-string table), `contrib/MINIMAX/2026-09-04/56368c18` (why iOS deliberately
treats it as non-healthy), `board/315a676f97984504949e9458507d15b5` (the concrete qdrant-st
incident), and `memory/claude/code-socratic-trade/coolify-github-runner-ubuntu-mismatch.md` (a
second, unrelated cause of the same status string).  `expect_text_contains`:
`"healthcheck not reporting yet"`.

The other seven: recurring iOS/TestFlight ship blockers across four apps; what keeps burning the
fleet's one shared Backblaze B2 account; the real incidents behind the "never `cat` a secrets file"
rule; the 2026-08-21 total Mac outage and the pm2 mistakes that slowed recovery; why the sentence-gap
rendering answer keeps changing across docs and what an agent should do about that; why all-green
PRs still stall fleet-wide (`required_review_thread_resolution` on all 11 repos); and the pattern the
MiniMax seat-quality audit found across nine PRs.

## Rows dropped

**Zero** rows were written and then dropped for failing verification — every row's
`expect_text_contains` and every `doc_id` in its chain was confirmed against the corpus copy before
being added to the file.  Two candidate chains were investigated and set aside *before* being
written, for different reasons:

- A board row about a MiniMax session printing live Slack `xapp-`/`xoxb-` tokens into a transcript
  (board `3a317ef6`) was a strong, real incident for the Group B secrets theme, but the topic sits
  close enough to live-credential material that it was dropped in favor of chains with no
  token-prefix strings anywhere in the cited text, per the "skip anything that looks like a key"
  instruction — even though no actual secret value appears in that chunk.
- A planned Group B4 support chunk (the `CLAUDE.md` line about handoff-file values being
  quote-wrapped and needing `tr -d '"'`) is live in the *current* global `CLAUDE.md`, but is not
  present in this corpus snapshot's ingested copy (`doc/local/.claude/CLAUDE.md`, checked by exact
  substring) — presumably added after this crawl.  Dropped from that row's support set rather than
  citing a chunk that doesn't actually exist in the corpus being measured.

## Harness limitation

`eval.py`'s `matches()` scores a single retrieved hit against every expectation on a row at once,
and `recall_search` returns at most one chunk per `doc_id` by default.  There is no way to express
"credit this row only if hits from *both* doc A and doc B appear in the top-k" — the harness can only
ask whether some ONE hit contains a needle.  For every Group A row this means `expect_text_contains`
had to be the string that completes the **last** hop, which measures "did retrieval surface the
chunk that finishes the chain" but not "did retrieval surface the whole chain."  A genuinely
multi-hop-aware eval would need a new row shape — something like `expect_all_of: [{doc_id_prefix,
text_contains}, ...]` scored against the whole top-k set, not a single hit — which `eval.py` does not
support today and which this PR does not add (the brief was to measure with the existing harness,
unmodified).  If GraphRAG-style retrieval is built, re-scoring this file with a hit-set-aware
variant of `matches()` would be a natural follow-up, since right now even a system that finds *both*
chunks but ranks the first-hop chunk higher than the second-hop chunk scores identically to a system
that never finds the second-hop chunk at all.

## Secrets check

Every cited chunk's full text was read before its `doc_id` was used.  None contains a live
credential value; the closest cases are variable/field **names** (`RECALL_API_TOKEN`,
`DEEPSEEK_API_KEY`, `ASC_KEY_P8`) cited as "this is what rotated / was exposed," never accompanied
by the value itself, and board/PR/commit identifiers (8-hex board ids, PR numbers, git shas) that
are structurally identifiers, not secrets, and are the whole point of the chains being tested.  No
question, `expect_text_contains` needle, or `note` in the new file contains a token, key, password,
or anything base64/hex-opaque long enough to be mistaken for one.
