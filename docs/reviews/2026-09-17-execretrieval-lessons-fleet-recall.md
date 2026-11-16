# ExecRetrieval (arXiv 2609.01865) — What It Teaches Fleet Recall

**Date:** 2026-09-17.  **Seat:** CLAUDE.  **Scope:** review only, no code changed.
**Question (owner):** is there anything to learn from this type of study for the fleet / agents RAG?
**Method:** two multi-agent workflows, 21 agents.  Phase 1 read the paper's full text, mapped
`scripts/fleet_rag/` on `origin/main` @ `1224526`, and ran a live read-only probe of the corpus.
Phase 2 had two blind proposal generators, one adversarial verifier per candidate lesson (each
checked every `file:line` and paper-line claim), and a completeness critic.

## Short Answer

Yes — one big lesson, two methodology lessons, two negative lessons.  But most of what is
worth doing came from reading our own code and probing our own corpus, not from the paper.

1. **Rank 1 is resemblance, not correctness.**  When a near-clone of the right answer is in
   the pool, a retriever finds the right answer (top 10) and still ranks the wrong twin first.
   Our near-clones are not buggy code, they are **superseded truth**: an old owner ruling
   beside the new one, an open board row beside its completed mirror.
2. **Our eval cannot see this**, because it has no correctness oracle — a superseded doc that
   contains the same phrase scores as a rank-1 win.
3. **Our eval has no error bars.**  At n = 75 the 95% interval on Recall@1 is about ±0.10,
   which is wider than most deltas we have acted on.
4. **Negative:** swapping the embedding model will not fix it, and neither will making the
   distinguishing text "louder."  The fix is structural metadata plus downstream reading.

## The Paper

939 Python tasks, each with one execution-verified canonical implementation and up to four
execution-verified single-edit buggy variants planted in the same search pool (4,694
snippets).  23 dense embedding configurations plus BM25.  Paired exact McNemar tests and
5,000-resample query-level bootstrap intervals (L199).

| Finding | Number | Where |
|---|---|---|
| Best system finds the right answer | exec@10 = 1.000 | L219 |
| …and ranks it first | exec@1 = 0.331, CI [0.299, 0.362] | L217 |
| When rank 1 is wrong it is the paired buggy twin | 91.5–99.4% of the time | Table 3, L257 |
| Canonical scores below at least one distractor | 66.8% / 78.4% of queries | L307–309 |
| Median canonical-vs-distractor cosine gap | −0.002 | L307–309 |
| **One** retained near-clone drops the best system | 0.993 → 0.678 | Table 4, L299 |
| Model size / dimension does not predict rank-1 skill | p = 0.622, 0.456 | L356–374 |
| Bigger or stacked edits do not help the retriever | 43–48% deceived, flat | App. G, L1688–1740 |
| BM25 | exec@1 0.058, exec@10 0.422 | L221 |

**What the paper does not show.**  It evaluates **no reranker, cross-encoder, hybrid fusion, or
LLM judge** — that is its own first item of future work (L402–416).  It has no prose, no
metadata, no temporal dimension.  And its limitations section says every magnitude is
"conditional on a near-clone candidate in the pool" and that how often deployed corpora pose
that choice "is unmeasured" (L396).  So it licenses *"when a near-clone is present, rank 1 is
unreliable."*  It licenses nothing about how often fleet recall is wrong.

## The Fleet Probe (Live, Read-Only, n = 13)

Neutral questions about topics where an owner ruling changed, limit 10, judged by reading
each hit.

| | Reranked | Fused only |
|---|---|---|
| Current truth at rank 1 | 5 / 13 | 4 / 13 |
| Current truth in top 5 | 9 / 13 | 8 / 13 |

- Textbook case: *"sentence gap in the Claude Code desktop app?"* returns the
  `FLEET-UI-COPY.md` `&nbsp;` rule at rank 1, reading "SOLVED 2026-08-19, owner-verified," with
  the current two-literal-spaces ruling (2026-09-04) at rank 2.  Verified as a genuine
  same-surface contradiction, not a scoping false positive.
- The reranker is a wash on this failure: it rescued two pairs and broke two others.
- `created_at` is file-level, so the stale *section* of a living doc carries the date of the
  file's latest edit.  A recency prior at document granularity would misfire.
- **Read this number carefully.**  The Wilson 95% interval on 5/13 is [0.18, 0.65].  Only
  about 10 of the 13 are true supersession pairs; two were other failures (candidate depth,
  no lexical bridge) and one was duplicate crowding.  It shows the failure exists here.  It
  does not measure its rate.

## Verified Facts About Our Code

