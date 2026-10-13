#!/usr/bin/env bash
# otel-lane-tag.sh — tag a Claude Code lane's untracked .claude/settings.local.json
# with OTEL_RESOURCE_ATTRIBUTES=seat=<seat>,lane=<lane-name>,project=<repo>, so the
# lane's Claude Code sessions carry seat/lane/project attribution into Sentry
# `agent-sessions` (and `project=` also drives Usage Monitor's per-project cost
# attribution — see UM AGENTS.md "Per-project cost attribution").
#
# On-demand helper — not a daemon, nothing to load/bootout.  Run it:
#   - right after creating a new lane under ~/apps/lanes/<Repo>/<seat>-<slug>
#     (layout v2, owner 2026-10-09; a lane still in an old ~/apps/lanes/<prefix>/ folder, or an
#     old flat lane ~/apps/<prefix>-<seat>[-suffix], works too)
#   - with --all to sweep every existing lane under ~/apps once
#
# Layout v2: the folder above a lane is the repo's folder under ~/Code (codeDir in fleet-apps.json),
# no longer the lowercase worktree prefix, so it is mapped back to the app through fleet-apps.json.
# The telemetry lane name stays <prefix>-<seat>-<slug>, the shape dashboards already group on.  A
# Claude desktop worktree (<slug>-<6 hex>, no seat in the name) is tagged seat=claude.  A review
# checkout (review-pr-<n>) and a tool folder (_codex, _managed, _review) are not lanes and are skipped,
# and so is a symlink left at an old lane path by the layout migration.
#
# External lanes (owner 2026-10-10): ~/apps/lanes/<Repo> may be a symlink onto the external disk
# (FLEET_LANES_EXTERNAL_ROOT, default /Volumes/External/Lanes; set but empty turns it off).  The path agents
# use is still ~/apps/lanes/<Repo>/<lane>, but a shell whose cwd is the real path (/Volumes/External/Lanes/...)
# is the same lane, so such a path is read at its ~/apps/lanes spelling before it is judged.
#
# It ONLY ever touches the untracked `.claude/settings.local.json` file inside
# a lane, and inside that file it only ever sets `env.OTEL_RESOURCE_ATTRIBUTES`
# — every other key in that file (and the rest of the lane) is left alone.
# Skips anything under ~/Code (those are human/reset trees, never lanes),
# anything that is not a git worktree, and any lane whose name does not
# cleanly parse as `<known-seat>[-slug]` under a known repo folder, or as
# `<known-prefix>-<known-seat>[-suffix]` flat (logged, not fatal).
# If the lane already sets a `project=` value of its own via `.envrc`
# (direnv), it is left untouched rather than overridden.
#
# Usage:
#   otel-lane-tag.sh                  # tag $PWD
#   otel-lane-tag.sh /path/to/lane    # tag one lane
#   otel-lane-tag.sh --all            # sweep every lane under ~/apps
#
# Source of truth for prefix/codeDir -> repo and seat -> worktreeSuffix is
# ai-fleet-coordinator's fleet-apps.json, read from origin/main (never the
# possibly-stale ~/Code checkout — see AGENT-SYNC.md "Where to work").
#
# Tracked in AI-Fleet-Coordinator (scripts/otel-lane-tag.sh) since layout v2; the working copy
# on the Mac is ~/apps/otel-lane-tag.sh (copy it over after a merge).  Test: scripts/test-otel-lane-tag.sh.
set -euo pipefail

APPS_ROOT="${OTEL_LANE_TAG_APPS_ROOT:-$HOME/apps}"
LANES_EXTERNAL_ROOT="${FLEET_LANES_EXTERNAL_ROOT-/Volumes/External/Lanes}"
LANES_EXTERNAL_ROOT="${LANES_EXTERNAL_ROOT%/}"
FLEET_REPO="${OTEL_LANE_TAG_FLEET_REPO:-$HOME/Code/ai-fleet-coordinator}"

fetch_fleet_apps_json() {
  git -C "$FLEET_REPO" fetch -q origin main 2>/dev/null || true
  git -C "$FLEET_REPO" show origin/main:fleet-apps.json 2>/dev/null
}

