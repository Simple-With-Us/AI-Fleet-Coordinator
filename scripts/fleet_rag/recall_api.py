"""The recall tool contract: recall_search / recall_stats / recall_contribute.

One implementation shared by the `recall` CLI, the `fleet-recall-mcp.py` stdio server, and
the seat-mcp tools, so every surface has exactly the same semantics.  Functions return plain
dicts and raise FleetRagError on user errors (bad arguments, missing credentials).  Nothing in
this module prints; callers decide how to render.

Backend seams: the module-level names `load_config`, `embed`, `embedder_healthy`,
`rerank_healthy`, `Qdrant`, `rerank`, `gitleaks_flagged`, and `gitleaks_available` are what the
functions call, so a test (or the FLEET_RECALL_FAKE=1 hook, see `install_fake_backend`) can
replace them without touching the live services.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import tempfile
import time
from typing import Any

from . import core, search_log, telemetry
from .core import (FleetRagError, LESSON_CATEGORIES, LESSON_SOURCE, build_point, content_hash,
                   match_filter, now_ms, query_terms, rerank_configured)
from .scrub import gitleaks_flagged as _real_gitleaks_flagged
from .scrub import scrub

# Seams (monkeypatch these in tests; install_fake_backend() swaps them all at once).
load_config = core.load_config
embed = core.embed
embedder_healthy = core.embedder_healthy
rerank_healthy = core.rerank_healthy
Qdrant = core.Qdrant
rerank = core.rerank
gitleaks_flagged = _real_gitleaks_flagged


def _real_gitleaks_available() -> bool:
    return bool(shutil.which("gitleaks"))


gitleaks_available = _real_gitleaks_available

SOURCES = ("board", "effort-log", "apple-note", "doc", "skill", "memory", "chat-log",
           "agent-contribution")
CATEGORIES = ("lesson", "preference", "infrastructure", "decision", "runbook", "finding", "note", "doc")
CONTRIB_CATEGORIES = ("lesson", "preference", "infrastructure", "decision", "runbook")
KNOWN_APPS = ("fleet", "socratic-trade", "congress-trade", "congress-trading-shared", "usage-monitor",
              "dealdex", "botfleet", "personal-site", "fleet-ops", "autorotate", "contactlogo", "clutch")
HIT_FIELDS = ("source", "app", "category", "seat", "doc_id", "chunk_index", "heading", "title",
              "url", "path", "created_at")

# The ingest orchestrator stores its run sentinel as a point with source="meta".  It is never
# a search result: every search filter carries this must_not clause.
META_SOURCE = "meta"


def meta_exclude() -> dict:
    """Fresh must_not condition for the ingest sentinel (fresh so callers cannot share state)."""
    return {"key": "source", "match": {"value": META_SOURCE}}

# Marker appended to recall_contribute()["scrubbed"] when gitleaks is not installed, so callers
# can see that only the regex scrub ran.
GITLEAKS_UNAVAILABLE = "gitleaks-unavailable"

MAX_LIMIT = 50
PER_DOC_MAX = 3          # recall_search(per_doc=...) upper bound
GROUP_DEPTH = 5          # chunks kept per doc group; group_hits is capped here
# In-process grouping fetches limit * GROUP_DEPTH points.  When one document owns most of the
# top of the fused list that window fills before `limit` docs are seen (30 chunks of one doc +
# 8 other matching docs, limit 5 -> 1 hit), so the window widens GROUP_WIDEN_FACTOR x, up to
# GROUP_WIDEN_ROUNDS more fetches, never past GROUP_WINDOW_MAX points, until `limit` groups are
# filled or the corpus runs out.  Every fetch carries the same filter (sentinel excluded).
GROUP_WIDEN_FACTOR = 4
GROUP_WIDEN_ROUNDS = 2
GROUP_WINDOW_MAX = 400
RERANK_MIN_CANDIDATES = 20
# Grouping backend.  Default: one flat fused query (limit * GROUP_DEPTH points) grouped by
# doc_id in-process, which preserves the global RRF order.  Qdrant 1.19's
# /points/query/groups fuses the prefetches PER GROUP (every group's top chunk scores 1.0), so
# its group order is not the fused ranking; it is kept as an opt-in for a Qdrant that fixes
# that (set FLEET_RECALL_QDRANT_GROUPS=1).  Measured 2026-09-02 on the live corpus: server-side
# groups Recall@1 0.39 vs in-process 0.73 on the same golden set.
QDRANT_GROUPS_ENV = "FLEET_RECALL_QDRANT_GROUPS"
CONTRIB_MIN = 40
CONTRIB_MAX = 4000
DAY_MS = 86_400_000

_APP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
_SEAT_RE = re.compile(r"^[A-Z][A-Z0-9_\-]{0,31}$")

_CACHE: dict[str, dict[str, str]] = {}


# --------------------------------------------------------------------------- config

def get_config(need_write: bool = False) -> dict[str, str]:
    """Cached credentials.  A write-capable config also satisfies later read requests."""
    if "write" in _CACHE:
        return _CACHE["write"]
    if not need_write and "read" in _CACHE:
        return _CACHE["read"]
    cfg = load_config(need_write=need_write)
    _CACHE["write" if need_write else "read"] = cfg
    return cfg


def reset_config_cache() -> None:
    _CACHE.clear()


def key_mode(cfg: dict[str, str]) -> str:
    """Which Qdrant key reads will use: read-only, write (no read-only key present), or none."""
    if cfg.get("QDRANT_READONLY_API_KEY"):
        return "read-only"
    if cfg.get("QDRANT_API_KEY"):
        return "write"
    return "none"


# --------------------------------------------------------------------------- validation

def _opt_str(name: str, value: Any) -> str | None:
    """Optional string argument: None / blank -> None; any non-str -> FleetRagError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise FleetRagError(f"{name} must be a string, not {type(value).__name__}")
    value = value.strip()
    return value or None


