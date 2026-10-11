#!/bin/bash
# shellcheck disable=SC2034,SC2154  # globals here are read and set by the sourced janitor functions (n_reap, lean, ...)
# Contract: the lanes-guard gates in disk-janitor.sh (board a7dfde0e Lane Map, board 912034a0 worktree vanish) and the
# matching guards in mac-auto-cleanup.sh's pressure reaper (3b).
#
#   * A lane under ~/apps/lanes retires ONLY when a fresh lane doctor report lists it in cleaner_candidates at the HEAD
#     it has now (the cleaner contract in scripts/fleet_lanes/README.md).  A missing, stale, future-dated, malformed or
#     schema-1 report keeps it, and so does a commit made after the report.
#   * Birth time governs age: a fresh lane is kept even with back-dated files and even if a report lists it.
#   * .janitor-keep, a process cwd, a failed or killed lsof, a killed activity scan, a failed git call, and a checkout
#     nested inside the lane all keep the lane (and its node_modules).
#   * janitor_pr_merged matches the PR by head sha, so a reused branch name never reads as merged.
#   * Every repo root, under either letter case, is protected by KEEP_RE, and the integration tree is never touched.
#
# Hermetic: everything lives under a mktemp directory with a FAKE HOME; gh, lsof and the lane doctor are shims (find
# gets a shim only to force a scan timeout).  Real git, real /bin/bash 3.2 (this file re-execs itself under it).  The
# janitor is sourced in library mode (JANITOR_LIB_ONLY=1), which defines functions and runs nothing; every path the
# sourced code could write to is checked to sit under the temp root BEFORE any phase function is called.
# The pressure reaper is a copy of the 3b Python block with /Users/jay rewritten to the fake home.
# macOS only (birth time, lsof -d cwd, F_GETPATH): elsewhere it prints SKIP and exits 0.

if [ "${BASH:-}" != /bin/bash ] && [ -x /bin/bash ]; then exec /bin/bash "$0" "$@"; fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
JANITOR="${ROOT}/disk-janitor.sh"
CLEANUP="${ROOT}/mac-auto-cleanup.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$JANITOR" ] || fail "missing $JANITOR"
[ -f "$CLEANUP" ] || fail "missing $CLEANUP"
if [ "$(uname -s)" != Darwin ]; then echo "SKIP not macOS (birth time, lsof -d cwd, F_GETPATH)"; exit 0; fi
[ "${BASH_VERSINFO[0]}" = 3 ] || echo "note: /bin/bash is ${BASH_VERSION}, not 3.2" >&2
/bin/bash -n "$JANITOR" || fail "bash -n $JANITOR"
/bin/bash -n "$CLEANUP" || fail "bash -n $CLEANUP"

