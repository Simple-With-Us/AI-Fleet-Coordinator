#!/usr/bin/env bash
# Claude Code SessionStart hook: one line of additionalContext reminding the session that the
# fleet recall corpus exists (N points), to search before re-deriving and to contribute at
# closeout.  Installed to ~/.claude/hooks/ by scripts/install-fleet-rag.sh --hooks.
#
# Speed: the point count is served from a small cache file
# ($HOME/apps/fleet-rag/state/hook-points-cache.json, TTL $FLEET_RECALL_HOOK_CACHE_TTL seconds,
# default 6 h).  A stale cache is used as-is and refreshed in the background (`recall stats
# --json` takes several seconds live).  With no cache at all the hook tries `recall stats
# --json` once with a 3 s timeout, then falls back to the last-ingest date from
# state/last-run.json.  On ANY error it prints nothing and exits 0.  FLEET_RECALL_HOOKS=0
# disables it.
#
# A point count <= 0 (a served-but-empty corpus, a cache from before this fix, or any other
# zero/negative reading) is always treated as unknown, never printed as "0 points" and never
# written into the cache -- a zero would tell every agent the corpus is empty and not worth
# searching, which is the worst possible message, and writing it would overwrite a good cached
# count with a bad one.  An unknown count falls through to the last-ingest-date message.
#
# Stdin carries the hook JSON (session_id, transcript_path, source); it is drained and unused.
[ "${FLEET_RECALL_HOOKS:-1}" = "0" ] && { cat >/dev/null 2>&1; exit 0; }
cat >/dev/null 2>&1
exec python3 - "$HOME" <<'PY' 2>/dev/null
import datetime, json, os, subprocess, sys, time

try:
    home = sys.argv[1]
    state = os.path.join(home, "apps", "fleet-rag", "state")
    cache = os.path.join(state, "hook-points-cache.json")
    ttl = float(os.environ.get("FLEET_RECALL_HOOK_CACHE_TTL", 6 * 3600))
    now = time.time()

    def read_json(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def recall_bin():
        for cand in (os.path.join(home, "apps", "fleet-rag", "recall"),
                     os.path.join(home, ".local", "bin", "recall")):
            if os.access(cand, os.X_OK):
                return cand
        return "recall"

    def write_cache(points):
        os.makedirs(state, exist_ok=True)
        tmp = cache + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"points": int(points), "at": time.time()}, fh)
        os.replace(tmp, cache)

    points = None
    cached = read_json(cache)
    stale = True
    if isinstance(cached, dict) and isinstance(cached.get("points"), int) and cached["points"] > 0:
        points = cached["points"]
        stale = now - float(cached.get("at") or 0) > ttl

    if points is None:
        # No cache at all (or a cached count <= 0, treated the same as no cache): one bounded
        # foreground attempt.
        try:
            out = subprocess.run([recall_bin(), "stats", "--json"], capture_output=True, text=True,
                                 timeout=3, env={**os.environ, "FLEET_RECALL_HOOKS": "0"})
            data = json.loads(out.stdout) if out.returncode == 0 else None
            if isinstance(data, dict) and isinstance(data.get("points"), int) and data["points"] > 0:
                points = data["points"]
                write_cache(points)
                stale = False
        except Exception:
            pass

    if stale and os.environ.get("FLEET_RECALL_HOOK_NO_REFRESH") != "1":
        # Refresh in the background; the current session never waits for it.  A count <= 0 is
        # never written -- it would overwrite a good cached count with a bad one.
        refresher = (
            "import json,os,subprocess,sys,time\n"
            "b,c=sys.argv[1],sys.argv[2]\n"
            "try:\n"
            "  o=subprocess.run([b,'stats','--json'],capture_output=True,text=True,timeout=60)\n"
            "  d=json.loads(o.stdout)\n"
            "  p=int(d['points'])\n"
            "  if p>0:\n"
            "    os.makedirs(os.path.dirname(c),exist_ok=True)\n"
            "    t=c+'.%d.tmp'%os.getpid()\n"
            "    json.dump({'points':p,'at':time.time()},open(t,'w'))\n"
            "    os.replace(t,c)\n"
            "except Exception:\n"
            "  pass\n")
        try:
            subprocess.Popen([sys.executable, "-c", refresher, recall_bin(), cache],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "FLEET_RECALL_HOOKS": "0"})
        except Exception:
            pass

    if points is not None:
        corpus = f"fleet recall corpus {points:,} points"
    else:
        last = read_json(os.path.join(state, "last-run.json"))
        if not (isinstance(last, dict) and last.get("finished_at") and last.get("ok")):
            sys.exit(0)
        day = datetime.datetime.fromtimestamp(int(last["finished_at"]) / 1000).strftime("%Y-%m-%d")
        corpus = f"fleet recall corpus available (last ingest {day})"
    line = (f"{corpus}; PRE-FLIGHT RETRIEVAL: search recall at the start of turns/tasks before diagnosing "
            "(triggers: error/fail, infra/deploy, cross-repo/domain, pre-owner query). "
            "Contribute a lesson at closeout (recall_contribute)")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": line}}))
except Exception:
    pass
sys.exit(0)
PY
