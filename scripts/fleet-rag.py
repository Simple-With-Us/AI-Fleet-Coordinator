#!/usr/bin/env python3
"""Query and ingest the fleet-agents knowledge corpus (thin CLI over the fleet_rag package).

Credentials come from the environment when set (QDRANT_URL, QDRANT_API_KEY, TEI_URL,
TEI_API_KEY, QDRANT_FLEET_COLLECTION are enough on their own), otherwise from Infisical via a
machine identity in the environment, otherwise from `fleet-secrets`.  Values are never printed.

    fleet-rag.py stats
    fleet-rag.py search "how do I avoid leaking credentials?" --limit 5
    fleet-rag.py search "vector database" --category infrastructure --since-days 30
    fleet-rag.py ingest notes.jsonl --source doc --app fleet --category lesson [--dry-run]
    fleet-rag.py ingest-all [--source board,doc,...] [--dry-run] [--limit N] [--since 7d] [--prune]
    fleet-rag.py ingest-all --fix-seeds

See docs/RAG-FLEET-INFRA.md.  Search is hybrid (dense + keyword, RRF) per the recall tool
contract; `ingest` routes raw JSON Lines through the same scrub + gitleaks + upsert path as
the full pipeline in fleet_rag.ingest.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fleet_rag import core, ingest
from fleet_rag.core import (
    FleetRagError,
    Qdrant,
    embed,
    embedder_healthy,
    match_filter,
    now_ms,
    query_terms,
)
from fleet_rag.scrub import GitleaksError

KNOWN_SOURCES = (
    "linear",
    "board",
    "effort-log",
    "apple-note",
    "doc",
    "skill",
    "memory",
    "chat-log",
    "agent-contribution",
)
KNOWN_APPS = (
    "fleet",
    "fleet-ops",
    "socratic-trade",
    "congress-trade",
    "congress-trading-shared",
    "usage-monitor",
    "dealdex",
    "botfleet",
    "autorotate",
    "contactlogo",
    "personal-site",
    "trading",
)
HIT_FIELDS = (
    "source",
    "app",
    "category",
    "seat",
    "doc_id",
    "chunk_index",
    "heading",
    "title",
    "url",
    "path",
    "created_at",
)


# --------------------------------------------------------------------------- tool contract


def recall_search(
    query: str,
    limit: int = 5,
    category: str | None = None,
    app: str | None = None,
    source: str | None = None,
    seat: str | None = None,
    since_days: int | None = None,
    cfg: dict | None = None,
) -> dict:
    """Hybrid search per the recall tool contract (prefers fleet_rag.recall_api when present)."""
    try:
        from fleet_rag.recall_api import recall_search as _shared  # type: ignore

        return _shared(
            query,
            limit=limit,
            category=category,
            app=app,
            source=source,
            seat=seat,
            since_days=since_days,
        )
    except ImportError:
        pass
    cfg = cfg or core.load_config()
    qd = Qdrant(cfg)
    flt = match_filter(category=category, app=app, source=source, seat=seat)
    if since_days:
        rng = {"key": "created_at", "range": {"gte": now_ms() - int(since_days) * 86_400_000}}
        flt = {"must": (flt or {"must": []})["must"] + [rng]}
    terms = query_terms(query)
    vec = embed(cfg, [query])[0]
    hits = qd.query_hybrid(vec, terms, limit=limit, flt=flt)
    out = []
    for h in hits:
        p = h.get("payload", {})
        out.append(
            {
                "score": h.get("score"),
                "text": p.get("text", ""),
                **{
                    k: p.get(k, "" if k not in ("chunk_index", "created_at") else 0)
                    for k in HIT_FIELDS
                },
            }
        )
    return {"hits": out, "mode": "hybrid" if terms else "dense"}


def recall_stats(cfg: dict | None = None) -> dict:
    try:
        from fleet_rag.recall_api import recall_stats as _shared  # type: ignore

        return _shared()
    except ImportError:
        pass
    cfg = cfg or core.load_config()
    qd = Qdrant(cfg)
    info = qd.info()
    return {
        "collection": qd.collection,
        "status": info.get("status"),
        "points": info.get("points_count"),
        "embedder_healthy": embedder_healthy(cfg),
        "by_source": {s: qd.count(match_filter(source=s)) for s in KNOWN_SOURCES},
        "by_app": {a: qd.count(match_filter(app=a)) for a in KNOWN_APPS},
        "dimension": info.get("config", {}).get("params", {}).get("vectors", {}).get("size"),
        "indexed": sorted(info.get("payload_schema", {})),
    }


# --------------------------------------------------------------------------- commands


def cmd_stats(args: argparse.Namespace) -> int:
    st = recall_stats()
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"collection : {st['collection']}")
    print(f"status     : {st['status']}")
    print(f"points     : {st['points']}")
    if st.get("dimension"):
        print(f"dimension  : {st['dimension']}")
    if st.get("indexed"):
        print(f"indexed    : {', '.join(st['indexed'])}")
    print(f"embedder   : {'healthy' if st['embedder_healthy'] else 'UNREACHABLE'}")
    print("by source  : " + ", ".join(f"{k}={v}" for k, v in st["by_source"].items() if v))
    print("by app     : " + ", ".join(f"{k}={v}" for k, v in st["by_app"].items() if v))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    res = recall_search(
        args.query,
        limit=args.limit,
        category=args.category,
        app=args.app,
        source=args.source,
        seat=args.seat,
        since_days=args.since_days,
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    if not res["hits"]:
        print("no matches")
        return 0
    for h in res["hits"]:
        tags = "/".join(
            x for x in (h.get("category"), h.get("app"), h.get("source"), h.get("seat")) if x
        )
        score = h.get("score")
        head = f"[{score:.4f}]" if isinstance(score, (int, float)) else "[-]"
        print(f"\n{head} {tags}  ({h.get('doc_id', '-')}#{h.get('chunk_index', 0)})")
        if h.get("title"):
            print(f"  title: {h['title']}")
        text = h.get("text", "")
        print("  " + (text if args.full else text[:400] + ("..." if len(text) > 400 else "")))
    print(f"\nmode: {res['mode']}")
    return 0


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise FleetRagError(f"{path}:{n}: not valid JSON") from None
            if not isinstance(row, dict):
                raise FleetRagError(f"{path}:{n}: row must be a JSON object")
            if not str(row.get("text", "")).strip():
                raise FleetRagError(f"{path}:{n}: missing 'text'")
            rows.append(row)
    if not rows:
        raise FleetRagError("nothing to ingest")
    return rows


def cmd_ingest(args: argparse.Namespace) -> int:
    rows = _read_jsonl(args.file)
    defaults = {
        "source": args.source,
        "app": args.app,
        "category": args.category,
        "seat": args.seat,
        "doc_id": args.doc_id or f"rows/{pathlib.Path(args.file).stem}",
        "created_at": args.created_at or None,
        "title": pathlib.Path(args.file).stem,
    }
    res = ingest.ingest_rows(rows, defaults, dry_run=args.dry_run)
    if args.dry_run:
        print(
            f"would upsert {res['would_write']} new points ({res['already_present']} already present, "
            f"{res['dropped_by_gitleaks']} dropped by gitleaks, {res['scrubbed']} scrubbed) — no write performed"
        )
    else:
        print(
            f"upserted {res['written']} new points ({res['already_present']} already present, "
            f"{res['dropped_by_gitleaks']} dropped by gitleaks, {res['scrubbed']} scrubbed)"
        )
    return 0


def cmd_ingest_all(args: argparse.Namespace) -> int:
    argv: list[str] = []
    if args.fix_seeds:
        argv.append("--fix-seeds")
    elif args.source:
        argv += ["--source", args.source]
    else:
        argv.append("--all")
    if args.dry_run:
        argv.append("--dry-run")
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.since:
        argv += ["--since", args.since]
    if args.no_heartbeat:
        argv.append("--no-heartbeat")
    if args.prune:
        argv.append("--prune")
    return ingest.main(argv)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="collection, embedder health, counts by source and app")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("search", help="hybrid (dense + keyword) search over the corpus")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--category")
    s.add_argument("--app")
    s.add_argument("--source")
    s.add_argument("--seat")
    s.add_argument("--since-days", dest="since_days", type=int)
    s.add_argument("--full", action="store_true", help="print whole chunks, not excerpts")
    s.add_argument("--json", action="store_true")

    i = sub.add_parser(
        "ingest",
        help="upsert JSON Lines rows through scrub + gitleaks (idempotent by content hash)",
    )
    i.add_argument("file")
    i.add_argument("--source", default="doc")
    i.add_argument("--app", default="fleet")
    i.add_argument("--category", default="lesson")
    i.add_argument("--seat", default=os.environ.get("AGENT_SEAT", "CLAUDE"))
    i.add_argument("--doc-id", dest="doc_id")
    i.add_argument(
        "--created-at",
        dest="created_at",
        type=int,
        default=0,
        help="epoch ms for every row without its own created_at (default: now)",
    )
    i.add_argument("--dry-run", action="store_true")

    a = sub.add_parser("ingest-all", help="run the full source pipeline (fleet_rag.ingest)")
    a.add_argument(
        "--source", help="comma-separated subset: board,effort-log,doc,skill,memory,apple-note"
    )
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--limit", type=int)
    a.add_argument("--since")
    a.add_argument("--fix-seeds", dest="fix_seeds", action="store_true")
    a.add_argument("--no-heartbeat", dest="no_heartbeat", action="store_true")
    a.add_argument(
        "--prune",
        action="store_true",
        help="after a source finishes cleanly, delete chunks of documents it no longer yields",
    )

    args = ap.parse_args()
    try:
        return {
            "stats": cmd_stats,
            "search": cmd_search,
            "ingest": cmd_ingest,
            "ingest-all": cmd_ingest_all,
        }[args.cmd](args)
    except (FleetRagError, GitleaksError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