T=$(mktemp -d "${TMPDIR:-/tmp}/janitor-lanes.XXXXXX") || fail "mktemp"
T=$(cd "$T" && /bin/pwd -P)
case "$T" in /Users/*/apps/*|/Users/*/Code/*|"$HOME"|"$HOME"/.claude-disk-janitor*) fail "refusing to use $T as the fake root" ;; esac
PIDS=""
cleanup() {
  [ -n "$PIDS" ] && { kill $PIDS; wait $PIDS; } 2>/dev/null
  chmod -R u+rwx "$T" 2>/dev/null
  rm -rf "$T"
}
trap cleanup EXIT

FH="$T/home"; BIN="$T/bin"
mkdir -p "$FH/Code" "$FH/apps/lanes" "$BIN" "$T/afc/fleet_lanes"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 GIT_ALLOW_PROTOCOL=file
GA=(-c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false -c init.defaultBranch=main -c core.hooksPath=/dev/null)
OLD=202609010000   # well over 7 days before any run of this test

# ---- shims ------------------------------------------------------------------------------------------------------
cat > "$BIN/gh" <<EOF
#!/usr/bin/python3
import json, os, sys
T = "$T"
args = sys.argv[1:]
open(T + "/gh.calls", "a").write(" ".join(args) + "\n")
if os.path.exists(T + "/gh.rc"):
    sys.exit(int(open(T + "/gh.rc").read()))
prs = json.load(open(T + "/gh.json")) if os.path.exists(T + "/gh.json") else []
head = args[args.index("--head") + 1] if "--head" in args else None
for p in prs:
    if p.get("headRefName") == head and p.get("state") == "MERGED":
        print(p["headRefOid"])
EOF
# lsof: mode file picks ok (fixed listing + $T/lsof.cwds), real, killed (partial then SIGKILL), stderr (error, rc 1)
cat > "$BIN/lsof" <<EOF
#!/bin/bash
mode=\$(cat "$T/lsof.mode" 2>/dev/null || echo ok)
echo "\$*" >> "$T/lsof.calls"
case "\$mode" in
  real) exec /usr/sbin/lsof "\$@" ;;
  stderr) echo "lsof: can't allocate memory for process table" >&2; exit 1 ;;
  killed) printf 'p1\nfcwd\nn/\n'; kill -9 \$\$ ;;
esac
printf 'p1\nfcwd\nn/\np77\nfcwd\nn/Users/nobody\n'
while IFS= read -r c; do [ -n "\$c" ] && printf 'p4242\nfcwd\nn%s\n' "\$c"; done < "$T/lsof.cwds" 2>/dev/null
exit 0
EOF
# find: passes through, except that a dep-reap activity scan (-mmin) sleeps while $T/find.hang exists
cat > "$BIN/find" <<EOF
#!/bin/bash
case " \$* " in *" -mmin "*) [ -f "$T/find.hang" ] && sleep 30 ;; esac
exec /usr/bin/find "\$@"
EOF
# git: passes through, except that `ls-files` exits 128 (as on a corrupt index) while $T/git.lsfail exists
REAL_GIT=$(command -v git) || fail "git not found"
cat > "$BIN/git" <<EOF
#!/bin/bash
case " \$* " in *" ls-files "*) [ -f "$T/git.lsfail" ] && { echo "fatal: index file corrupt" >&2; exit 128; } ;; esac
exec "$REAL_GIT" "\$@"
EOF
# df: fails (stderr only, rc 1) while $T/df.fail exists
cat > "$BIN/df" <<EOF
#!/bin/bash
[ -f "$T/df.fail" ] && { echo "df: /System/Volumes/Data: Operation timed out" >&2; exit 1; }
exec /bin/df "\$@"
EOF
chmod +x "$BIN"/*
# Fake lane doctor: python3 -B -m fleet_lanes.doctor ... --write PATH.  Mode file: copy $T/doctor-next.json, fail, garbage.
: > "$T/afc/fleet_lanes/__init__.py"
cat > "$T/afc/fleet_lanes/doctor.py" <<EOF
import os, shutil, sys
T = "$T"
open(T + "/doctor.calls", "a").write(" ".join(sys.argv[1:]) + "\n")
mode = open(T + "/doctor.mode").read().strip() if os.path.exists(T + "/doctor.mode") else "fail"
out = sys.argv[sys.argv.index("--write") + 1]
if mode == "copy":
    shutil.copy(T + "/doctor-next.json", out)
elif mode == "garbage":
    open(out, "w").write("{not json")
else:
    sys.exit(1)
EOF

# ---- fixture repo: an integration tree with an origin that is a local bare repo (never the network) ---------------
git "${GA[@]}" init -q --bare "$T/origin.git"
git "${GA[@]}" clone -q "$T/origin.git" "$FH/Code/BotFleet" 2>/dev/null
(
  cd "$FH/Code/BotFleet" || exit 1
  echo hi > f.txt
  printf 'node_modules/\n.janitor-keep\n.claude/worktrees/\nlocal-state.sqlite\n' > .gitignore
  mkdir -p app/vendor/lib/node_modules/pkg && echo 'module.exports = 1' > app/vendor/lib/node_modules/pkg/index.js
  git add . && git add -f app/vendor/lib/node_modules/pkg/index.js
  git "${GA[@]}" commit -qm init && git branch -M main && git push -q origin main 2>/dev/null
  git fetch -q origin && git remote set-head origin main >/dev/null 2>&1
  mkdir -p node_modules/dep && echo dep > node_modules/dep/x.js
) || fail "fixture repo"
# Its files are old, so only KEEP_RE (not the 4-hour activity check) stands between it and the dep-reaps.
/usr/bin/find "$FH/Code/BotFleet" -path "$FH/Code/BotFleet/.git" -prune -o -type f -exec touch -h -t "$OLD" {} +
mkdir -p "$FH/Code/fleet-ops" && git "${GA[@]}" init -q "$FH/Code/fleet-ops"   # registry says Fleet-OPS, disk says fleet-ops
cat > "$T/fleet-apps.json" <<EOF
{"codeRoot": "$FH/Code", "apps": [{"repo": "BotFleet", "codeDir": "BotFleet"}, {"repo": "Fleet-OPS", "codeDir": "Fleet-OPS"}]}
EOF
integ_hash() { ( cd "$FH/Code/BotFleet" && /usr/bin/find . -path ./.git -prune -o -print -type f -exec shasum {} + | sort | shasum ); }
INTEG_BEFORE=$(integ_hash)

LANES="$FH/apps/lanes"
# mk_lane PATH BRANCH -- a linked worktree of BotFleet at origin/main, with an untracked node_modules
mk_lane() {
  mkdir -p "$(dirname "$1")"
  git -C "$FH/Code/BotFleet" worktree add -q -b "$2" "$1" origin/main 2>/dev/null || fail "worktree add $1"
  mkdir -p "$1/node_modules/dep" && echo dep > "$1/node_modules/dep/x.js"
}
commit_in() { ( cd "$1" && echo "$2-$RANDOM" > "$2" && git add "$2" && git "${GA[@]}" commit -qm "c $2" ) || fail "commit in $1"; }
# age_files PATH -- every file old (mtime only; the directories keep their real birth time)
age_files() { /usr/bin/find "$1" -type f -exec touch -h -t "$OLD" {} + 2>/dev/null; }
# age_birth PATH -- the checkout dir and its git admin dir BORN old (on APFS an older mtime moves the birth back)
age_birth() { local gd; gd=$(git -C "$1" rev-parse --absolute-git-dir); touch -t "$OLD" "$1" "$gd"; }
age() { age_files "$1"; age_birth "$1"; }
gone() { [ ! -e "$1" ]; }
# drop_lanes PREFIX -- force-remove every registered worktree at or under PREFIX (test fixture cleanup, fake repo only)
drop_lanes() {
  local w
  git -C "$FH/Code/BotFleet" worktree list --porcelain | awk '/^worktree /{sub(/^worktree /,""); print}' | sort -r |
    while IFS= read -r w; do
      case "$w" in "$1"|"$1"/*) git -C "$FH/Code/BotFleet" worktree remove --force --force "$w" >/dev/null 2>&1 ;; esac
    done
  git -C "$FH/Code/BotFleet" worktree prune
}
present() { [ -e "$1" ]; }

# write_report OUT [lane:override,...] -- a schema-2 report (as the real doctor writes it) approving each lane at its
# current HEAD.  Overrides: path=<lane>|key=<json> applies to that lane's entry; top:key=<json> to the report.
write_report() {
  local out="$1"; shift
  python3 - "$out" "$@" <<'PY'
import datetime, json, os, subprocess, sys
out, specs = sys.argv[1], sys.argv[2:]
now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
r = {"schema": 2, "generated_at": now, "lsof": "ok", "gh": "ok", "gh_repos": {"Test/BotFleet": "ok"},
     "fresh_days": 7, "cleaner_min_days": 7, "checkouts": [], "cleaner_candidates": []}
entries = {}
for spec in specs:
    if spec.startswith("top:"):
        k, v = spec[4:].split("=", 1); r[k] = json.loads(v); continue
    lane, _, kv = spec.partition("|")
    if lane not in entries:
        sha = subprocess.run(["git", "-C", lane, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        e = {"path": lane, "realpath": os.path.realpath(lane), "kind": "LINKED-WORKTREE", "registered": True,
             "owner_repo": "Test/BotFleet", "location_class": "LANE_NESTED", "safety": "SAFE-TO-REMOVE",
             "pr_state": "MERGED", "head_sha": sha, "janitor_keep": False, "active": False, "cwd_procs": [],
             "read_errors": [], "tool_cache": False, "lane_age_days": 30.0, "idle_days": 30.0}
        entries[lane] = e; r["checkouts"].append(e); r["cleaner_candidates"].append(e["realpath"])
    if kv:
        k, v = kv.split("=", 1); entries[lane][k] = json.loads(v)
r["cleaner_candidates"] = sorted(r["cleaner_candidates"])
json.dump(r, open(out, "w"))
PY
}

# ---- source the janitor in library mode with the fake home --------------------------------------------------------
EXT="$T/ext/Lanes"; mkdir -p "$EXT"      # the fake external lanes disk (FLEET_LANES_EXTERNAL_ROOT); lanes/Ext is a symlink onto it
export HOME="$FH" LANES_ROOT="$LANES" FLEET_APPS_JSON="$T/fleet-apps.json" AFC_SCRIPTS="$T/afc" \
  STALE_DAYS=7 REAP_WORKTREES=1 WT_REAP_DRYRUN=0 LANE_DOCTOR_RETRY_MIN=360 FLEET_LANES_EXTERNAL_ROOT="$EXT"
JANITOR_LIB_ONLY=1
# shellcheck source=disk-janitor.sh
source "$JANITOR"
export PATH="$BIN:$PATH"   # the janitor re-exports PATH with /opt/homebrew/bin first: shims go in front again
for v in DIR LOCK LOG STATE FLEET_APPS_CACHE LANE_REPORT LANE_DOCTOR_FAIL LANES_ROOT LANES_EXTERNAL_ROOT AFC_SCRIPTS FLEET_APPS_JSON; do
  case "${!v}" in "$T"/*) ;; *) fail "$v=${!v} is outside the fake root $T" ;; esac
done
[ "$(command -v lsof)" = "$BIN/lsof" ] && [ "$(command -v gh)" = "$BIN/gh" ] || fail "shims are not first on PATH"
mkdir -p "$LOCK"
REPOS=("$FH/Code/BotFleet" "$FH/Code/fleet-ops")
janitor_extend_repos
[ -s "$FLEET_APPS_CACHE" ] || fail "the registry cache must be written under the fake DIR"
for r in "${REPOS[@]}"; do case "$r" in "$T"/*) ;; *) fail "REPOS holds $r, outside the fake root" ;; esac; done
[ "${#REPOS[@]}" = 2 ] || fail "registry Fleet-OPS and disk fleet-ops must de-duplicate to one repo: ${REPOS[*]}"
janitor_protect_primaries
now="test"; free=100; FREE_KNOWN=1; lean=0; IDLE_HRS=4

# ---- KEEP_RE: every repo root, either letter case; lanes and in-repo scratch are not protected --------------------
for p in "$FH/Code/BotFleet" "$FH/Code/fleet-ops" "$FH/Code/Fleet-OPS" "$FH/Code/FLEET-OPS" "$FH/code/botfleet" \
         "$(git -C "$FH/Code/BotFleet" worktree list --porcelain | awk '/^worktree /{sub(/^worktree /,""); print; exit}')"; do
  janitor_keep_match "$p" || fail "KEEP_RE must protect $p"
done
for p in /Users/jay/apps/botfleet-claude /Users/jay/apps/agent-sync-push /Users/jay/apps/botfleet-server; do   # regex only, never cd there
  printf '%s\n' "$p" | grep -qiE "$KEEP_RE" || fail "KEEP_RE must still protect the standing lane $p"
done
for p in "$LANES/BotFleet/claude-x" "$FH/Code/BotFleet/.claude/worktrees/abc" "$FH/Code/BotFleet-copy" "$FH/apps/botfleet-claude-fix"; do
  janitor_keep_match "$p" && fail "KEEP_RE must not protect $p"
done

# ---- unit checks: nested-lane detection, inner checkout, git failure, argv, free probe ----------------------------
mkdir -p "$FH/apps/lanes/BotFleet/case-probe"
janitor_is_nested_lane "$FH/Apps/LANES/botfleet/case-probe" || fail "a case variant of LANES_ROOT is still a nested lane"
ln -s "$LANES" "$T/lanes-link"
janitor_is_nested_lane "$T/lanes-link/BotFleet/case-probe" || fail "a symlinked spelling of LANES_ROOT is still a nested lane"
janitor_is_nested_lane "$FH/apps/botfleet-claude-x" && fail "a flat lane is not nested"
# external lanes (owner 2026-10-10): git lists a lane behind a lanes/<Repo> symlink at its REAL path on the external disk,
# which is not under LANES_ROOT; it is a nested lane all the same, and so is a case variant of it
janitor_is_nested_lane "$EXT/BotFleet/claude-x" || fail "a lane at its real path on the external lanes disk is a nested lane"
janitor_is_nested_lane "$T/EXT/lanes/BotFleet/claude-x" || fail "a case variant of the external lanes root is still a nested lane"
janitor_is_nested_lane "$T/ext/Lanes-extra/BotFleet/claude-x" && fail "a sibling that only shares the prefix is not an external lane"
janitor_is_nested_lane "$T/ext/Other/claude-x" && fail "another folder on the external disk is not a lane"
( LANES_EXTERNAL_ROOT=""; janitor_is_nested_lane "$EXT/BotFleet/claude-x" ) && fail "an empty FLEET_LANES_EXTERNAL_ROOT switches the external root off"
# layout v2 (owner 2026-10-09): the janitor tests only "under the lanes root", so every v2 shape is a nested lane and
# stays doctor-gated; a legacy prefix folder, a review checkout, a Claude desktop folder and a Codex folder all count
for shape in "BotFleet/review-pr-482" "BotFleet/review-pr-482-codex" "BotFleet/active-engines-display-e380b8" \
             "_codex/fix-thing/BotFleet" "Congress.Trade/claude-x" "AI-Fleet-Coordinator/claude-x" \
             "botfleet/claude-x" "_managed/fleet/agent-sync-runtime" "_review/botfleet/pr-1"; do
  janitor_is_nested_lane "$LANES/$shape" || fail "a v2 or legacy lane shape under the lanes root must count as nested: $shape"
done

# pgrep: a lane name with regex characters (pgrep exits 2 on the raw path) must still be found in argv
RX="$LANES/BotFleet/claude-c++-fix(1)"; mkdir -p "$RX"
(cd / && exec python3 -c 'import time; time.sleep(300)' "$RX/server.js") </dev/null >/dev/null 2>&1 &
PIDS="$PIDS $!"; sleep 1
janitor_argv_busy "$RX" || fail "argv match must survive regex characters in the path"
janitor_argv_busy "$LANES/BotFleet/claude-nobody-runs-here" && fail "argv check must report idle when nothing matches"

# freek: a failed df with no prior reading is UNKNOWN (never 0 G, which opened lowfree, pressure and CRIT at once)
: > "$T/df.fail"; rm -f "$STATE"
( free=""; JANITOR_FREE_PROBED=""; janitor_read_free; [ "$FREE_KNOWN" = 0 ] && [ -z "$free" ] ) || fail "df failure + no state must be FREE_KNOWN=0"
grep -q 'FREE-UNKNOWN' "$LOG" || fail "FREE-UNKNOWN must be logged"
printf 'free=0\nts=x\n' > "$STATE"
( janitor_read_free; [ "$FREE_KNOWN" = 0 ] ) || fail "a poisoned free=0 state must not read as 0 G"
printf 'free=24\nts=x\n' > "$STATE"
( janitor_read_free; [ "$FREE_KNOWN" = 1 ] && [ "$free" = 24 ] ) || fail "df failure falls back to the last real reading"
rm -f "$T/df.fail" "$STATE"

# ---- RETIRE: one world, one fresh report; only the old merged lane with no keep marker and no cwd retires ----------
L_FRESH="$LANES/BotFleet/claude-fresh";       mk_lane "$L_FRESH" claude/fresh                                  # born now, 0 ahead
L_MTIME="$LANES/BotFleet/claude-mtime-only";  mk_lane "$L_MTIME" claude/mtime; commit_in "$L_MTIME" a; age_files "$L_MTIME"
L_MERGED="$LANES/BotFleet/claude-merged";     mk_lane "$L_MERGED" claude/merged; commit_in "$L_MERGED" a
echo db > "$L_MERGED/local-state.sqlite"; age "$L_MERGED"
L_KEEP="$LANES/BotFleet/claude-keep";         mk_lane "$L_KEEP" claude/keep; commit_in "$L_KEEP" a; touch "$L_KEEP/.janitor-keep"; age "$L_KEEP"
L_CWD="$LANES/BotFleet/claude-cwd";           mk_lane "$L_CWD" claude/cwd; commit_in "$L_CWD" a; mkdir -p "$L_CWD/src"; age "$L_CWD"
L_UNLISTED="$LANES/BotFleet/claude-unlisted"; mk_lane "$L_UNLISTED" claude/unlisted; age "$L_UNLISTED"
L_INNER="$LANES/BotFleet/claude-holds-inner"; mk_lane "$L_INNER" claude/holds-inner; age "$L_INNER"
mk_lane "$L_INNER/.claude/worktrees/session-1a2b3c" claude/session; echo wip >> "$L_INNER/.claude/worktrees/session-1a2b3c/f.txt"
age_files "$L_INNER"
[ -z "$(git -C "$L_KEEP" status --porcelain)" ] || fail "fixture: the keep marker must be ignored, so only the marker gate keeps that lane"
[ -z "$(git -C "$L_INNER" status --porcelain)" ] || fail "fixture: the inner checkout must be ignored by the outer lane"
echo ok > "$T/lsof.mode"; echo "$L_CWD/src" > "$T/lsof.cwds"
# Even a report that lists every lane (a doctor would not list the fresh ones) retires only the merged one.
write_report "$LANE_REPORT" "$L_FRESH" "$L_MTIME" "$L_MERGED" "$L_KEEP" "$L_CWD" "$L_INNER"
janitor_retire_worktrees
gone "$L_MERGED" || fail "the old lane the doctor approves at its HEAD must retire (log: $(tail -3 "$LOG"))"
[ "$n_reap" = 1 ] || fail "exactly one lane retires, n_reap=$n_reap"
for l in "$L_FRESH" "$L_MTIME" "$L_KEEP" "$L_CWD" "$L_UNLISTED" "$L_INNER" "$L_INNER/.claude/worktrees/session-1a2b3c" "$FH/Code/BotFleet"; do
  present "$l" || fail "retire must keep $l"
done
git -C "$FH/Code/BotFleet" rev-parse -q --verify refs/heads/claude/merged >/dev/null || fail "retiring keeps the branch"

# A real process with its cwd in the lane, seen by the REAL lsof, keeps it (the shim is a pass-through here).
L_REALCWD="$LANES/BotFleet/claude-realcwd"; mk_lane "$L_REALCWD" claude/realcwd; age "$L_REALCWD"
(cd "$L_REALCWD" && exec sleep 300) </dev/null >/dev/null 2>&1 & PIDS="$PIDS $!"; sleep 1
echo real > "$T/lsof.mode"; write_report "$LANE_REPORT" "$L_REALCWD"
janitor_retire_worktrees
present "$L_REALCWD" || fail "a lane with a live process cwd (real lsof) must be kept"

# ---- RETIRE: report and lsof failures, each a one-field change from a report that approves the lane ---------------
OLDTIME=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(hours=4)).isoformat(timespec="seconds"))')
FUTTIME=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)+d.timedelta(hours=1)).isoformat(timespec="seconds"))')
N=0
# retire_case EXPECT NAME SETUP...: a new old, merged, approved lane; SETUP is eval'd to break exactly one thing.
retire_case() {
  local expect="$1" name="$2" setup="$3" L
  N=$((N + 1)); L="$LANES/BotFleet/claude-case$N"
  mk_lane "$L" "claude/case$N"; commit_in "$L" a; age "$L"
  echo ok > "$T/lsof.mode"; : > "$T/lsof.cwds"; echo copy > "$T/doctor.mode"; rm -f "$LANE_DOCTOR_FAIL"
  write_report "$LANE_REPORT" "$L"
  LANE_REPORT_REFRESHED=""
  eval "$setup"
  janitor_retire_worktrees
  case "$expect" in
    retire) gone "$L" || fail "[$name] must retire (log: $(tail -2 "$LOG"))" ;;
    keep)   present "$L" || fail "[$name] must keep the lane" ;;
  esac
  drop_lanes "$L"
}
retire_case retire control ""
retire_case keep   "report missing, doctor fails" 'rm -f "$LANE_REPORT"; echo fail > "$T/doctor.mode"'
[ -f "$LANE_DOCTOR_FAIL" ] || fail "a failed doctor refresh must leave the retry stamp"
retire_case retire "report missing, doctor refresh writes an approving one" 'cp "$LANE_REPORT" "$T/doctor-next.json"; rm -f "$LANE_REPORT"'
retire_case keep   "report missing, doctor failed recently (no retry)" 'cp "$LANE_REPORT" "$T/doctor-next.json"; rm -f "$LANE_REPORT"; : > "$LANE_DOCTOR_FAIL"'
retire_case keep   "report malformed" 'echo "{not json" > "$LANE_REPORT"; echo fail > "$T/doctor.mode"'
retire_case keep   "doctor refresh writes garbage" 'touch -t "$OLD" "$LANE_REPORT"; echo garbage > "$T/doctor.mode"'
retire_case keep   "report schema 1" 'write_report "$LANE_REPORT" "$L" "top:schema=1"'
retire_case keep   "report stale by mtime" 'touch -t "$OLD" "$LANE_REPORT"; echo fail > "$T/doctor.mode"'
retire_case keep   "report mtime in the future" 'touch -t 203001010000 "$LANE_REPORT"; echo fail > "$T/doctor.mode"'
retire_case keep   "generated_at 4 h old (copied report, fresh mtime)" 'write_report "$LANE_REPORT" "$L" "top:generated_at=\"$OLDTIME\""'
retire_case keep   "generated_at in the future" 'write_report "$LANE_REPORT" "$L" "top:generated_at=\"$FUTTIME\""'
retire_case keep   "lsof unavailable in the report" 'write_report "$LANE_REPORT" "$L" "top:lsof=\"unavailable\""'
retire_case keep   "gh failed for the repo" 'write_report "$LANE_REPORT" "$L" "top:gh_repos={\"Test/BotFleet\":\"failed\"}"'
retire_case keep   "listed in checkouts but not in cleaner_candidates" 'write_report "$LANE_REPORT" "$L" "top:cleaner_candidates=[]"'
retire_case keep   "cwd_procs null (lsof failed at report time)" 'write_report "$LANE_REPORT" "$L" "$L|cwd_procs=null"'
retire_case keep   "PR state NONE" 'write_report "$LANE_REPORT" "$L" "$L|pr_state=\"NONE\""'
retire_case keep   "HEAD moved after the report (amend, no remote ref)" '( cd "$L" && git "${GA[@]}" commit -q --amend -m later ); age "$L"'
retire_case keep   "newline path in another entry" 'write_report "$LANE_REPORT" "$L" "$L|safety=\"NEEDS-REVIEW\"" "top:cleaner_candidates=[\"/elsewhere/odd\\n$L\"]"'
retire_case keep   "lsof killed part-way (rc 137)" 'echo killed > "$T/lsof.mode"'
retire_case keep   "lsof printed only an error (rc 1)" 'echo stderr > "$T/lsof.mode"'
retire_case keep   "process cwd in the lane, path in on-disk case" 'echo "$L/src" > "$T/lsof.cwds"'
retire_case keep   ".janitor-keep added after the report" 'touch "$L/.janitor-keep"'
grep -q 'CWD-SNAPSHOT-FAILED rc=137' "$LOG" || fail "a killed lsof must be logged as CWD-SNAPSHOT-FAILED"

# Letter case: git records a worktree in the case it was typed, lsof prints the on-disk case.  A lane typed as
# apps/LANES/botfleet/... is still a nested lane (no report = kept, even 0 commits ahead), and a process whose cwd lsof
# prints in the on-disk spelling still keeps it.
TC_TYPED="$FH/apps/LANES/botfleet/claude-typedcase"; TC_DISK="$LANES/BotFleet/claude-typedcase"
mk_lane "$TC_TYPED" claude/typedcase; age "$TC_DISK"
git -C "$FH/Code/BotFleet" worktree list --porcelain | grep -q "^worktree .*/LANES/botfleet/claude-typedcase$" \
  || fail "fixture: git must record the typed case"