def _one_of(name: str, value: str | None, allowed: tuple[str, ...]) -> str | None:
    if value is not None and value not in allowed:
        raise FleetRagError(f"{name} must be one of " + "|".join(allowed) + f" (got {value!r})")
    return value


# --------------------------------------------------------------------------- search

def build_filter(category: str | None = None, app: str | None = None, source: str | None = None,
                 seat: str | None = None, since_days: int | None = None,
                 now: int | None = None) -> dict:
    """Exact-match filter on the four enumerated fields plus a created_at lower bound.

    Always excludes the ingest sentinel (source="meta"), so the result is never None.
    """
    flt = match_filter(category=category, app=app, source=source, seat=seat) or {}
    must = list(flt.get("must", []))
    if since_days is not None:
        if not isinstance(since_days, int) or isinstance(since_days, bool) or since_days <= 0:
            raise FleetRagError("since_days must be a positive integer")
        gte = (now if now is not None else now_ms()) - since_days * DAY_MS
        must.append({"key": "created_at", "range": {"gte": gte}})
    out: dict[str, Any] = {"must_not": [meta_exclude()]}
    if must:
        out["must"] = must
    return out


def _hit(raw: dict) -> dict:
    p = raw.get("payload") or {}
    out: dict[str, Any] = {"score": round(float(raw.get("score", 0.0)), 4), "text": p.get("text", "")}
    for f in HIT_FIELDS:
        out[f] = p.get(f, 0 if f in ("chunk_index", "created_at") else "")
    return out


def candidate_count(limit: int) -> int:
    """How many fused candidates to pull before grouping / reranking down to `limit`."""
    return max(4 * limit, RERANK_MIN_CANDIDATES)


def use_qdrant_groups() -> bool:
    return os.environ.get(QDRANT_GROUPS_ENV, "").strip() == "1"


def _groups_unsupported(e: Exception) -> bool:
    msg = str(e)
    return any(code in msg for code in ("HTTP 400", "HTTP 404", "HTTP 405", "HTTP 501"))