FLEET_APPS_JSON="$(fetch_fleet_apps_json)"
if [ -z "$FLEET_APPS_JSON" ]; then
  echo "otel-lane-tag: could not read fleet-apps.json from origin/main via $FLEET_REPO; aborting" >&2
  exit 1
fi

tag_one_lane() {
  local lane_dir="$1"
  if [ ! -d "$lane_dir" ]; then
    echo "skip  $lane_dir  (no such directory)"
    return 0
  fi
  lane_dir="$(cd "$lane_dir" && pwd)"
  local lane_name lane_parent="" place="$lane_dir"
  lane_name="$(basename "$lane_dir")"
  # `place` is where the lane sits in the map: a real path on the external lanes disk is read at its
  # ~/apps/lanes spelling.  `lane_dir` stays as given, for git and for the file that is written.
  if [ -n "$LANES_EXTERNAL_ROOT" ]; then
    case "$lane_dir" in
      "$LANES_EXTERNAL_ROOT"/*) place="$APPS_ROOT/lanes/${lane_dir#"$LANES_EXTERNAL_ROOT"/}" ;;
    esac
  fi

  case "$place" in
    "$APPS_ROOT"/*) ;;
    *)
      printf '%-45s skip  not under ~/apps\n' "$lane_name"
      return 0
      ;;
  esac

  # Lane Map v2 (owner 2026-10-09): lanes live at ~/apps/lanes/<Repo>/<seat>-<slug>.  Review checkouts
  # (review-pr-<n>) and tool folders (_codex, _managed, _review) are not lanes.  The folder above a
  # nested lane is handed to the parser, which maps it to the app (a repo folder, or an old prefix).
  case "$place" in
    "$APPS_ROOT"/lanes/_*)
      printf '%-45s skip  review or tool-managed folder, not a lane\n' "$lane_name"
      return 0
      ;;
    "$APPS_ROOT"/lanes/*/review-pr-*)
      printf '%-45s skip  review checkout, not a lane\n' "$lane_name"
      return 0
      ;;
    "$APPS_ROOT"/lanes/*/*)
      lane_parent="$(basename "$(dirname "$place")")"
      ;;
    "$APPS_ROOT"/lanes|"$APPS_ROOT"/lanes/*)
      printf '%-45s skip  not a lane folder\n' "$lane_name"
      return 0
      ;;
  esac

  if ! git -C "$lane_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    printf '%-45s skip  not a git worktree\n' "$lane_name"
    return 0
  fi

  local result
  result="$(FLEET_APPS_JSON="$FLEET_APPS_JSON" LANE_DIR="$lane_dir" LANE_NAME="$lane_name" LANE_PARENT="$lane_parent" python3 <<'PYEOF'
import json, os, re, sys

fleet = json.loads(os.environ["FLEET_APPS_JSON"])
lane_dir = os.environ["LANE_DIR"]
lane_name = os.environ["LANE_NAME"]
lane_parent = os.environ.get("LANE_PARENT", "")

# Known prefixes (longest first, so "fleet-ops" beats "fleet").
prefixes = sorted(
    ((a["worktreePrefix"], a["repo"]) for a in fleet["apps"]),
    key=lambda kv: -len(kv[0]),
)

# Known seat suffixes, plus the abbreviations actually seen in lane names on
# this Mac (mm -> minimax, ag -> antigravity).  Longest match string first.
seat_matches = [(s["worktreeSuffix"], s["worktreeSuffix"]) for s in fleet["seats"]]
seat_matches += [("mm", "minimax"), ("ag", "antigravity")]
seat_matches = sorted(set(seat_matches), key=lambda kv: -len(kv[0]))

prefix = repo = rest = None
if lane_parent:
    # Layout v2: ~/apps/lanes/<Repo>/<name>.  The folder is the app's codeDir (any case, since
    # the volume is case-insensitive); the pre-v2 lowercase worktree prefix still maps too, so a
    # lane that has not migrated yet keeps its tag.
    want = lane_parent.lower()
    for a in fleet["apps"]:
        if want in (a.get("codeDir", a["repo"]).lower(), a["repo"].lower(), a["worktreePrefix"].lower()):
            prefix, repo = a["worktreePrefix"], a["repo"]
            break
    if prefix is None:
        print("skip  unknown repo folder")
        sys.exit(0)
    rest = lane_name
    lane_name = f"{prefix}-{lane_name}"
else:
    for pfx, rp in prefixes:
        if lane_name == pfx or lane_name.startswith(pfx + "-"):
            prefix, repo = pfx, rp
            rest = lane_name[len(pfx):].lstrip("-")
            break
    if prefix is None:
        print("skip  unknown app prefix")
        sys.exit(0)

seat = None
for match_str, canonical in seat_matches:
    if rest == match_str or rest.startswith(match_str + "-"):
        seat = canonical
        break
if seat is None and lane_parent and re.search(r"-[0-9a-f]{6}$", rest):
    # A Claude desktop worktree (<slug>-<6 hex>) has no seat in its name; the desktop app is the
    # Claude account.  The lane name keeps the seat so it groups with the other claude lanes.
    seat = "claude"
    lane_name = f"{prefix}-claude-{rest}"
if seat is None:
    print("skip  unknown seat (lane does not follow <seat>-<slug> or <prefix>-<seat>[-suffix])")
    sys.exit(0)

# A lane that already sets its own project attribution via direnv keeps it.
envrc = os.path.join(lane_dir, ".envrc")
if os.path.exists(envrc):
    with open(envrc) as f:
        text = f.read()
    if re.search(r"(?m)^\s*(export\s+)?project\s*=", text) or "OTEL_RESOURCE_ATTRIBUTES" in text:
        print("skip  .envrc already sets project attribution")
        sys.exit(0)

# NOTE (verified 2026-09-24, CLAUDE): Sentry's OTLP log ingest reserves the
# top-level key "project" for its own project slug and silently overwrites an
# incoming resource attribute of the same name -- exactly the same collision
# already observed for "organization.id" (see the Sept 2026 Sentry agent
# telemetry eval). `project=<repo>` is kept because Usage Monitor's own
# OTLP-metrics project-resolver reads that exact key (AGENTS.md "Per-project
# cost attribution"), and metrics go to UM, not through this collision.
# `repo=<repo>` is a second, Sentry-safe copy of the same value so Sentry
# dashboards/alerts can actually filter agent-sessions by app.
attrs = f"seat={seat},lane={lane_name},project={repo},repo={repo}"

settings_dir = os.path.join(lane_dir, ".claude")
settings_path = os.path.join(settings_dir, "settings.local.json")
os.makedirs(settings_dir, exist_ok=True)

if os.path.exists(settings_path):
    with open(settings_path) as f:
        raw = f.read().strip()
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        print(f"skip  {settings_path} is not a JSON object")
        sys.exit(0)
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
    if env.get("OTEL_RESOURCE_ATTRIBUTES") == attrs:
        print(f"noop  already tagged: {attrs}")
        sys.exit(0)
    env["OTEL_RESOURCE_ATTRIBUTES"] = attrs
    data["env"] = env
    action = "merged"
else:
    data = {"env": {"OTEL_RESOURCE_ATTRIBUTES": attrs}}
    action = "created"

with open(settings_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

print(f"{action} {attrs}")
PYEOF
)"
  printf '%-45s %s\n' "${lane_parent:+$lane_parent/}$lane_name" "$result"
}

if [ "${1:-}" = "--all" ]; then
  for d in "$APPS_ROOT"/*/; do
    [ -d "$d" ] || continue
    case "${d%/}" in "$APPS_ROOT"/lanes) continue ;; esac   # nested lanes are swept below
    tag_one_lane "${d%/}"
  done
  for d in "$APPS_ROOT"/lanes/*/*/; do
    [ -d "$d" ] || continue
    [ -L "${d%/}" ] && continue                              # a symlink the layout migration left at an old path
    case "${d%/}" in "$APPS_ROOT"/lanes/_*) continue ;; esac
    tag_one_lane "${d%/}"
  done
else
  tag_one_lane "${1:-$PWD}"
fi
