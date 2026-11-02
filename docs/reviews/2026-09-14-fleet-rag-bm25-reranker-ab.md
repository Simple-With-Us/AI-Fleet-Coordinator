# Fleet RAG: BM25 and bge-reranker-v2-m3 A/B — 2026-09-14

Historical identifiers have been redacted. These records do not certify current deployment state.

**Author:** CLAUDE, owner-requested.  **Board:** af4d7bb6.  **Scope:** measurement only — nothing
in this document changed production retrieval, the recall API, the MCP server, or the collection
schema, and nothing here made an LLM call or wrote to `fleet-agents`.

## TL;DR

Only one leg of this A/B could actually be run this session: **BM25 alone**, fully offline, scores
R@1 0.627 / R@5 0.853 / MRR 0.717 against the live production anchor of R@1 0.72 / R@5 0.93 / MRR
0.81 — worse overall, and much worse specifically on agent-contribution lessons (R@1 0.23) where
queries are phrased in plain English rather than the identifiers BM25 rewards, but on par with or
ahead of the anchor on `board` queries (R@1 0.82, R@5 1.00) where hex ids and title fragments do
the work.  Every variant that needs a dense vector — the reproduced production 3-leg baseline, BM25
added as a 4th leg, BM25 replacing the keyword leg, both rerankers, and the best-combination —
could **not** be measured: the live Hetzner TEI embed and rerank endpoints failed every bounded,
retried attempt (confirmed with direct diagnostics, not just a slow overall harness run), and the
local BAAI/bge-m3 fallback this brief specifies for exactly this situation could not be built
either, because Hugging Face's CDN failed every attempt to download the required model weights to
this Mac.  Both failures are independently confirmed, well-diagnosed, and unrelated to each other
(see *Operational note*).  **Recommendation: do not conclude anything about BM25-as-a-fused-leg
from this run** — only BM25-alone-vs-baseline is real data, and it does not answer the brief's
central question (whether BM25 helps the *existing* dense+keyword+lesson pipeline).  The three
driver scripts for the blocked parts (`run_live_ab.py`, `run_local_ab.py`, `run_offline_bm25.py`)
are written, reviewed, and exercised on their working code paths (the BM25-only leg runs through
both the live-variant and local-variant builders without incident) — the next attempt only needs
one of the two infrastructure problems to clear, not new code.

## Method

All code lives under `scripts/fleet_rag/experiments/` in this PR.  Everything is read-only
against the live `fleet-agents` collection (scroll + dense search only, the same
`QDRANT_READONLY_API_KEY` path `recall_search` itself uses) and makes zero writes and zero LLM
calls.  Every variant is scored by the **unmodified** `fleet_rag/eval.py` harness
(`run_eval`/`load_golden`/`matches`) — nothing in `eval.py` was changed — so every number below
is directly comparable to `python3 -m fleet_rag.eval`.

Pipeline for every variant (mirrors `recall_api.recall_search`): embed the query with the real
TEI call production makes → fetch that variant's retrieval legs → fuse them with reciprocal rank
fusion (`experiments/rrf.py`) → group by `doc_id` in fused order (production's own grouping
semantics) → flatten to the best `per_doc` chunks per document → optional rerank → first `k` hits.

- **`experiments/tokenizer.py`** — the BM25 tokenizer (see *Tokenizer choice* below).
- **`experiments/corpus_cache.py`** — one read-only scroll of the collection into a local JSONL
  cache (`experiments/.cache/`, gitignored) so repeated runs don't re-scroll ~42k points.
- **`experiments/bm25_index.py`** — `rank_bm25.BM25Okapi` over the cached corpus.
- **`experiments/legs.py`** — the three production prefetch legs (plain dense, keyword-filtered
  dense, lesson-filtered dense), fetched individually via `core.Qdrant` so they can be
  recombined; core.py itself is untouched.
- **`experiments/rrf.py`** — the RRF fusion and doc-id grouping used to recombine legs.
- **`experiments/variants.py`** — builds one `search(query, limit, **kwargs) -> {"hits","mode"}`
  callable per variant, matching `eval.py`'s existing contract exactly (see
  `fleet_rag/tests/test_eval.py`), so `eval.run_eval` scores every variant without modification.
- **`experiments/reranker.py`** — loads `cross-encoder/ms-marco-MiniLM-L-6-v2` and
  `BAAI/bge-reranker-v2-m3` locally via `sentence_transformers.CrossEncoder` and times every
  `.predict()` call.