def group_hits(points: list[dict], group_by: str = "doc_id", group_size: int = 1) -> list[dict]:
    """In-process equivalent of the groups query: first-seen order, at most group_size per key."""
    groups: dict[Any, list[dict]] = {}
    for pt in points:
        key = (pt.get("payload") or {}).get(group_by)
        bucket = groups.setdefault(key, [])
        if len(bucket) < max(1, group_size):
            bucket.append(pt)
    return [{"id": k, "hits": v} for k, v in groups.items()]


def fused_groups(q: Any, vector: list[float], terms: list[str], n_groups: int, depth: int,
                 flt: dict | None, prefer_lessons: bool = True) -> list[dict]:
    """Up to `n_groups` doc groups from one flat fused query, grouped in-process in fused order.

    Starts with a window of n_groups * depth points and widens it (GROUP_WIDEN_FACTOR x, at
    most GROUP_WIDEN_ROUNDS times, capped at GROUP_WINDOW_MAX) while fewer than `n_groups`
    documents came back and the previous window was full, i.e. more candidates may exist.  Each
    group keeps at most `depth` chunks, so group_hits semantics are unchanged.
    """
    window = n_groups * depth
    groups: list[dict] = []
    for round_ in range(GROUP_WIDEN_ROUNDS + 1):
        flat = q.query_hybrid(vector, terms, window, flt, prefer_lessons=prefer_lessons)
        groups = group_hits(flat, "doc_id", depth)
        done = len(groups) >= n_groups or len(flat) < window or window >= GROUP_WINDOW_MAX
        if done or round_ == GROUP_WIDEN_ROUNDS:
            break
        window = min(window * GROUP_WIDEN_FACTOR, GROUP_WINDOW_MAX)
    return groups[:n_groups]


def _grouped_candidates(groups: list[dict], per_doc: int) -> list[dict]:
    """Flatten Qdrant groups into hits: the best `per_doc` chunks of each doc, in fused order.

    Every hit carries group_hits = how many chunks of that doc were in the fused candidate set
    (capped at GROUP_DEPTH) so callers can see when one document dominated the query.
    """
    out: list[dict] = []
    for g in groups:
        hits = g.get("hits") or []
        depth = len(hits)
        for raw in hits[:per_doc]:
            h = _hit(raw)
            h["group_hits"] = depth
            out.append(h)
    return out


def _apply_rerank(cfg: dict[str, str], query: str, hits: list[dict]) -> bool:
    """Reorder hits in place by cross-encoder score.  False (and hits untouched) on any error."""
    if not hits or not rerank_configured(cfg):
        return False
    try:
        scores = rerank(cfg, query, [h["text"] for h in hits])
    except FleetRagError as e:
        # Silent by design (a flaky reranker must never break a query) -- but silent everywhere
        # means a real outage only surfaces at the next weekly `recall eval`.  One Sentry
        # breadcrumb per fallback (no query text, no doc content) closes that gap without
        # changing the fallback behavior itself.
        telemetry.capture_message(f"rerank fallback: {type(e).__name__}", level="warning",
                                  operation="rerank-fallback")
        return False
    if len(scores) != len(hits):
        return False
    for h, sc in zip(hits, scores):
        h["fused_score"] = h["score"]
        h["rerank_score"] = round(float(sc), 4)
    hits.sort(key=lambda h: -h["rerank_score"])
    for h in hits:
        h["score"] = h["rerank_score"]
    return True