| Fact | Where |
|---|---|
| Board `status` / `severity` are computed, then dropped at ingest | `sources.py:426-427` → `ingest.py:201-212` never reads `doc.extra` |
| Status survives only as a prose line in chunk 0 of a row | `sources.py:356`; `chunk.py:90-95` repeats only the title |
| `category` lesson/finding keys off *resolution text*, not status, so they can disagree | `sources.py:413,420` |
| `updated_at` is stored but never returned; hits carry no point id | `recall_api.py:51-52` |
| No recency, status, authority, or supersession signal anywhere in ranking | `core.py:428-475` |
| Reranker sees query + chunk text only | `recall_api.py:238`, `core.py:300` |
| `seat=OWNER` matches nothing live — it is set only on chat-log docs, which are not ingested | `sources.py:1051`, `:93-95`; 0 hits on 3 searches |
| Owner rulings come back `source=doc, seat=FLEET`; the lesson prefetch boosts agent contributions *over* them | `core.py:516-522` |
| Agent contributions are 436 of 45,953 points (0.95%) | `recall stats` |
| Contribute is upsert-only: a correction never marks the wrong lesson down | `recall_api.py:380-447` |
| Duplicate guard: cosine ≥ 0.92 against agent contributions only | `contribute_guard.py:19-34` |
| Same-`doc_id` edits genuinely retire old chunks (the pipeline's real strength) | `ingest.py:381-390` |
| `recall eval` never touches the public fallback, so **no recorded measurement was corrupted** | `eval.py:124-126`, `scripts/recall:238` |
| The knob drop on `recall search` is documented, tested, intended — and the server rejects unknown args | `public_fallback.py:48-53,139-143`; `server.py:89-104,160-162` |
| **Bug:** `require_direct_path` does not use `direct_path_blocked`, so on a Mac with no Tailscale.app eval / ingest / bare doctor neither fall back nor fail fast; they grind at 120 s × 4 retries | `public_fallback.py:330` vs `:265-279`, used correctly at `:448` |
| A `reject_*` key in `golden.jsonl` loads clean and is silently ignored | `eval.py:48-66,78-98` |
| `fused_groups` first Qdrant window is uncapped (limit 40 → 800 points; cap 400 applies only on widening) | `recall_api.py:188-213` |
| bge-m3 is invoked with raw text, which is what its model card documents | `core.py` embed call — no action |
| Stale comment: "best-of-33-contributions" | `core.py:70` |

The "one GitHub issue with contradictory status" seen in the probe is really **eight distinct
board rows** for one work item (agent-report, github-issue, and effort-row mirrors; two pairs
differ only by hyphen vs em-dash in the title).  Doc-level grouping worked correctly.  This is a
document-identity problem created by the triple-file protocol plus a re-mirror after a
typography edit.

## Verdicts, In Build Order

| # | Item | Verdict | Size | Paper support |
|---|---|---|---|---|
| 0 | **Skill bullet** — for a ruling or infra fact, read the top 5, never act on rank 1 alone, open the source for its date; corrections need `force: true` | Adopt, reworded | S | Analogical (L382) |
| 1 | **Measurement path** — `require_direct_path` → `direct_path_blocked`; pass `rerank` / `per_doc` / `prefer_lessons` through, **server first**, then client | Adopt, modified | S–M | None |
| 2 | **Counterfactual eval + paired statistics**, one PR | Adopt, modified | L | Direct (protocol), analogical (construct) |
| 3 | **Persist `status` / `severity`; return `status`, `updated_at`, point id** | Adopt, modified | M | None |
| 4 | **Fix stale sources** — only after item 2's rows carry a VOID state | Adopt, modified | S–M | Analogical |
| 5 | **Near-duplicate families: annotate and demote, never collapse** | Adopt, inverted | M | Analogical (Table 4) |
| 6 | **Supersession by point id, demote not hide** | Adopt, modified | L | Analogical |
| 7 | Rerank-margin flag · candidate depth · chunk-level dates · query-time LLM adjudication | **Measure first** | — | — |

**Item 0** is the one thing to do if only one thing gets done.  It is the only item whose value
does not depend on a number nobody has, and the probe says the answer is usually already on
screen.  Do **not** tell agents to look for `seat: OWNER` (matches nothing) or to trust the
payload's date.  Canonical file is `docs/fleet-skills/fleet-recall/SKILL.md`; leave
`docs/AGENTS-RECALL-SNIPPET.md` alone.

**Item 2 details.**  A separate `counterfactual.jsonl`, never `golden.jsonl` (that set anchors
history and gates CI at `scripts/recall:250`).  Paired rows `{query, current:{…}, superseded:{…}}`,
each side a spec `matches()` already understands.  Three metrics: **current@1**,
**stale_above_current** (the paper's own L63 metric; defined even when neither side reaches top
5), and **void**.  VOID matters: when someone fixes a stale doc, ingest deletes the distractor
and the row silently becomes easy — a vanished distractor must void the row, never count as a
win.  About 10 harvested pairs plus about 90 planted by zero-LLM template mutations (status
flip, tag swap, date change, negation), **reported apart, never pooled**.  Power: roughly 125
rows for 80% power at a 75/25 discordant split.  Statistics are stdlib: exact McNemar via
`math.comb` on Recall@1 discordant pairs, plus a 5,000-resample fixed-seed bootstrap on
R@1 / R@5 / MRR.  At n = 75, exact McNemar needs a 6–0 discordant split, so the smallest
detectable Recall@1 delta is about 0.08 — it tightens the harness, it does not solve small n.
The 2026-09-14 BM25 A/B cannot be re-adjudicated from the repo; its per-query cache is
gitignored.

**Item 3 details.**  Backfill from live `sources.iter_board()` fanned out through
`ingest.collection_ids_for_doc()` with `set_payload` — never by regex over stored chunk text,
which would miss every chunk past index 0.  Going forward no invalidation machinery is needed:
status is already in the hashed markdown, so a status edit already re-ingests the row.

**Item 4 details.**  RAG ingests the loose `~/apps/FLEET-UI-COPY.md` as canonical and skips the
tracked mirror, so a PR alone changes nothing recall serves.  The `AFL` offenders
(`Fleet-OPS/FLEET-PROTOCOL.md`, `AGENTS.md`, and `ATTACK-MAP.md`, which lists AFL canonical and AFC
retired at rerank 0.91 against 0.015 for the corrective board row) live in the `fleet-ops` repo
and need a cross-repo row.  The conflict sweep belongs as a step in the existing weekly Oracle
routine "Fleet RAG weekly health + recall eval," not a new job.

**Item 5 details.**  Detect families from text already in the hits (token-shingle Jaccard plus
`difflib`, heading prefix stripped) — no vectors, no new calls.  Stamp `family_id` and
`also_seen`, cap a family at two consecutive top slots, push the surplus below the first
unrelated hit, and **never drop a hit**.  When members disagree on status set `conflict: true`
and keep both visible and adjacent.  Fix the re-mirror identity key at ingest.

**Item 6 details.**  Address by point id, not `doc_id` (the flagship case is one stale section
of a living doc).  Demote, never hide: a deterministic post-rerank pass that cannot leave a
superseded point above a live one, with `superseded_by` / `superseded_at` / `superseded_by_seat`
always returned.  Restrict to agent-contribution points at first.  Make the argument loud on
every surface in the same PR, or it silently no-ops through the public fallback.

## Rejected, And Why

- **Collapse near-duplicates by cosine.**  The paper's whole finding is that the right item and
  its wrong twin are indistinguishable in vector space.  Collapsing turns a visible rank-2 into
  a silent coin flip with no recovery path.  Measured: contradictory open-vs-completed twins
  sit at `difflib` 0.80–0.88, inside the boilerplate band, so no threshold separates them.
- **Hide superseded points by default.**  Nothing authenticates the seat string, and the public
  service shares one token.  The agent likeliest to file a wrong "correction" is the one that
  just read the stale doc at rank 1.
- **Rerank-margin `ambiguous` flag, as drafted.**  On the probe's own table the margin separates
  right-at-1 from wrong-at-1 at AUC 0.60 (p ≈ 0.28).  At threshold 0.02 it fires on 11 of 13
  queries, barely beating a constant.  A flag that fires on 85% of searches trains every seat to
  ignore it.  The better-supported signal is the paper's causal variable — *a near-clone is
  present among the top documents* — and it must also fire when rerank did not engage, because
  fused margins are structurally 0.17–0.50 and read as "confident."
- **Authority boost on `seat=OWNER`.**  Matches nothing.
- **Document-level recency prior.**  Misfires on living docs.  Chunk-level date extraction covers
  only 30% of doc chunks (17% body-only), and "take the max date" picks the wrong date on the
  probed example.
- **Swap the embedding model / add louder "SUPERSEDED" prose.**  The paper argues against both.

## Still Unmeasured

| Claim | What settles it |
|---|---|
| How often a real query poses a near-clone choice (every impact rating depends on this) | Offline near-clone family count over `experiments/corpus_cache`, plus `stale_above_current` on harvested rows |
| current@1 as a level | ≥ 100 rows, planted and harvested reported apart |
| Whether TEI rerank scores are sigmoid-normalised | One `/rerank` call with and without `raw_scores` |
| The 0.92 guard refuses real corrections | Embed about 20 real correction/original pairs, plot the cosine distribution |
| "Rerank gives up above about 120 candidates" | Time it at 32 / 64 / 128 / 200, report p95 |
| Candidate-depth misses are a pattern | Currently n = 1 (Congress.Trade); run a depth sweep |

## Credit Where Due

The paper earns the **construct** (a paired pool scored by a rank-1 correctness metric), the
**statistical protocol**, the **pool-density finding** that makes near-clone *presence* the
right diagnosis, the two **negative lessons**, and the framing that the discrimination burden
passes downstream (L382).  Status fields, supersession metadata, the fallback bug, the
contribute guard, duplicate families, and candidate depth are fleet code-reading, the n = 13
probe, and ordinary IR practice.

Related work consulted: NevIR (arXiv 2305.07614) — bi-encoders score below random on
negation minimal pairs, cross-encoders help but stay far below human; Zep (2501.13956) —
bi-temporal validity, invalidate rather than delete; Mem0 (2504.19413) — ADD / UPDATE / DELETE
memory operations.