echo ok > "$T/lsof.mode"; : > "$T/lsof.cwds"; write_report "$LANE_REPORT"   # a valid report that lists nothing
janitor_retire_worktrees
present "$TC_DISK" || fail "a 0-ahead lane typed in another letter case must stay doctor-gated (kept with no approval)"
write_report "$LANE_REPORT" "$TC_TYPED"; echo "$TC_DISK/src" > "$T/lsof.cwds"
janitor_retire_worktrees
present "$TC_DISK" || fail "a process cwd printed in the on-disk case must keep a lane typed in another case"
: > "$T/lsof.cwds"
janitor_retire_worktrees
gone "$TC_DISK" || fail "control: the approved typed-case lane with no cwd retires"

drop_lanes "$LANES/BotFleet"

# ---- external lanes: lanes/Ext is a symlink onto the external disk; git lists its lanes at the REAL path ---------
# Without the external root in janitor_is_nested_lane such a lane is not "nested", skips the doctor gate and retires on
# the legacy tests (here: 0 commits ahead of origin/main).  It must stay doctor-gated like any lane under the lanes root.
mkdir -p "$EXT/Ext" && ln -s "$EXT/Ext" "$LANES/Ext"
L_XA="$LANES/Ext/claude-listed"; mk_lane "$L_XA" claude/x-listed; commit_in "$L_XA" a; age "$L_XA"
L_XB="$LANES/Ext/claude-unlisted"; mk_lane "$L_XB" claude/x-unlisted; age "$L_XB"
L_XC="$LANES/Ext/claude-cwd"; mk_lane "$L_XC" claude/x-cwd; commit_in "$L_XC" a; mkdir -p "$L_XC/src"; age "$L_XC"
git -C "$FH/Code/BotFleet" worktree list --porcelain | grep -q "^worktree $EXT/Ext/claude-unlisted$" \
  || fail "fixture: git must record the real path on the external disk"
