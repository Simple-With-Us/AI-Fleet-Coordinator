"""Retrieval-quality eval for the configured fleet collection.

Scores a golden set of repo docs, runbooks and policies at recall@1, recall@5,
recall@10 and MRR.  A query counts as recalled at k when any returned hit's
doc_id starts with one of that query's expected prefixes.

    python3 -m fleet_rag.retrieval_eval --offline [--json] [--out report.json]
    python3 -m fleet_rag.retrieval_eval                 # live search of the configured collection
    python3 -m fleet_rag.retrieval_eval --compare a.json b.json

--offline chunks the files named by the golden set and ranks them with an
in-memory BM25 index.  It does not read Qdrant or Infisical credentials.
Chunk target, overlap and hard max apply only in that mode, so two offline
runs can be compared after a chunking change.  --source, --app and --category
are exact filters on both modes.  The live mode passes them to recall_search
along with --per-doc, --no-rerank and --no-lessons.

Exit 0 when recall@10 meets --threshold (default 0.9), 1 when it does not,
2 on a bad golden file, a missing fixture or a usage error.  --compare always
exits 0 for a valid pair of reports.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import Counter

from .chunk import HARD_MAX, OVERLAP, TARGET, chunk_markdown
from .core import FleetRagError
from .experiments.tokenizer import tokenize
from .recall_api import recall_search

GOLDEN = pathlib.Path(__file__).with_name("retrieval_golden.json")
REPO_NAME = "AI-Fleet-Coordinator"
DOC_PREFIX = f"doc/{REPO_NAME}/"
COLLECTION_SETTING = "QDRANT_FLEET_COLLECTION"
SCHEMA_VERSION = 1
TOP_K = 10
DEFAULT_THRESHOLD = 0.9
FIXTURE_SOURCE = "doc"
FIXTURE_APP = "fleet"
FIXTURE_CATEGORY = "doc"

METRIC_KEYS = ("recall_at_1", "recall_at_5", "recall_at_10", "mrr")
METRIC_LABELS = (
    ("recall_at_1", "recall@1"),
    ("recall_at_5", "recall@5"),
    ("recall_at_10", "recall@10"),
    ("mrr", "MRR"),
)


def default_repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _safe_rel(rel: str) -> str:
    rel = rel.replace("\\", "/").lstrip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        raise FleetRagError(f"unsafe fixture path: {rel}")
    return rel


def rel_from_prefix(prefix: str) -> str:
    if not prefix.startswith(DOC_PREFIX):
        raise FleetRagError(f"expect_doc_id_prefix must start with {DOC_PREFIX}: {prefix}")
    return _safe_rel(prefix[len(DOC_PREFIX) :])


def load_golden(path: pathlib.Path | str = GOLDEN) -> dict:
    """Load the retrieval golden file.  JSON object with a queries array."""
    raw_path = pathlib.Path(path)
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FleetRagError(f"{raw_path}: cannot read golden file ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list):
        raise FleetRagError(f"{raw_path}: expected an object with a queries array")
    queries = []
    seen: set[str] = set()
    for n, row in enumerate(data["queries"], 1):
        if not isinstance(row, dict):
            raise FleetRagError(f"{raw_path}: query {n} is not an object")
        qid = row.get("id")
        query = row.get("query")
        prefixes = row.get("expect_doc_id_prefix")
        if not isinstance(qid, str) or not qid.strip():
            raise FleetRagError(f"{raw_path}: query {n} needs an id")
        if qid in seen:
            raise FleetRagError(f"{raw_path}: duplicate id {qid}")
        seen.add(qid)
        if not isinstance(query, str) or not query.strip():
            raise FleetRagError(f"{raw_path}: {qid} needs a query")
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if not isinstance(prefixes, list) or not prefixes or not all(
            isinstance(p, str) and p.strip() for p in prefixes
        ):
            raise FleetRagError(f"{raw_path}: {qid} needs expect_doc_id_prefix")
        queries.append({
            "id": qid,
            "query": query.strip(),
            "expect_doc_id_prefix": [p.strip() for p in prefixes],
            "note": row.get("note") or "",
        })
    if not queries:
        raise FleetRagError(f"{raw_path}: no queries")
    extra = data.get("extra_paths") or []
    if not isinstance(extra, list) or not all(isinstance(p, str) and p.strip() for p in extra):
        raise FleetRagError(f"{raw_path}: extra_paths must be a list of paths")
    return {"queries": queries, "extra_paths": [_safe_rel(p.strip()) for p in extra]}


def relevant(doc_id: str, prefixes: list[str]) -> bool:
    return any(doc_id.startswith(prefix) for prefix in prefixes)


def first_rank(hits: list[dict], prefixes: list[str]) -> tuple[int | None, str]:
    for i, hit in enumerate(hits, 1):
        doc_id = str(hit.get("doc_id") or "")
        if relevant(doc_id, prefixes):
            return i, doc_id
    return None, ""


def score_ranks(ranks: list[int | None]) -> dict:
    n = len(ranks)

    def recall(k: int) -> float:
        if not n:
            return 0.0
        return sum(1 for rank in ranks if rank is not None and rank <= k) / n

    mrr = 0.0 if not n else sum((1.0 / rank) if rank else 0.0 for rank in ranks) / n
    return {
        "n": n,
        "recall_at_1": recall(1),
        "recall_at_5": recall(5),
        "recall_at_10": recall(10),
        "mrr": mrr,
    }


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return fallback


def _chunk_settings(target: int | None, overlap: int | None, hard_max: int | None) -> dict:
    chosen = {
        "target": TARGET if target is None else target,
        "overlap": OVERLAP if overlap is None else overlap,
        "hard_max": HARD_MAX if hard_max is None else hard_max,
    }
    if chosen["target"] < 1 or chosen["overlap"] < 0 or chosen["hard_max"] < chosen["target"]:
        raise FleetRagError(
            "chunk settings must satisfy target >= 1, overlap >= 0 and hard_max >= target"
        )
    if chosen["overlap"] >= chosen["target"]:
        raise FleetRagError("chunk overlap must be smaller than the target")
    return chosen


def fixture_paths(golden: dict) -> list[str]:
    rels: list[str] = []
    seen: set[str] = set()
    for row in golden["queries"]:
        for prefix in row["expect_doc_id_prefix"]:
            rel = rel_from_prefix(prefix)
            if rel not in seen:
                seen.add(rel)
                rels.append(rel)
    for rel in golden["extra_paths"]:
        if rel not in seen:
            seen.add(rel)
            rels.append(rel)
    return rels


def build_fixture_rows(repo: pathlib.Path, rels: list[str], chunk: dict) -> list[dict]:
    rows: list[dict] = []
    for rel in rels:
        path = repo / rel
        if not path.is_file():
            raise FleetRagError(f"fixture file missing: {rel}")
        text = path.read_text(encoding="utf-8", errors="replace")
        doc_id = f"{DOC_PREFIX}{rel}"
        chunks = chunk_markdown(
            text,
            _title(text, path.name),
            target=chunk["target"],
            overlap=chunk["overlap"],
            hard_max=chunk["hard_max"],
        )
        if not chunks:
            chunks_text = [text.strip() or path.name]
        else:
            chunks_text = [ch.text for ch in chunks]
        for index, body in enumerate(chunks_text):
            rows.append({
                "doc_id": doc_id,
                "text": body,
                "index": index,
                "source": FIXTURE_SOURCE,
                "app": FIXTURE_APP,
                "category": FIXTURE_CATEGORY,
            })
    if not rows:
        raise FleetRagError("fixture index is empty")
    return rows


class FixtureIndex:
    """Okapi BM25 over fixture chunks.  Results are collapsed to per_doc hits per doc_id."""

    def __init__(self, rows: list[dict], *, k1: float = 1.5, b: float = 0.75):
        self.rows = rows
        self.k1 = k1
        self.b = b
        self._tokens = [tokenize(row.get("text", "")) for row in rows]
        self._tf = [Counter(tokens) for tokens in self._tokens]
        self.n = len(rows)
        self.avgdl = (sum(len(tokens) for tokens in self._tokens) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for tokens in self._tokens:
            df.update(set(tokens))
        self._df = df

    def _score_query(self, terms: list[str]) -> list[float]:
        scores = [0.0] * self.n
        if not terms or not self.avgdl:
            return scores
        for index, tf in enumerate(self._tf):
            dl = len(self._tokens[index]) or 1
            total = 0.0
            for term in terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
                denom = freq + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
                total += idf * (freq * (self.k1 + 1.0)) / denom
            scores[index] = total
        return scores

    def search(
        self,
        query: str,
        limit: int = TOP_K,
        *,
        per_doc: int = 1,
        source: str | None = None,
        app: str | None = None,
        category: str | None = None,
    ) -> dict:
        terms = tokenize(query)
        scores = self._score_query(terms)
        order = sorted(
            range(self.n),
            key=lambda i: (-scores[i], self.rows[i]["doc_id"], self.rows[i]["index"]),
        )
        kept: Counter[str] = Counter()
        hits: list[dict] = []
        for index in order:
            if scores[index] <= 0:
                break
            row = self.rows[index]
            if source and row["source"] != source:
                continue
            if app and row["app"] != app:
                continue
            if category and row["category"] != category:
                continue
            if kept[row["doc_id"]] >= per_doc:
                continue
            kept[row["doc_id"]] += 1
            hits.append({
                "doc_id": row["doc_id"],
                "text": row["text"],
                "score": scores[index],
                "source": row["source"],
            })
            if len(hits) >= limit:
                break
        return {"hits": hits, "mode": "offline-bm25"}


def _filters(source, app, category, since_days, per_doc, rerank, prefer_lessons) -> dict:
    return {
        "source": source,
        "app": app,
        "category": category,
        "since_days": since_days,
        "per_doc": per_doc,
        "rerank": rerank,
        "prefer_lessons": prefer_lessons,
    }


def run_retrieval(
    golden: dict,
    search,
    *,
    mode: str,
    chunk: dict | None,
    filters: dict,
    docs: int | None = None,
    chunks: int | None = None,
    limit: int = TOP_K,
) -> dict:
    details = []
    ranks: list[int | None] = []
    modes: Counter[str] = Counter()
    for row in golden["queries"]:
        result = search(row["query"], limit=limit)
        hits = result.get("hits") or []
        modes[str(result.get("mode", "?"))] += 1
        rank, doc_id = first_rank(hits, row["expect_doc_id_prefix"])
        ranks.append(rank)
        top = hits[0] if hits else {}
        details.append({
            "id": row["id"],
            "query": row["query"],
            "expected": row["expect_doc_id_prefix"],
            "rank": rank,
            "hit_doc_id": doc_id or None,
            "top_doc_id": top.get("doc_id"),
        })
    report = {
        "schema_version": SCHEMA_VERSION,
        "suite": "retrieval-quality",
        "mode": mode,
        "target_collection": COLLECTION_SETTING,
        "k": [1, 5, 10],
        **score_ranks(ranks),
        "chunk": chunk,
        "filters": filters,
        "docs": docs,
        "chunks": chunks,
        "modes": dict(sorted(modes.items())),
        "queries": details,
    }
    return report


def render(report: dict) -> str:
    lines = [
        f"retrieval-quality  mode={report['mode']}  collection={report['target_collection']}  "
        f"n={report['n']}  docs={report.get('docs')}  chunks={report.get('chunks')}",
        f"{'metric':<12} {'value':>8}",
    ]
    for key, label in METRIC_LABELS:
        lines.append(f"{label:<12} {report[key]:>8.3f}")
    if report.get("chunk"):
        chunk = report["chunk"]
        lines.append(
            f"chunk target={chunk['target']} overlap={chunk['overlap']} hard_max={chunk['hard_max']}"
        )
    filters = report.get("filters") or {}
    active = {key: value for key, value in filters.items() if value not in (None, True, 1) or key == "per_doc"}
    if active:
        shown = ", ".join(f"{key}={value}" for key, value in active.items())
        lines.append(f"filters {shown}")
    lines.append(f"{'id':<28} {'rank':>4}  hit")
    for row in report["queries"]:
        rank = "-" if row["rank"] is None else str(row["rank"])
        hit = row.get("hit_doc_id") or row.get("top_doc_id") or ""
        miss = "" if row["rank"] is not None and row["rank"] <= TOP_K else "  MISS"
        lines.append(f"{row['id']:<28} {rank:>4}  {hit}{miss}")
    return "\n".join(lines)


def compare_runs(before: dict, after: dict) -> dict:
    _require_report(before, "before")
    _require_report(after, "after")
    before_ids = {row["id"]: row for row in before["queries"]}
    changes = []
    for row in after["queries"]:
        prev = before_ids.get(row["id"])
        if prev is None:
            changes.append({
                "id": row["id"],
                "query": row["query"],
                "rank_before": None,
                "rank_after": row.get("rank"),
                "hit_before": None,
                "hit_after": row.get("hit_doc_id"),
            })
            continue
        if prev.get("rank") != row.get("rank") or prev.get("hit_doc_id") != row.get("hit_doc_id"):
            changes.append({
                "id": row["id"],
                "query": row["query"],
                "rank_before": prev.get("rank"),
                "rank_after": row.get("rank"),
                "hit_before": prev.get("hit_doc_id"),
                "hit_after": row.get("hit_doc_id"),
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": "retrieval-compare",
        "before": {key: before[key] for key in METRIC_KEYS},
        "after": {key: after[key] for key in METRIC_KEYS},
        "deltas": {key: after[key] - before[key] for key in METRIC_KEYS},
        "n_before": before["n"],
        "n_after": after["n"],
        "chunk_before": before.get("chunk"),
        "chunk_after": after.get("chunk"),
        "filters_before": before.get("filters"),
        "filters_after": after.get("filters"),
        "mode_before": before.get("mode"),
        "mode_after": after.get("mode"),
        "changes": changes,
    }


def _fmt_rank(rank) -> str:
    return "-" if rank is None else str(rank)


def render_compare(diff: dict) -> str:
    lines = [
        f"retrieval compare  n={diff['n_before']} -> {diff['n_after']}  "
        f"mode {diff.get('mode_before')} -> {diff.get('mode_after')}",
        f"{'metric':<12} {'before':>8} {'after':>8} {'delta':>8}",
    ]
    labels = dict(METRIC_LABELS)
    for key in METRIC_KEYS:
        lines.append(
            f"{labels[key]:<12} {diff['before'][key]:>8.3f} {diff['after'][key]:>8.3f} "
            f"{diff['deltas'][key]:>+8.3f}"
        )
    lines.append(f"chunk before {diff.get('chunk_before')}")
    lines.append(f"chunk after  {diff.get('chunk_after')}")
    lines.append(f"filters before {diff.get('filters_before')}")
    lines.append(f"filters after  {diff.get('filters_after')}")
    if diff["changes"]:
        lines.append("rank changes (before -> after, - is a miss):")
        for row in diff["changes"]:
            lines.append(
                f"  {row['id']:<28} {_fmt_rank(row['rank_before']):>2} -> "
                f"{_fmt_rank(row['rank_after']):<2}  {row.get('hit_after') or ''}"
            )
    else:
        lines.append("no per-query rank changes")
    return "\n".join(lines)


def _require_report(report: dict, label: str) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise FleetRagError(f"{label}: not a retrieval-eval report")
    if report.get("suite") == "retrieval-compare":
        raise FleetRagError(f"{label}: that file is a compare result, not a run")
    for key in ("queries", *METRIC_KEYS):
        if key not in report:
            raise FleetRagError(f"{label}: report is missing {key}")


def load_report(path: pathlib.Path | str) -> dict:
    raw = pathlib.Path(path)
    try:
        data = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FleetRagError(f"{raw}: cannot read report ({exc})") from None
    _require_report(data, str(raw))
    return data


def write_report(path: pathlib.Path | str, report: dict) -> None:
    dest = pathlib.Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _emit(report: dict, *, as_json: bool, out: str | None, renderer) -> None:
    if out:
        write_report(out, report)
    print(json.dumps(report, indent=2) if as_json else renderer(report))


def _live_search(filters: dict):
    def search(query: str, limit: int = TOP_K) -> dict:
        kwargs = {
            "per_doc": filters["per_doc"],
            "rerank": filters["rerank"],
            "prefer_lessons": filters["prefer_lessons"],
        }
        if filters["source"]:
            kwargs["source"] = filters["source"]
        if filters["app"]:
            kwargs["app"] = filters["app"]
        if filters["category"]:
            kwargs["category"] = filters["category"]
        if filters["since_days"] is not None:
            kwargs["since_days"] = filters["since_days"]
        return recall_search(query, limit=limit, **kwargs)

    return search


def run_offline(
    *,
    golden_path: pathlib.Path | str = GOLDEN,
    repo: pathlib.Path | str | None = None,
    chunk_target: int | None = None,
    chunk_overlap: int | None = None,
    chunk_hard_max: int | None = None,
    source: str | None = None,
    app: str | None = None,
    category: str | None = None,
    per_doc: int = 1,
) -> dict:
    if per_doc < 1:
        raise FleetRagError("per_doc must be at least 1")
    golden = load_golden(golden_path)
    root = pathlib.Path(repo) if repo else default_repo_root()
    chunk = _chunk_settings(chunk_target, chunk_overlap, chunk_hard_max)
    rels = fixture_paths(golden)
    rows = build_fixture_rows(root, rels, chunk)
    index = FixtureIndex(rows)
    filters = _filters(source, app, category, None, per_doc, None, None)
    return run_retrieval(
        golden,
        lambda query, limit: index.search(
            query, limit, per_doc=per_doc, source=source, app=app, category=category
        ),
        mode="offline",
        chunk=chunk,
        filters=filters,
        docs=len(rels),
        chunks=len(rows),
    )


def run_live(
    *,
    golden_path: pathlib.Path | str = GOLDEN,
    source: str | None = None,
    app: str | None = None,
    category: str | None = None,
    since_days: int | None = None,
    per_doc: int = 1,
    rerank: bool = True,
    prefer_lessons: bool = True,
) -> dict:
    golden = load_golden(golden_path)
    filters = _filters(source, app, category, since_days, per_doc, rerank, prefer_lessons)
    return run_retrieval(
        golden,
        _live_search(filters),
        mode="live",
        chunk=None,
        filters=filters,
    )


def compare_files(before: str, after: str, *, as_json: bool = False, out: str | None = None) -> int:
    diff = compare_runs(load_report(before), load_report(after))
    _emit(diff, as_json=as_json, out=out, renderer=render_compare)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet_rag.retrieval_eval",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--golden", default=str(GOLDEN))
    parser.add_argument("--repo", help="repo root for --offline (defaults to this checkout)")
    parser.add_argument("--offline", action="store_true", help="score the fixture index, no Qdrant")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write the JSON report to this path")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="fail when recall@10 is below this (default 0.9); compare ignores it")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE.json", "AFTER.json"))
    parser.add_argument("--chunk-target", dest="chunk_target", type=int)
    parser.add_argument("--chunk-overlap", dest="chunk_overlap", type=int)
    parser.add_argument("--chunk-hard-max", dest="chunk_hard_max", type=int)
    parser.add_argument("--source")
    parser.add_argument("--app")
    parser.add_argument("--category")
    parser.add_argument("--since-days", dest="since_days", type=int)
    parser.add_argument("--per-doc", dest="per_doc", type=int, default=1)
    parser.add_argument("--no-rerank", dest="rerank", action="store_false")
    parser.add_argument("--no-lessons", dest="prefer_lessons", action="store_false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.compare:
            return compare_files(args.compare[0], args.compare[1], as_json=args.json, out=args.out)
        if args.offline:
            if args.since_days is not None:
                raise FleetRagError("--since-days applies to a live run; the fixture has no dates")
            report = run_offline(
                golden_path=args.golden,
                repo=args.repo,
                chunk_target=args.chunk_target,
                chunk_overlap=args.chunk_overlap,
                chunk_hard_max=args.chunk_hard_max,
                source=args.source,
                app=args.app,
                category=args.category,
                per_doc=args.per_doc,
            )
        else:
            if any(value is not None for value in (args.chunk_target, args.chunk_overlap, args.chunk_hard_max)):
                raise FleetRagError("chunk settings apply to --offline; a live run scores the ingested collection")
            report = run_live(
                golden_path=args.golden,
                source=args.source,
                app=args.app,
                category=args.category,
                since_days=args.since_days,
                per_doc=args.per_doc,
                rerank=args.rerank,
                prefer_lessons=args.prefer_lessons,
            )
    except FleetRagError as exc:
        print(f"retrieval-eval: {exc}", file=sys.stderr)
        return 2
    _emit(report, as_json=args.json, out=args.out, renderer=render)
    if args.threshold is None:
        return 0
    return 0 if report["recall_at_10"] >= args.threshold else 1


def run_from_namespace(args) -> int:
    """Entry used by `recall eval --retrieval` / `--offline`."""
    argv = ["--golden", args.golden or str(GOLDEN)]
    if args.offline:
        argv.append("--offline")
    if args.json:
        argv.append("--json")
    if getattr(args, "out", None):
        argv += ["--out", args.out]
    if args.threshold is not None:
        argv += ["--threshold", str(args.threshold)]
    if getattr(args, "repo", None):
        argv += ["--repo", args.repo]
    for flag, value in (
        ("--chunk-target", getattr(args, "chunk_target", None)),
        ("--chunk-overlap", getattr(args, "chunk_overlap", None)),
        ("--chunk-hard-max", getattr(args, "chunk_hard_max", None)),
        ("--source", getattr(args, "source", None)),
        ("--app", getattr(args, "app", None)),
        ("--category", getattr(args, "category", None)),
        ("--since-days", getattr(args, "since_days", None)),
    ):
        if value is not None:
            argv += [flag, str(value)]
    if getattr(args, "per_doc", 1) != 1:
        argv += ["--per-doc", str(args.per_doc)]
    if not getattr(args, "rerank", True):
        argv.append("--no-rerank")
    if not getattr(args, "prefer_lessons", True):
        argv.append("--no-lessons")
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
