#!/usr/bin/env bash
# Test for otel-lane-tag.sh (layout v2).  Run by hand:  bash scripts/test-otel-lane-tag.sh
# A fake apps root and a fake fleet repo; nothing outside the temp directory is read or written.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/otel-lane-tag-test.XXXXXX")"
T="$(cd "$T" && pwd -P)"
trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
FLEET="$T/Code/ai-fleet-coordinator"; APPS="$T/apps"
mkdir -p "$FLEET" "$APPS/lanes"
cat > "$FLEET/fleet-apps.json" <<'JSON'
{"apps": [
  {"repo": "BotFleet", "codeDir": "BotFleet", "worktreePrefix": "botfleet"},
  {"repo": "Congress.Trade", "codeDir": "Congress.Trade", "worktreePrefix": "congress"},
  {"repo": "Socratic-Trade", "codeDir": "Socratic-Trade", "worktreePrefix": "trading"},
  {"repo": "AI-Fleet-Coordinator", "codeDir": "AI-Fleet-Coordinator", "worktreePrefix": "fleet"},
  {"repo": "fleet-ops", "codeDir": "Fleet-OPS", "worktreePrefix": "fleet-ops"}
 ],
 "seats": [{"tag": "CLAUDE", "worktreeSuffix": "claude"}, {"tag": "MM", "worktreeSuffix": "minimax"}]}
JSON
git -C "$FLEET" init -q -b main && git -C "$FLEET" add fleet-apps.json && git -C "$FLEET" commit -q -m registry
git -C "$FLEET" update-ref refs/remotes/origin/main HEAD

lane() { mkdir -p "$APPS/$1"; git init -q "$APPS/$1"; }
run() { OTEL_LANE_TAG_APPS_ROOT="$APPS" OTEL_LANE_TAG_FLEET_REPO="$FLEET" bash "$HERE/otel-lane-tag.sh" "$@"; }
attrs() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["env"]["OTEL_RESOURCE_ATTRIBUTES"])' "$APPS/$1/.claude/settings.local.json"; }
tagged() { [ -e "$APPS/$1/.claude/settings.local.json" ]; }

lane lanes/BotFleet/claude-x
lane lanes/botfleet/claude-y
lane lanes/Congress.Trade/minimax-z
lane lanes/Fleet-OPS/claude-ops
lane lanes/BotFleet/fix-it-a1b2c3
lane lanes/BotFleet/review-pr-7
lane lanes/_codex/slug/BotFleet
lane lanes/_managed/fleet/runtime
lane lanes/nonesuch/claude-x
lane lanes/BotFleet/notes
lane trading-claude-q
lane lanes/AI-Fleet-Coordinator/claude-real
mkdir -p "$APPS/lanes/fleet"; ln -s "$APPS/lanes/AI-Fleet-Coordinator/claude-real" "$APPS/lanes/fleet/claude-old"

run "$APPS/lanes/BotFleet/claude-x" >/dev/null
[ "$(attrs lanes/BotFleet/claude-x)" = "seat=claude,lane=botfleet-claude-x,project=BotFleet,repo=BotFleet" ] || fail "v2 lane: $(attrs lanes/BotFleet/claude-x)"
run "$APPS/lanes/botfleet/claude-y" >/dev/null
[ "$(attrs lanes/botfleet/claude-y)" = "seat=claude,lane=botfleet-claude-y,project=BotFleet,repo=BotFleet" ] || fail "an old prefix folder still tags"
run "$APPS/lanes/Congress.Trade/minimax-z" >/dev/null
[ "$(attrs lanes/Congress.Trade/minimax-z)" = "seat=minimax,lane=congress-minimax-z,project=Congress.Trade,repo=Congress.Trade" ] || fail "uppercase and dot folder"
run "$APPS/lanes/Fleet-OPS/claude-ops" >/dev/null
[ "$(attrs lanes/Fleet-OPS/claude-ops)" = "seat=claude,lane=fleet-ops-claude-ops,project=fleet-ops,repo=fleet-ops" ] || fail "Fleet-OPS folder"
run "$APPS/lanes/BotFleet/fix-it-a1b2c3" >/dev/null
[ "$(attrs lanes/BotFleet/fix-it-a1b2c3)" = "seat=claude,lane=botfleet-claude-fix-it-a1b2c3,project=BotFleet,repo=BotFleet" ] || fail "desktop worktree is tagged claude"
run "$APPS/trading-claude-q" >/dev/null
[ "$(attrs trading-claude-q)" = "seat=claude,lane=trading-claude-q,project=Socratic-Trade,repo=Socratic-Trade" ] || fail "flat lane"
again="$(run "$APPS/lanes/BotFleet/claude-x")"
case "$again" in *noop*) ;; *) fail "a second run is a noop: $again" ;; esac

