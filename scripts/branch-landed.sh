#!/bin/bash
# Squash-safe "did this branch already land on main?"
#
# All fleet repos squash-merge with delete_branch_on_merge, so a correctly
# landed branch has its commits absent from origin/main AND no remote branch
# left behind.  `git merge-base --is-ancestor HEAD origin/main` and "N commits
# ahead and not on remote" therefore flag every landed lane as abandoned
# (2026-09-05 false panic; ~110 of 146 worktrees on 2026-09-06).  Board 059f65b3.
#
# Usage:
#   branch-landed.sh [repo-or-worktree] [branch]
# Defaults: cwd, current branch.
#
# Prints one of MERGED / OPEN / CLOSED / NONE / UNKNOWN plus the PR URL when
# there is one.  Also prints the three-dot diffstat (origin/main...HEAD) so a
# human can see remaining unique work.  Exit 0 only for MERGED.
#
# A two-dot `git diff origin/main HEAD` on a stale lane is actively misleading.
set -euo pipefail

REPO="${1:-.}"
if [ -n "${2:-}" ]; then
  BRANCH="$2"
else
  BRANCH=$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null || true)
fi
BRANCH="${BRANCH#refs/heads/}"

if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "UNKNOWN  detached HEAD — pass a branch name" >&2
  exit 2
fi

url=$(git -C "$REPO" remote get-url origin 2>/dev/null || true)
url=${url%.git}
url=${url#git@github.com:}
url=${url#https://github.com/}
url=${url#ssh://git@github.com/}
case "$url" in
  */*) GH_REPO="$url" ;;
  *)
    echo "UNKNOWN  cannot parse origin remote as owner/repo" >&2
    exit 2
    ;;
esac

json=$(gh pr list --repo "$GH_REPO" --head "$BRANCH" --state all \
  --json number,state,mergedAt,url 2>/dev/null || true)

set +e
python3 - "$BRANCH" "$json" <<'PY'
import json, sys
branch = sys.argv[1]
raw = sys.argv[2] if len(sys.argv) > 2 else ""
prs = []
if raw.strip():
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        prs = []
if not prs:
    print(f"NONE  no PR for --head {branch}")
    sys.exit(3)
# Prefer a merged PR if several exist for the same head.
prs_sorted = sorted(prs, key=lambda p: (p.get("state") != "MERGED", -(p.get("number") or 0)))
p = prs_sorted[0]
state = (p.get("state") or "").upper()
url = p.get("url") or ""
num = p.get("number")
if state == "MERGED":
    print(f"MERGED  PR #{num}  {url}")
    sys.exit(0)
if state == "OPEN":
    print(f"OPEN  PR #{num}  {url}")
    sys.exit(1)
print(f"CLOSED  PR #{num}  {url}")
sys.exit(4)
PY
status=$?
set -e

base=$(git -C "$REPO" rev-parse --abbrev-ref origin/HEAD 2>/dev/null || echo origin/main)
echo "three-dot ${base}...HEAD (unique work vs merge-base; empty means no unique commits):"
git -C "$REPO" diff --stat "${base}...HEAD" 2>/dev/null || true
exit "$status"
