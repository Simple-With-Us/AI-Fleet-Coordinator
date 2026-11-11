"""Read-only local cache of the fleet-agents corpus payloads, for building a BM25 index.

Scrolls `fleet-agents` through core.Qdrant (never upserts, never deletes; uses the
read-only key exactly the way recall_search does) and writes one JSON object per point
to a local JSONL cache so repeated experiment runs do not re-scroll ~42k points from
Hetzner every time.  The cache lives under experiments/.cache/, which is gitignored --
it holds fleet corpus content, not code, and has no reason to bloat the PR diff.

    python3 -m fleet_rag.experiments.corpus_cache            # build/refresh the cache
    python3 -m fleet_rag.experiments.corpus_cache --count    # just print how many points
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .. import core, recall_api

CACHE_DIR = pathlib.Path(__file__).with_name(".cache")
CACHE_FILE = CACHE_DIR / "fleet-agents-corpus.jsonl"

# Same payload fields recall_api._hit() surfaces, plus the point id.
FIELDS = ("source", "app", "category", "seat", "doc_id", "chunk_index", "heading", "title",
          "url", "path", "created_at", "text")


def _default_filter() -> dict:
    """The same must_not-meta filter every unfiltered recall_search query uses."""
    return recall_api.build_filter()


def scroll_corpus(cfg: dict[str, str] | None = None) -> list[dict]:
    """Read-only scroll of every non-meta point.  Uses QDRANT_READONLY_API_KEY when set."""
    cfg = cfg or core.load_config(need_write=False)
    q = core.Qdrant(cfg)
    flt = _default_filter()
    out = []
    for p in q.scroll(flt=flt, limit=256, with_payload=True, with_vector=False):
        payload = p.get("payload") or {}
        row = {"id": p.get("id"), **{f: payload.get(f, "") for f in FIELDS}}
        out.append(row)
    return out


def build_cache(force: bool = False) -> pathlib.Path:
    if CACHE_FILE.exists() and not force:
        return CACHE_FILE
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rows = scroll_corpus()
    tmp = CACHE_FILE.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    tmp.replace(CACHE_FILE)
    return CACHE_FILE


def load_cache(force_rebuild: bool = False) -> list[dict]:
    path = build_cache(force=force_rebuild)
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="ignore any existing cache")
    ap.add_argument("--count", action="store_true", help="just print the point count")
    a = ap.parse_args(argv)
    rows = load_cache(force_rebuild=a.rebuild)
    if a.count:
        print(len(rows))
    else:
        print(f"cached {len(rows)} points -> {CACHE_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