for skip in lanes/BotFleet/review-pr-7 lanes/_codex/slug/BotFleet lanes/_managed/fleet/runtime lanes/nonesuch/claude-x lanes/BotFleet/notes; do
  run "$APPS/$skip" >/dev/null
  tagged "$skip" && fail "must not tag $skip"
done
out="$(run "$APPS/lanes/nonesuch/claude-x")"; case "$out" in *"unknown repo folder"*) ;; *) fail "reason: $out" ;; esac
out="$(run "$APPS/lanes/BotFleet/review-pr-7")"; case "$out" in *"review checkout"*) ;; *) fail "reason: $out" ;; esac

# --all sweeps nested lanes, skips the migration's symlink, and tags each lane once
rm -rf "$APPS/lanes/BotFleet/claude-x/.claude"
run --all >/dev/null
tagged lanes/BotFleet/claude-x || fail "--all tags nested lanes"
tagged lanes/AI-Fleet-Coordinator/claude-real || fail "--all tags the real lane"
[ -L "$APPS/lanes/fleet/claude-old" ] || fail "the symlink must still be a symlink"
[ "$(attrs lanes/AI-Fleet-Coordinator/claude-real)" = "seat=claude,lane=fleet-claude-real,project=AI-Fleet-Coordinator,repo=AI-Fleet-Coordinator" ] \
  || fail "the real lane keeps its own tag, not the symlink's"
# External lanes: lanes/<Repo> is a symlink onto the external disk.  The lane tags the same through the symlink
# and through its real path, and the sweep reaches it.
EXT="$T/ext/Lanes"
mkdir -p "$EXT/Socratic-Trade/claude-ext" "$EXT/Socratic-Trade/review-pr-9" "$EXT/_codex/slug/BotFleet"
git init -q "$EXT/Socratic-Trade/claude-ext"; git init -q "$EXT/Socratic-Trade/review-pr-9"
ln -s "$EXT/Socratic-Trade" "$APPS/lanes/Socratic-Trade"
want="seat=claude,lane=trading-claude-ext,project=Socratic-Trade,repo=Socratic-Trade"
run "$APPS/lanes/Socratic-Trade/claude-ext" >/dev/null
[ "$(attrs lanes/Socratic-Trade/claude-ext)" = "$want" ] || fail "a lane behind a symlink: $(attrs lanes/Socratic-Trade/claude-ext)"
rm -rf "$EXT/Socratic-Trade/claude-ext/.claude"
out="$(FLEET_LANES_EXTERNAL_ROOT="$EXT" run "$EXT/Socratic-Trade/claude-ext")"
case "$out" in *"trading-claude-ext"*created*|*"trading-claude-ext"*) ;; *) fail "real path: $out" ;; esac
[ "$(attrs lanes/Socratic-Trade/claude-ext)" = "$want" ] || fail "the real path tags the same lane: $(attrs lanes/Socratic-Trade/claude-ext)"
out="$(FLEET_LANES_EXTERNAL_ROOT="$EXT" run "$EXT/Socratic-Trade/review-pr-9")"
case "$out" in *"review checkout"*) ;; *) fail "a review on the external disk is still a review: $out" ;; esac
out="$(FLEET_LANES_EXTERNAL_ROOT="$EXT" run "$EXT/_codex/slug/BotFleet")"
case "$out" in *"tool-managed"*) ;; *) fail "a Codex folder on the external disk is still a tool folder: $out" ;; esac
rm -rf "$EXT/Socratic-Trade/claude-ext/.claude"
out="$(run "$EXT/Socratic-Trade/claude-ext")"
case "$out" in *"not under ~/apps"*) ;; *) fail "without the setting the real path is outside the map: $out" ;; esac
rm -rf "$EXT/Socratic-Trade/claude-ext/.claude"
run --all >/dev/null
tagged lanes/Socratic-Trade/claude-ext || fail "--all reaches a lane behind a symlinked repo folder"
echo OK