echo ok > "$T/lsof.mode"; echo "$EXT/Ext/claude-cwd/src" > "$T/lsof.cwds"        # lsof prints the real path, too
write_report "$LANE_REPORT" "$L_XA" "$L_XC"                                       # the doctor lists two of the three
janitor_retire_worktrees
present "$L_XB" || fail "an external lane the doctor did not list must be kept, however merged it looks (it is nested)"
present "$L_XC" || fail "a process cwd at the real external path must keep an approved external lane"
gone "$L_XA" || fail "an approved external lane with no cwd must retire (log: $(tail -2 "$LOG"))"
: > "$T/lsof.cwds"
janitor_dep_reap_worktrees >/dev/null 2>&1; present "$L_XB" || fail "dep-reap must not remove an external lane"

# git worktree prune deletes the registry entry of every worktree whose folder is missing: an unmounted external disk
# must never reach it (janitor_prune_safe), while a mounted one, or no external lanes at all, prunes as before.
janitor_prune_safe "$FH/Code/BotFleet" || fail "mounted disk: prune is safe"
ln -s "$T/nowhere-at-all" "$LANES/Unrelated"        # a dangling link that has nothing to do with the external disk
janitor_prune_safe "$FH/Code/BotFleet" || fail "a dangling link that does not point at the external disk must not stop prune"
rm "$LANES/Unrelated"
( LANES_EXTERNAL_ROOT=""; mv "$T/ext/Lanes" "$T/ext/Lanes.off"; janitor_prune_safe "$FH/Code/BotFleet"; rc=$?; mv "$T/ext/Lanes.off" "$T/ext/Lanes"; exit $rc ) \
  || fail "with the external root off prune is always safe"