def recall_search(query: str, limit: int = 5, category: str | None = None, app: str | None = None,
                  source: str | None = None, seat: str | None = None,
                  since_days: int | None = None, per_doc: int = 1, prefer_lessons: bool = True,
                  rerank: bool = True) -> dict:
    """Hybrid (dense + keyword + lesson, RRF) search over the fleet-agents corpus, one hit per
    document.

    Pipeline: one fused query (dense + keyword + lesson prefetches, RRF) over a window of
    docs * GROUP_DEPTH points (widened when one doc crowds it out, see fused_groups), grouped
    by doc_id in fused order (see QDRANT_GROUPS_ENV for the server-side groups query) -> the
    best `per_doc` (1..3) chunks of each doc -> optional
    cross-encoder rerank of the top candidate_count(limit) docs when TEI_RERANK_URL /
    TEI_RERANK_API_KEY are configured and the endpoint answers in time (any failure keeps the
    fused order) -> first `limit` hits.

    prefer_lessons=True adds the agent-contribution lesson prefetch so a matching lesson always
    enters the fusion near the top; pass False for raw document research.

    Returns {"hits": [...], "mode": "hybrid"|"dense" (+"+rerank" when the rerank engaged)}.
    Each hit carries group_hits (chunks of that doc among the candidates, capped at
    GROUP_DEPTH) and, when reranked, fused_score / rerank_score.  Read-only key when available.
    """
    if not isinstance(query, str) or not query.strip():
        raise FleetRagError("query must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise FleetRagError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    if isinstance(per_doc, bool) or not isinstance(per_doc, int) or not 1 <= per_doc <= PER_DOC_MAX:
        raise FleetRagError(f"per_doc must be an integer between 1 and {PER_DOC_MAX}")
    category = _one_of("category", _opt_str("category", category), CATEGORIES)
    source = _one_of("source", _opt_str("source", source), SOURCES)
    app = _opt_str("app", app)
    seat = _opt_str("seat", seat)
    if seat:
        seat = seat.upper()
    if app:
        app = app.lower()
    t0 = time.time()
    flt = build_filter(category, app, source, seat, since_days)
    cfg = get_config(need_write=False)
    query = query.strip()
    vector = embed(cfg, [query])[0]
    terms = query_terms(query)
    # Ask for exactly `limit` doc groups unless a rerank can happen, in which case pull the wider
    # candidate set the cross-encoder needs.
    will_rerank = bool(rerank) and rerank_configured(cfg)
    n_cand = candidate_count(limit) if will_rerank else limit
    q = Qdrant(cfg)
    depth = max(GROUP_DEPTH, per_doc)
    groups: list[dict] | None = None
    if use_qdrant_groups():
        try:
            groups = q.query_groups(vector, terms, n_cand, flt, group_by="doc_id", group_size=depth,
                                    prefer_lessons=bool(prefer_lessons))
        except FleetRagError as e:
            if not _groups_unsupported(e):
                raise
    if groups is None:
        groups = fused_groups(q, vector, terms, n_cand, depth, flt, prefer_lessons=bool(prefer_lessons))
    hits = _grouped_candidates(groups, per_doc)
    mode = "hybrid" if terms else "dense"
    if will_rerank and _apply_rerank(cfg, query, hits):
        mode += "+rerank"
    hits_out = hits[:limit]
    latency_ms = int((time.time() - t0) * 1000)
    search_log.log_search(query=query, seat=seat, app=app, hits_count=len(hits_out),
                          latency_ms=latency_ms, mode=mode)
    return {"hits": hits_out, "mode": mode}


# --------------------------------------------------------------------------- stats

def recall_stats() -> dict:
    """Collection status plus counts by source and by app.

    by_app always carries an "other" bucket (points whose app is not in KNOWN_APPS, including
    the ingest sentinel), so sum(by_app.values()) == points.
    """
    cfg = get_config(need_write=False)
    q = Qdrant(cfg)
    info = q.info()
    by_source = {s: q.count(match_filter(source=s)) for s in SOURCES}
    by_app = {}
    for a in KNOWN_APPS:
        n = q.count(match_filter(app=a))
        if n:
            by_app[a] = n
    by_app["other"] = q.count({"must_not": [{"key": "app", "match": {"any": list(KNOWN_APPS)}}]})
    searches = search_log.search_summary(days=7)
    return {
        "collection": q.collection,
        "status": info.get("status", "?"),
        "points": info.get("points_count", 0),
        "embedder_healthy": bool(embedder_healthy(cfg)),
        "rerank_healthy": bool(rerank_healthy(cfg)) if rerank_configured(cfg) else None,
        "by_source": by_source,
        "by_app": by_app,
        "searches_7d": searches["total_searches"],
        "searches_by_seat_7d": searches["by_seat"],
    }


# --------------------------------------------------------------------------- contribute

def _still_secret(text: str) -> bool:
    """True when gitleaks still flags the scrubbed text.

    Fails CLOSED: if gitleaks cannot deliver a verdict (scrub.GitleaksError or any other
    exception from the gate) a FleetRagError is raised so the caller refuses the contribution.
    Only call this when gitleaks_available() is true.
    """
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write(json.dumps({"text": text}) + "\n")
            path = fh.name
        try:
            return bool(gitleaks_flagged(path))
        except FleetRagError:
            raise
        except Exception as e:  # noqa: BLE001 - GitleaksError or anything else: refuse, class only
            raise FleetRagError("refusing: the gitleaks gate could not verify the scrubbed text "
                                f"({type(e).__name__}); nothing was stored") from None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def recall_contribute(text: str, category: str, app: str = "fleet", seat: str | None = None,
                      title: str | None = None, url: str | None = None) -> dict:
    """Store one reusable lesson / preference / infrastructure fact / decision / runbook.

    Guardrails: 40..4000 chars after strip, category from CONTRIB_CATEGORIES, seat required
    (falls back to $AGENT_SEAT), secrets scrubbed and reported, refused if gitleaks still
    flags the scrubbed text or cannot run.  When gitleaks is not installed the regex scrub
    alone is applied and "gitleaks-unavailable" is appended to the returned scrubbed list.
    Requires the write key.
    """
    if not isinstance(text, str):
        raise FleetRagError("text must be a string")
    text = text.strip()
    if len(text) < CONTRIB_MIN:
        raise FleetRagError(f"text too short: {len(text)} chars (minimum {CONTRIB_MIN})")
    if len(text) > CONTRIB_MAX:
        raise FleetRagError(f"text too long: {len(text)} chars (maximum {CONTRIB_MAX})")
    if not isinstance(category, str):
        raise FleetRagError(f"category must be a string, not {type(category).__name__}")
    if category not in CONTRIB_CATEGORIES:
        raise FleetRagError("category must be one of " + "|".join(CONTRIB_CATEGORIES))
    seat = (_opt_str("seat", seat) or os.environ.get("AGENT_SEAT") or "").strip().upper()
    if not seat:
        raise FleetRagError("seat is required (pass seat=... or set AGENT_SEAT)")
    if not _SEAT_RE.match(seat):
        raise FleetRagError("seat must be an uppercase tag like CLAUDE or GROK-BOT")
    app = (_opt_str("app", app) or "fleet").lower()
    if not _APP_RE.match(app):
        raise FleetRagError("app must be a lowercase slug like fleet or socratic-trade")
    title = _opt_str("title", title) or ""
    url = _opt_str("url", url) or ""
    if url and not re.match(r"^https?://", url):
        raise FleetRagError("url must start with http:// or https://")

    clean, kinds = scrub(text)
    reported = list(kinds)
    if gitleaks_available():
        if _still_secret(clean):
            raise FleetRagError("refusing: text still looks like it contains a secret after scrubbing")
    else:
        reported.append(GITLEAKS_UNAVAILABLE)

    cfg = get_config(need_write=True)
    ts = now_ms()
    day = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
    doc_id = f"contrib/{seat}/{day}/{content_hash(clean)[:8]}"
    payload: dict[str, Any] = {
        "source": "agent-contribution",
        "app": app,
        "category": category,
        "seat": seat,
        "doc_id": doc_id,
        "chunk_index": 0,
        "chunk_count": 1,
        "heading": "",
        "title": title,
        "url": url,
        "path": "",
        "created_at": ts,
        "updated_at": ts,
        "ingest_run": f"contrib-{day}",
    }
    if kinds:
        payload["scrubbed"] = kinds
    point = build_point(clean, payload)
    point["vector"] = embed(cfg, [clean])[0]
    status = Qdrant(cfg).upsert([point])
    return {"id": point["id"], "doc_id": doc_id, "scrubbed": reported, "status": status}


# --------------------------------------------------------------------------- fake backend

class FakeQdrant:
    """In-memory stand-in with the subset of core.Qdrant the recall functions use.

    The store is class-level so a server process and its tests see one collection.  Every
    query_hybrid / search_dense call is appended to `calls` as (vector, terms, limit, filter).
    Filters support must / must_not / should, nested filters, match value / any / text, and
    range gte, which is the subset the recall functions and core.Qdrant.query_hybrid emit.
    """

    points: list[dict] = []
    calls: list[tuple] = []
    group_calls: list[dict] = []
    upserts: list[list[dict]] = []

    def __init__(self, cfg: dict[str, str], collection: str | None = None):
        self.collection = collection or cfg.get("QDRANT_FLEET_COLLECTION", "fleet-agents-fake")

    @classmethod
    def reset(cls, seed: bool = True) -> None:
        cls.points, cls.calls, cls.group_calls, cls.upserts = [], [], [], []
        if seed:
            cls.points.extend(_fake_seed())

    def healthz(self) -> bool:
        return True

    def info(self) -> dict:
        return {"status": "green", "points_count": len(self.points), "payload_schema": {}}

    @classmethod
    def _cond(cls, p: dict, cond: dict) -> bool:
        if any(k in cond for k in ("must", "must_not", "should")):
            return cls._matches(p, cond)
        val = p["payload"].get(cond.get("key"))
        if "match" in cond:
            m = cond["match"]
            if "value" in m:
                return val == m["value"]
            if "any" in m:
                return val in m["any"]
            if "text" in m:
                return str(m["text"]).lower() in str(val or "").lower()
        if "range" in cond:
            return isinstance(val, (int, float)) and val >= cond["range"]["gte"]
        return True

    @classmethod
    def _matches(cls, p: dict, flt: dict | None) -> bool:
        if not flt:
            return True
        if not all(cls._cond(p, c) for c in flt.get("must", [])):
            return False
        if any(cls._cond(p, c) for c in flt.get("must_not", [])):
            return False
        should = flt.get("should") or []
        return not should or any(cls._cond(p, c) for c in should)

    def count(self, flt: dict | None = None, exact: bool = True) -> int:
        return sum(1 for p in self.points if self._matches(p, flt))

    def search_dense(self, vector: list[float], limit: int = 5, flt: dict | None = None) -> list[dict]:
        return self.query_hybrid(vector, [], limit, flt, prefer_lessons=False)

    @staticmethod
    def _is_lesson(p: dict) -> bool:
        pl = p["payload"]
        return pl.get("source") == LESSON_SOURCE and pl.get("category") in LESSON_CATEGORIES

    def _scored(self, vector: list[float], terms: list[str], flt: dict | None,
                prefer_lessons: bool) -> list[dict]:
        """Keyword-overlap score (0.5 + 0.1 per matching term); lessons get +0.3 when boosted.
        Ties keep insertion order, so a seeded point outranks a later identical one."""
        scored = []
        for p in self.points:
            if not self._matches(p, flt):
                continue
            low = p["payload"].get("text", "").lower()
            overlap = sum(1 for t in terms if t in low)
            score = 0.5 + 0.1 * overlap + (0.3 if prefer_lessons and self._is_lesson(p) else 0.0)
            scored.append({"id": p["id"], "score": round(score, 4), "payload": p["payload"]})
        scored.sort(key=lambda h: -h["score"])
        return scored

    def query_hybrid(self, vector: list[float], terms: list[str], limit: int = 5,
                     flt: dict | None = None, prefetch_limit: int | None = None,
                     prefer_lessons: bool = True) -> list[dict]:
        FakeQdrant.calls.append((vector, terms, limit, flt))
        return self._scored(vector, terms, flt, prefer_lessons)[:limit]

    def query_groups(self, vector: list[float], terms: list[str], limit: int = 5,
                     flt: dict | None = None, group_by: str = "doc_id", group_size: int = 1,
                     prefetch_limit: int | None = None, prefer_lessons: bool = True) -> list[dict]:
        """Groups built on top of query_hybrid, so a test subclass that overrides query_hybrid
        (to raise, to record) sees the grouped path too."""
        FakeQdrant.group_calls.append({"limit": limit, "group_by": group_by, "group_size": group_size,
                                       "prefer_lessons": prefer_lessons, "flt": flt, "terms": terms})
        size = max(1, group_size)
        n = limit
        while True:
            pts = self.query_hybrid(vector, terms, n, flt, prefer_lessons=prefer_lessons)
            groups = group_hits(pts, group_by, size)
            if len(groups) >= limit or len(pts) < n:
                return groups[:limit]
            n *= 2                      # same doc dominated: widen until `limit` groups are filled

    def upsert(self, points: list[dict], wait: bool = True) -> str:
        FakeQdrant.upserts.append(points)
        ids = {p["id"] for p in points}
        FakeQdrant.points = [p for p in self.points if p["id"] not in ids] + [
            {"id": p["id"], "payload": p["payload"]} for p in points]
        return "completed"


def _fake_seed() -> list[dict]:
    rows = [
        ("Never grep, cat, or Read ~/.secrets/global-api-keys for KEY=value lines - that prints "
         "secret values into the transcript.  List names only.", "preference", "fleet"),
        ("The fleet vector database runs as Coolify service qdrant-st on the shared Hetzner box, "
         "bound to the Tailscale mesh address on port 6333.", "infrastructure", "fleet"),
        ("Commit and land finished work without waiting to be asked; unpushed work is invisible "
         "to peer agents and gets redone.", "preference", "fleet"),
    ]
    out = []
    for i, (text, cat, app) in enumerate(rows):
        pt = build_point(text, {"source": "doc", "app": app, "category": cat, "seat": "CLAUDE",
                                "doc_id": f"seed/fleet-standards-{i}", "chunk_index": 0, "chunk_count": 1,
                                "heading": "", "title": "Fleet standards seed", "url": "", "path": "",
                                "created_at": 1756684800000, "updated_at": 1756684800000,
                                "ingest_run": "fake"})
        out.append({"id": pt["id"], "payload": pt["payload"]})
    return out


def install_fake_backend(seed: bool = True) -> None:
    """Point every seam at in-process fakes (no network, no credentials, no gitleaks)."""
    global load_config, embed, embedder_healthy, rerank_healthy, Qdrant, rerank, gitleaks_flagged, gitleaks_available
    FakeQdrant.reset(seed=seed)
    load_config = lambda need_write=False, extra=(): {  # noqa: E731
        "TEI_URL": "http://fake", "TEI_API_KEY": "fake", "QDRANT_URL": "http://fake",
        "QDRANT_FLEET_COLLECTION": "fleet-agents-fake", "QDRANT_API_KEY": "fake",
        "QDRANT_READONLY_API_KEY": "fake"}
    embed = lambda cfg, texts: [[0.0] * 4 for _ in texts]  # noqa: E731
    embedder_healthy = lambda cfg: True  # noqa: E731
    rerank_healthy = lambda cfg: True  # noqa: E731 - fake config has no rerank keys, so unused
    Qdrant = FakeQdrant
    rerank = core.rerank                  # real client; the fake config has no rerank keys
    gitleaks_flagged = lambda path, timeout=300: set()  # noqa: E731
    gitleaks_available = lambda: True  # noqa: E731
    reset_config_cache()