- **`experiments/run_ab.py`** — the original live-only driver (Parts A/B/C against a fully healthy
  Hetzner box).  Not usable this session (see *Operational note*); kept for when the box recovers.
- **`experiments/run_offline_bm25.py`** — the fully offline half: BM25 alone, and both rerankers on
  BM25 candidates.  Needs no Qdrant, no TEI, and no local dense embedder at all.  This is what
  actually ran this session (rerankers unavailable — see below).
- **`experiments/local_dense.py` / `local_variants.py` / `run_local_ab.py`** — the brief's own
  fallback: brute-force cosine search over BAAI/bge-m3 embeddings computed locally
  (`sentence_transformers`, MPS on this Mac) instead of the live TEI/Qdrant path, with the same RRF
  fusion and doc-grouping as production.  Written and exercised on its BM25-only leg; blocked
  end-to-end because the bge-m3 weights never finished downloading (see below).
- **`experiments/live_cache.py` / `live_variants.py` / `run_live_ab.py`** — a resilient live driver
  added mid-session: every embed / leg-fetch / rerank call is disk-cached per query (or per
  query+text, for rerank) so a 75-query run touches the live box at most once per fact, with a
  short, real per-call timeout and a small bounded retry count instead of `core.http_json`'s
  defaults (see that module's docstring for a `core.DEFAULT_TIMEOUT` reassignment gotcha this
  surfaced: default *parameter* values are bound once at `def` time, so only patching the
  `http_json` name itself actually changes what every caller uses).  Confirmed the live embed and
  rerank endpoints are genuinely down for real inference, not just slow (see below), so this also
  did not produce Part A/B/C data this session — but it is the right tool for the next attempt.
- **`experiments/variants.py`** — one small fix alongside the above: a leg-set of `("bm25",)` alone
  no longer calls the live embed endpoint at all (it never used the vector), so a pure-BM25
  variant has zero live dependency, on the live driver or the original one.

### Tokenizer choice

The fleet corpus is identifier-heavy (env var names, error strings, board ids, file paths), which
is exactly where a naive tokenizer breaks BM25: splitting on every non-alnum character shatters
`SERVICE_PASSWORD` into noise, and a plain regex `\w+` swallows `.`/`/`/`-` as if they were spaces,
losing the fact that `docs/RAG-FLEET-INFRA.md` is one path.  `experiments/tokenizer.py` treats
`. _ / -` as **identifier glue**, not word separators to discard:

1. Extract "compound" runs of `[A-Za-z0-9]` glued by `. _ / -` and emit each compound **whole**,
   as one atomic term — `SERVICE_PASSWORD`, `EADDRINUSE` (no glue, stays one token), a bare
   32-char hex board id, `vm.swappiness`, `docs/RAG-FLEET-INFRA.md` all survive as one rare,
   high-IDF term.
2. Any compound that actually contains glue is **also** split into its parts, which are emitted
   as additional terms — so a plain-English query for "password" still finds a chunk that only
   ever spells it `SERVICE_PASSWORD`.  A compound with no glue needs no decomposition (splitting
   `EADDRINUSE` would just reproduce the same token).
3. No stemming — identifiers are kept literal on purpose.
4. The same short stopword list `core.query_terms` already uses for the production keyword leg,
   so both tokenizers agree on what counts as noise.

Examples (see the module docstring for more):

| input | tokens |
|---|---|
| `PINECONE_TRIAL_ENDS_AT` | `pinecone_trial_ends_at`, `pinecone`, `trial`, `ends`, `at`\* |
| `EADDRINUSE` | `eaddrinuse` (unchanged — no glue) |
| `docs/RAG-FLEET-INFRA.md` | `docs/rag-fleet-infra.md`, `docs`, `rag`, `fleet`, `infra`, `md` |

\* `at` is 2 chars and not a stopword, so it survives at `min_len=2`; short and low-information
but harmless given BM25's IDF weighting.

## Part A — retrieval: BM25 vs the production 3-leg fusion

**BM25 alone** (no dense leg, no keyword leg, no lesson leg, no rerank — `("bm25",)` in
`variants.LEG_NAMES`), scored by the unmodified `eval.py` harness against the same 75-row
`golden.jsonl`:

| | R@1 | R@5 | MRR |
|---|---|---|---|
| **Live production baseline** (dense+keyword+lesson, RRF, reranked — confirmed twice previously) | 0.72 | 0.93 | 0.81 |
| **BM25 alone** (measured this session) | **0.627** | **0.853** | **0.717** |