mv "$T/ext/Lanes" "$T/ext/Lanes.off"
janitor_prune_safe "$FH/Code/BotFleet" && fail "a lanes/Ext symlink that leads nowhere (disk not mounted): prune must be skipped"
rm "$LANES/Ext"
janitor_prune_safe "$FH/Code/BotFleet" && fail "disk not mounted and the repo still lists lanes under it: prune must be skipped"
janitor_prune_safe "$FH/Code/fleet-ops" || fail "disk not mounted but this repo lists nothing under it: prune is safe"
mv "$T/ext/Lanes.off" "$T/ext/Lanes"; ln -s "$EXT/Ext" "$LANES/Ext"
janitor_prune_safe "$FH/Code/BotFleet" || fail "remounted: prune is safe again"
drop_lanes "$EXT"; rm -f "$LANES/Ext"
mv "$T/ext/Lanes" "$T/ext/Lanes.off"
janitor_prune_safe "$FH/Code/BotFleet" || fail "no external lanes registered and no link: an absent disk costs nothing, prune"
mv "$T/ext/Lanes.off" "$T/ext/Lanes"

# A lane moved to the disk keeps its OLD registered path (its lanes spelling), and a mover may make the link relative.
# Neither names the external root, so the guard has to look at the listed worktree's top folder instead.
mkdir -p "$LANES/Moved"; mk_lane "$LANES/Moved/claude-moved" claude/moved-to-disk
mv "$LANES/Moved" "$EXT/Moved" && ln -s "../../../ext/Lanes/Moved" "$LANES/Moved"
[ -d "$LANES/Moved/claude-moved" ] || fail "fixture: the relative link must work while the disk is there"
git -C "$FH/Code/BotFleet" worktree list --porcelain | grep -q "^worktree $LANES/Moved/claude-moved$" || fail "fixture: git must still list the lanes spelling"
janitor_prune_safe "$FH/Code/BotFleet" || fail "relative link, disk mounted: prune is safe"
mv "$T/ext/Lanes" "$T/ext/Lanes.off"
janitor_prune_safe "$FH/Code/BotFleet" && fail "a relative dangling link over a lane registered at its lanes path: prune must be skipped"
mv "$T/ext/Lanes.off" "$T/ext/Lanes"
janitor_prune_safe "$FH/Code/BotFleet" || fail "relative link, remounted: prune is safe again"
drop_lanes "$LANES/Moved"; rm -f "$LANES/Moved"; rm -rf "$EXT/Moved"

# ---- janitor_pr_merged: by head sha, never by a reused branch name -----------------------------------------------
P="$T/prrepo"; git "${GA[@]}" init -q "$P"; ( cd "$P" && echo a > a && git add a && git "${GA[@]}" commit -qm one ) || fail "pr repo"
git -C "$P" remote add origin git@github.com:Test/PrRepo.git
S1=$(git -C "$P" rev-parse HEAD)
( cd "$P" && echo b > b && git add b && git "${GA[@]}" commit -qm two ) || fail "pr repo commit"
S2=$(git -C "$P" rev-parse HEAD)
OTHER=$(git "${GA[@]}" -C "$P" commit-tree -m unrelated "$(git -C "$P" rev-parse 'HEAD^{tree}')")   # same tree, unrelated history
set_pr() { printf '[{"headRefName":"claude/x","state":"MERGED","headRefOid":"%s"}]\n' "$1" > "$T/gh.json"; }
set_pr "$S2";    janitor_pr_merged "$P" refs/heads/claude/x "$S2"    || fail "PR head == HEAD must read merged"
set_pr "$S2";    janitor_pr_merged "$P" refs/heads/claude/x "$S1"    || fail "HEAD an ancestor of the merged PR head must read merged"
set_pr "$S1";    janitor_pr_merged "$P" refs/heads/claude/x "$S2"    && fail "commits beyond the merged PR head must not read merged"
set_pr "$OTHER"; janitor_pr_merged "$P" refs/heads/claude/x "$S2"    && fail "a reused branch name (unrelated merged head) must not read merged"
set_pr "$S2";    janitor_pr_merged "$P" refs/heads/claude/y "$S2"    && fail "another branch's merged PR must not count"
set_pr "$S2";    janitor_pr_merged "$P" refs/heads/claude/x ""       && fail "no sha, no verdict"
set_pr "$S2";    janitor_pr_merged "$P" "" "$S2"                     && fail "a detached HEAD has no branch to look up"
echo 1 > "$T/gh.rc"; janitor_pr_merged "$P" refs/heads/claude/x "$S2" && fail "a failed gh must not read merged"
rm -f "$T/gh.rc"