BM25 alone recovers about 92 % of the baseline's Recall@5 and 88 % of its MRR using no embedding
model at all — a real signal that lexical matching alone carries a lot of this corpus, but clearly
short of the full pipeline.  Per-source breakdown (`eval.py`'s `by_source`, BM25-alone):

| source | n | R@1 | R@5 | MRR |
|---|---|---|---|---|
| board | 11 | 0.82 | 1.00 | 0.89 |
| doc | 2 | 1.00 | 1.00 | 1.00 |
| apple-note | 8 | 0.75 | 0.88 | 0.79 |
| agent-contribution | 13 | **0.23** | **0.69** | **0.45** |
| any (no narrower bucket) | 41 | 0.66 | 0.85 | 0.73 |

The split is exactly what the tokenizer's own design predicts: `board` and `doc` queries tend to
name a distinctive identifier or file path (a 32-char hex board id, `RAG-FLEET-INFRA.md`) that
survives as a rare, high-IDF compound token, so BM25 does very well there — actually *matching or
beating* the live baseline's implied per-source recall on `board`.  `agent-contribution` queries are
the opposite case: they are phrased as plain-English questions about a lesson (*"should I trigger a
Coolify API deploy after merging to main"*) that never repeats the lesson text's own wording closely
enough for BM25's term overlap to find it, which is precisely the gap dense embeddings and the
production lesson-prefetch leg (`LESSON_SCORE_THRESHOLD`, a dedicated agent-contribution prefetch)
exist to close.  Full list of the 11 BM25-alone misses is in
`experiments/.cache/results/bm25_alone_full.json` (gitignored; re-run
`python3 -m fleet_rag.experiments.run_offline_bm25` to reproduce) — 8 of the 11 are
agent-contribution or [retired identifier] rows without a strong unique keyword.

**Not measured — three variants blocked:**

- **`prod-3leg` reproduction** (dense+keyword+lesson, this experiment's own RRF fusion of
  individually-fetched legs, the parity check `run_ab.py` was designed to do against the real
  `+lessons` config)
- **`bm25-added`** (dense+keyword+lesson+bm25, BM25 as a 4th fused leg)
- **`bm25-replaces-keyword`** (dense+bm25+lesson, BM25 swapped in for the filtered-dense keyword leg)

All three need a real query embedding, and every path to one failed this session (live TEI embed,
live TEI rerank, and the local bge-m3 fallback — see *Operational note* for the exact, independently
confirmed failures on each).  This is the actual central question the brief asked — whether BM25
helps *on top of* the existing dense+keyword+lesson pipeline — and it remains open.

## Part B — reranker: ms-marco-MiniLM-L-6-v2 vs BAAI/bge-reranker-v2-m3

**Blocked: Hugging Face CDN unreachable from this Mac.**  Neither reranker could be evaluated,
locally or live:

- **Local (`sentence_transformers.CrossEncoder`, the intended apples-to-apples comparison):**
  `BAAI/bge-reranker-v2-m3` (~568M params) never finished downloading — every attempt (the
  default `hf_xet` fast-transfer path, the classic resumable HTTP downloader with
  `HF_HUB_DISABLE_XET=1`, and a direct `curl --retry --continue-at -` against the resolved URL,
  across two separate environment instances during this session) failed at the same hop: a TLS
  handshake timeout or connection reset against `us.aws.cdn.hf.co`, the CDN host `resolve/main/...`
  redirects to.  Small files from the same repos (config/tokenizer/sentencepiece, low tens of MB)
  downloaded fine every time via direct `curl`, so this is specifically a large-transfer failure
  against that CDN host, not a total outage.  `cross-encoder/ms-marco-MiniLM-L-6-v2` (~22.7M
  params, ~90MB) — production's actual model — hit the identical failure in this session's fresh
  environment, so even the smaller model could not be secured locally this time (an earlier
  environment instance this session did load it successfully before an unrelated session reset
  wiped the cache; see *Operational note*).
- **Live (production's actual deployment, TEI-served):** confirmed separately and directly
  unreachable for real inference — see *Operational note*'s embed/rerank diagnostics — so this
  could not stand in for the local comparison either.

No quality, latency, or parameter-count comparison is reported.  The code (`experiments/reranker.py`,
and the `rerank_fn` plumbing in `variants.py` / `local_variants.py` / `live_variants.py`) is written
and was exercised successfully against `cross-encoder/ms-marco-MiniLM-L-6-v2` earlier in this same
session (before the environment reset), so the comparison itself is not the open question — getting
both sets of weights onto a machine that can run this is.

## Part C — best combination

**Not measured.**  Part C is defined as the best Part-A retrieval variant plus the best Part-B
reranker; both inputs are blocked (see Parts A and B above), so there is nothing to combine yet.
`run_live_ab.py` and `run_local_ab.py` both already implement Part C mechanically (pick the
highest-`recall_at_k` Part-A leg-set, pick the highest-`recall_at_k` Part-B reranker, run one more
`eval.run_eval` pass) — it will produce a number the next time either script's inputs are available,
with no further code changes.

## Golden-set observations

**Rank flips:** none to report — only one variant (BM25-alone) actually ran, so there is no
second variant's ranking to diff against it this session.  `run_live_ab.py` / `run_local_ab.py`
already compute and record `part_a_flips` (both directions) once the blocked variants can run.

**Review for wrong or ambiguous rows:** read all 75 rows in `golden.jsonl` looking for a query
whose expectation doesn't actually hold, or that admits a materially different reading than
intended.  Found no row that looks outright wrong.  Two rows are worth flagging as *intentional*
looseness, not defects, since a future editor might otherwise "tighten" them incorrectly:

- `"bge-m3 reindex and backfill program for Socratic.Trade retrieval"` — its own note says
  "several rows share the title"; the row narrows with `expect_source: "board"` plus a text needle
  rather than a `doc_id` prefix, so it accepts any matching board row rather than pinning one.
- `"Background Jobs Master List of Mac local processes"` — its own note says "many versions"; same
  pattern (`expect_source: "apple-note"` plus a text needle, not a prefix).

Both are pre-existing, already self-documented in their `note` field, and consistent with
`eval.matches()`'s semantics (a hit only needs to satisfy every expectation *present* on the row,
so a looser row is a deliberate choice to tolerate multiple valid answers, not a bug).  Nothing was
changed in `golden.jsonl` (out of scope per the brief).

## Recommendation

**Ship nothing to production from this session.**  The only real number — BM25 alone trails the
live baseline by roughly 8-9 points of Recall@1/MRR (7.7 points on Recall@5) — says lexical matching is a meaningfully weaker
retriever than the existing dense+keyword+lesson pipeline on this corpus, which nobody proposed
disputing; it does **not** answer whether adding true BM25 as a complementary fused leg (Part A's
`bm25-added` / `bm25-replaces-keyword`) would improve on what production already ships, and that is
the question this whole measurement exists to answer.  Recommend re-running
`python3 -m fleet_rag.experiments.run_live_ab` (works as soon as the Hetzner box's TEI embed/rerank
endpoints answer real inference requests again, not just health/count checks — see *Operational
note*) or `python3 -m fleet_rag.experiments.run_local_ab` (works as soon as `BAAI/bge-m3` can be
downloaded to this Mac) before making any production decision.  Both are already written, and the
BM25-only code path both share has already run cleanly through each of them.

**Production cost, for when the retrieval question is answered:** even a clearly-positive result
would mean standing up and maintaining a true BM25 index (`rank_bm25.BM25Okapi` or equivalent)
alongside Qdrant in `recall_api`/`core.py` — built from the same corpus, kept in sync with every
ingest run, adding a maintained second index rather than only a filter clause on the existing dense
index.  That is a real, ongoing operational cost (rebuild-on-ingest, memory footprint, one more
thing to monitor), separate from whether the retrieval-quality win justifies it.  Not proposed here
because the retrieval-quality question itself is still open.

**bge-reranker-v2-m3:** no basis yet to recommend for or against it — Part B never ran.  If a
future attempt shows it beats `ms-marco-MiniLM-L-6-v2` on this golden set, weigh that against its
~25x parameter count (568M vs 22.7M) and CPU-only Hetzner latency (not measured by any of this
session's Mac-local timings) before shipping it.

## Operational note (unrelated to the measurement itself)

Two independent infrastructure problems, neither caused by this experiment, together blocked every
part of this measurement except BM25-alone.  Both are described precisely below because the next
attempt only needs one of them to clear.

**1. Hetzner box (`100.69.77.26`): inference endpoints down, lightweight endpoints up.**  SSH to
the box timed out during the banner exchange throughout this session.  `recall.jays.services/health`
answered 200 quickly, and a `recall stats` call (which only issues `q.info()` / `q.count()` /
`embedder_healthy()` / `rerank_healthy()` — collection metadata and `/health` pings, never a real
embedding or rerank computation) succeeded after over two minutes.  That distinction matters: a
health check answering does not mean the model-serving path is up.  Direct, isolated, bounded
diagnostics against the actual inference endpoints all failed:

| call | port | attempts | result |
|---|---|---|---|
| `core.embed` (live run, unpatched retry/timeout) | :8081 | 1 (5 internal sub-attempts) | `RemoteDisconnected`, twice, over two different queries |
| `core.embed` (isolated diagnostic, 30s × 3 attempts) | :8081 | 1 | `TimeoutError`, 94s |
| `core.rerank` (isolated diagnostic, its own 8s budget) | :8082 | 1 | `TimeoutError`, 8.1s |

This is consistent with a board comment on this same item (`af4d7bb6`) from earlier in this task:
the box measured load average 6.65 on 6 vCPUs, 10Gi/16Gi swap in use, and the Qdrant container at
101% CPU — i.e. the box is overloaded, not merely network-flaky, so the model-serving paths time
out or drop connections under real inference load even while the cheap metadata/health paths
eventually answer.

A real methodology bug surfaced while chasing this: the first fix attempt reassigned
`core.DEFAULT_TIMEOUT` / `core.RETRIES` as module attributes, which is a **no-op** — every caller
in `core.py` (`http_json`, `Qdrant._call`, `embed`, `rerank`) already bound those names as
*parameter defaults* when `core.py` was first imported (Python evaluates default argument values
once, at `def` time, not per call), so the reassignment changed the module attribute but not any
already-defined function's behavior.  What every caller *does* look up dynamically, on every call,
is the plain name `http_json` inside `core.py`'s own module namespace — so `live_variants.py` /
`run_live_ab.py` instead replace that name itself (`core.http_json = _bounded_http_json`), which is
what actually took effect for the 30s-timeout/3-attempt diagnostic above.  Left in the code as a
documented gotcha (`run_live_ab.py`'s module docstring) since it is easy to reintroduce.

**2. Hugging Face CDN: large-file downloads fail from this Mac, unrelated to Hetzner.**
`huggingface.co`'s API answered throughout (slowly — 2-10s for small JSON responses), but every
attempt to download `BAAI/bge-m3`, `BAAI/bge-reranker-v2-m3`, or (in this session's second
environment instance) `cross-encoder/ms-marco-MiniLM-L-6-v2` failed partway through, always at the
same hop: a TLS handshake timeout or connection reset against `us.aws.cdn.hf.co`, the CDN host
`resolve/main/...` URLs redirect to.  Confirmed independent of `huggingface_hub`'s own retry logic
by reproducing the identical failure with raw `curl -L` (`SSL_ERROR_SYSCALL` / `LibreSSL SSL_connect`
against that same host) — three separate curl attempts to the bare host showed roughly 2-of-3
short requests succeeding but any sustained large-file transfer failing, which is consistent with
a connection that survives brief exchanges but cannot sustain a long one from this network path
right now.  Tried, in order, across both environment instances of this session: the default
`hf_xet` fast-transfer client; the classic resumable downloader with `HF_HUB_DISABLE_XET=1`; and a
direct `curl --retry 8 --continue-at -` against the resolved URL.  Small files from the same
repos (config.json, tokenizer.json, sentencepiece.bpe.model — low tens of MB) downloaded
successfully every time via direct curl, so this is specifically a large-transfer/CDN-host problem,
not a total network outage, and not specific to any one download client.

**3. A session interruption mid-task compounded both.**  This task's session was interrupted once
(the coordinator's read: a platform auth hiccup, since resolved) and resumed in a fresh environment
instance: the git worktree (`/Users/jay/apps/fleet-claude-bm25`, including the Python virtualenv)
persisted, but the Hugging Face cache under `~/.cache` did not, so partial downloads from before the
interruption (including a fully-loaded `cross-encoder/ms-marco-MiniLM-L-6-v2` that had briefly
worked) had to restart from zero in the new instance and hit problem 2 again.

No production change is proposed for either issue — this is an availability record for whoever
re-runs this measurement, not a retrieval-quality finding.

## Addendum: reranker bake-off, 2026-09-17 (CLAUDE)

**Timestamp:** Thu, Sep 17, 2026 at 10:52 AM CT (measured ~14:26-15:44 UTC on fleet-hetzner-nbg1).
**Scope:** measurement only, production untouched — every candidate ran as a throwaway
`rr-<label>` TEI container on `recall-api`'s own docker network (no published ports), reached
only via `docker exec -e TEI_RERANK_URL=...`; each was stopped and removed before the next
started.  Full method, raw JSON, and the full wins/losses list are in
`/private/tmp/claude-501/-Users-jay-Code-AI-Fleet-Coordinator/782eb1ae-dcc5-4acf-8fe8-425dc6fe8890/scratchpad/rerank/results.md`.
This directly answers Part B above, which this session's predecessor could not measure (HF CDN
was unreachable that day).

Eval: the unmodified `fleet_rag.eval` scorer over the full 75-row `golden.jsonl`, 20 candidate
docs reranked per query (`candidate_count(5)=20`), production's real 8s `RERANK_TIMEOUT` budget,
auto-rerun at 30s for any candidate that hit a fallback.

| Model | Params | R@1 | R@5 | MRR | rerank p50 | rerank p95 | fallbacks @8s | wins vs ctrl | losses vs ctrl |
|---|---|---|---|---|---|---|---|---|---|
| *Fused only (no rerank)* | — | 0.667 | 0.853 | 0.742 | — | — | — | — | — |
| **ms-marco-MiniLM-L-6-v2 (control, production)** | 22.7M | 0.707 | 0.920 | 0.796 | 718 ms | 901 ms | 0/75 | — | — |
| ms-marco-MiniLM-L-12-v2 | 33.4M | 0.720 | 0.933 | 0.809 | 1384 ms | 1883 ms | 0/75 | 2 | 1 |
| gte-reranker-modernbert-base | 149M | 0.760 | 0.933 | 0.836 | 5864 ms | 7573 ms | 3/75 | 5 | 1 |
| bge-reranker-base | 278M | 0.733 | 0.933 | 0.815 | 3895 ms | 4642 ms | 0/75 | 7 | 5 |
| bge-reranker-v2-m3 | 568M | 0.667 | 0.853 | 0.744 | — (0 scored) | — (0 scored) | **75/75** | 3 | 6 |

Uncapped reruns (30s budget): modernbert's 3 fallback queries all resolved to the same ranks
once given time (score unchanged, true rerank latency 5.9s p50 / 7.6s p95 / 7.9s max).
bge-reranker-v2-m3 still hit **75/75** fallbacks at 30s — every one of 150 `/rerank` calls
(75 at 8s, 75 at 30s) ended in `TimeoutError reaching rr-bge-v2m3:80`; it never returned a
single scored response on this box's `--cpus 8` allocation, so its row above is the fused
baseline by fallback, not a real reranked measurement, and it is disqualified from the latency
comparison outright.

**Candidate loading:** all five loaded on TEI `cpu-1.8` without incident, nothing skipped
(`bge-reranker-base` / `bge-reranker-v2-m3` are XLM-RoBERTa, the same family as this box's live
`bge-m3` embedder; `gte-reranker-modernbert-base` also served correctly on this image version).

**Recommendation:** gte-reranker-modernbert-base is the only candidate that beats the control on
all three quality metrics with genuine reranking (R@1 0.760, R@5 0.933, MRR 0.836), but its
7.6s rerank p95 is far past a 3s search budget on this CPU allocation — not viable un-optimized.
Within an actual 3s p95 search budget, only the control (p95 1.06s) and
ms-marco-MiniLM-L-12-v2 (p95 2.00s) qualify; L-12 scores marginally higher on every metric for
roughly double the latency and still clears the budget with margin, making it the one clear,
measurable upgrade over production within a 3s p95 constraint. bge-reranker-base has the best
R@1 among candidates that complete (0.733) but its 4.96s search p95 exceeds 3s. bge-reranker-v2-m3
cannot be recommended on this hardware at any quality level — it needs more CPU, a GPU, or a much
smaller candidate/batch size before it can even be measured.

**Cleanup:** every `rr-*` container removed before the next candidate started (confirmed none
remain); `/var/tmp/rr-models` model cache deleted at the end (confirmed gone); no image pulled
(TEI `cpu-1.8`, 938 MB, was already local).  Disk on `/`: 158G/131G avail before, 162G/126G avail
after — the ~4GB delta tracks a `docker system df` Build Cache growth (50/7.0GB to 63/9.95GB)
from other apps deploying on this shared box during the run window, not from this experiment.