# ---- DEP-REAP (pressure phase) -------------------------------------------------------------------------------------
D_OLD="$LANES/congress/claude-dep-old";      mk_lane "$D_OLD" claude/dep-old; age "$D_OLD"
D_YOUNG="$LANES/congress/claude-dep-young";  mk_lane "$D_YOUNG" claude/dep-young; age_files "$D_YOUNG"
D_KEEP="$LANES/congress/claude-dep-keep";    mk_lane "$D_KEEP" claude/dep-keep; touch "$D_KEEP/.janitor-keep"; age "$D_KEEP"
D_CWD="$LANES/congress/claude-dep-cwd";      mk_lane "$D_CWD" claude/dep-cwd; mkdir -p "$D_CWD/src"; age "$D_CWD"
D_INNER="$LANES/congress/claude-dep-parent"; mk_lane "$D_INNER" claude/dep-parent; age "$D_INNER"
D_NEST="$D_INNER/.claude/worktrees/session-9f9f9f"; mk_lane "$D_NEST" claude/dep-session
echo 'module.exports = 2 // uncommitted' > "$D_NEST/app/vendor/lib/node_modules/pkg/index.js"; touch "$D_NEST/.janitor-keep"
age_files "$D_INNER"
D_FLAT="$FH/apps/congress-claude-dep-flat";    mk_lane "$D_FLAT" claude/dep-flat; age "$D_FLAT"      # flat legacy lane
D_FLATCWD="$FH/apps/congress-claude-dep-flatcwd"; mk_lane "$D_FLATCWD" claude/dep-flatcwd; age "$D_FLATCWD"
D_GITFAIL="$LANES/congress/claude-dep-badindex"; mk_lane "$D_GITFAIL" claude/dep-badindex
echo 'module.exports = 3 // uncommitted' > "$D_GITFAIL/app/vendor/lib/node_modules/pkg/index.js"; age "$D_GITFAIL"
printf garbage > "$(git -C "$D_GITFAIL" rev-parse --absolute-git-dir)/index"
git -C "$D_GITFAIL" status --porcelain >/dev/null 2>&1 && fail "fixture: the corrupt index must make git status fail"
[ -n "$(wt_blocking_dirt "$D_GITFAIL")" ] || fail "a failed git status must read as dirt, not clean"
janitor_has_inner_checkout "$D_INNER" || fail "the session checkout inside the lane must be found"
janitor_has_inner_checkout "$D_OLD" && fail "a lane with no inner checkout must not report one"

# Forced activity-scan timeout and a failed lsof: everything keeps its deps, the positive control included.
echo ok > "$T/lsof.mode"; : > "$T/lsof.cwds"; : > "$T/find.hang"
( JANITOR_SCAN_SECS=2; janitor_dep_reap_worktrees ) >/dev/null 2>&1   # the real budget is 15 s; 2 s keeps the test short
rm -f "$T/find.hang"
grep -q 'WATCHDOG killed phase=wt-activity-scan' "$LOG" || fail "the activity scan must have been killed by its watchdog"
for l in "$D_OLD" "$D_YOUNG" "$D_KEEP" "$D_CWD" "$D_INNER" "$D_NEST" "$D_GITFAIL"; do
  present "$l/node_modules" || fail "a killed activity scan must keep $l/node_modules"
done
for m in killed stderr; do
  echo "$m" > "$T/lsof.mode"
  ( janitor_dep_reap_worktrees ) >/dev/null 2>&1
  present "$D_OLD/node_modules" && present "$D_FLAT/node_modules" && present "$D_FLATCWD/node_modules" \
    || fail "lsof=$m must keep every node_modules, flat lanes and the idle controls included"
done

# ls-files failing (rc 128) while git status still works: only rc 1 means "not tracked", so nothing is reaped.
: > "$T/git.lsfail"; echo ok > "$T/lsof.mode"; : > "$T/lsof.cwds"
( janitor_dep_reap_worktrees ) >/dev/null 2>&1
rm -f "$T/git.lsfail"
present "$D_OLD/node_modules" && present "$D_OLD/app/vendor/lib/node_modules/pkg/index.js" \
  || fail "a failed ls-files must keep node_modules and the tracked vendored tree"

# Normal pressure run: only the old, idle, unclaimed checkout loses its deps; nothing tracked is ever touched.
echo ok > "$T/lsof.mode"; printf '%s\n' "$D_CWD/src" "$D_FLATCWD" > "$T/lsof.cwds"
( janitor_dep_reap_worktrees ) >/dev/null 2>&1
gone "$D_FLAT/node_modules"       || fail "positive control: an old idle flat lane must lose node_modules"
present "$D_FLATCWD/node_modules" || fail "a process cwd keeps a flat lane's node_modules"
gone "$D_OLD/node_modules"      || fail "positive control: an old idle clean lane must lose node_modules"
gone "$D_INNER/node_modules"    || fail "the parent lane's own node_modules must be reaped"
present "$D_YOUNG/node_modules" || fail "a young nested lane keeps its node_modules"
present "$D_KEEP/node_modules"  || fail ".janitor-keep keeps node_modules"
present "$D_CWD/node_modules"   || fail "a process cwd keeps node_modules"
present "$D_NEST/node_modules"  || fail "a checkout nested in a lane keeps its own node_modules"
grep -q 'uncommitted' "$D_NEST/app/vendor/lib/node_modules/pkg/index.js" 2>/dev/null || fail "the nested checkout's tracked vendored edit must survive"
present "$D_GITFAIL/node_modules" || fail "a failed git status must keep node_modules"
grep -q 'uncommitted' "$D_GITFAIL/app/vendor/lib/node_modules/pkg/index.js" 2>/dev/null || fail "a tracked vendored edit must survive a failed git status"
present "$D_OLD/app/vendor/lib/node_modules/pkg/index.js" || fail "a tracked vendored node_modules is never reaped"
present "$FH/Code/BotFleet/node_modules" || fail "the integration tree's node_modules must never be reaped"

# ---- mac-auto-cleanup 3b (Python): the same guards ----------------------------------------------------------------
python3 - "$CLEANUP" "$T/reaper.py" "$FH" <<'PY' || fail "extract the 3b reaper"
import sys
s = open(sys.argv[1]).read()
k = s.index("# 3b."); a = s.index("python3 <<'PY'\n", k) + len("python3 <<'PY'\n"); b = s.index("\nPY\n", a)
body = s[a:b].replace("/Users/jay", sys.argv[3])
if "/Users/jay" in body.replace(sys.argv[3], ""):
    sys.exit(1)
open(sys.argv[2], "w").write(body + "\n")
PY
sed -i '' "s#$FH/Code/AI-Fleet-Coordinator/fleet-apps.json#$T/fleet-apps.json#" "$T/reaper.py"
grep -q "$T/fleet-apps.json" "$T/reaper.py" || fail "reaper copy must read the fake registry"
run_reaper() { HOME="$FH" python3 "$T/reaper.py" >/dev/null 2>&1; }
R_OLD="$LANES/hog/claude-r-old";     mk_lane "$R_OLD" claude/r-old; age "$R_OLD"
R_FORGED="$LANES/hog/claude-r-seed"; mk_lane "$R_FORGED" claude/r-seed; age_files "$R_FORGED"; touch -t "$OLD" "$R_FORGED"  # dir re-timed (rsync -a), admin dir young
R_CWD="$LANES/hog/claude-r-cwd";     mk_lane "$R_CWD" claude/r-cwd; age "$R_CWD"
R_FLAT="$FH/apps/hoghunter-claude-r-flat"; mk_lane "$R_FLAT" claude/r-flat; age "$R_FLAT"
ln -s "$FH/Code/BotFleet" "$FH/apps/botfleet-alias"                                 # a dir symlink into the integration tree
for m in killed stderr; do
  echo "$m" > "$T/lsof.mode"; run_reaper
  present "$R_OLD/node_modules" && present "$R_FLAT/node_modules" || fail "reaper: lsof=$m must keep every node_modules"
done
echo ok > "$T/lsof.mode"; echo "$R_CWD" > "$T/lsof.cwds"
run_reaper
gone "$R_OLD/node_modules"       || fail "reaper positive control: an old idle nested lane must lose node_modules"
gone "$R_FLAT/node_modules"      || fail "reaper positive control: an old idle flat lane must lose node_modules"
present "$R_FORGED/node_modules" || fail "reaper: a re-timed checkout dir with a young git admin dir is still young"
present "$R_CWD/node_modules"    || fail "reaper: a process cwd keeps node_modules"
present "$D_YOUNG/node_modules" && present "$D_KEEP/node_modules" && present "$D_GITFAIL/node_modules" \
  || fail "reaper: young, .janitor-keep and git-failure lanes keep node_modules"
grep -q 'uncommitted' "$D_GITFAIL/app/vendor/lib/node_modules/pkg/index.js" || fail "reaper: tracked vendored edit must survive"
present "$FH/Code/BotFleet/node_modules" || fail "reaper: a symlink in ~/apps must never reach the integration tree"

# ---- the integration tree is untouched -----------------------------------------------------------------------------
INTEG_AFTER=$(integ_hash)
[ "$INTEG_BEFORE" = "$INTEG_AFTER" ] || fail "the integration tree's files changed"
[ -z "$(git -C "$FH/Code/BotFleet" status --porcelain)" ] || fail "the integration tree must stay clean"

echo OK
