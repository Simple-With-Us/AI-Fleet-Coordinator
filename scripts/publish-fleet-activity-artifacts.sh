#!/usr/bin/env bash
# publish-fleet-activity-artifacts.sh — push generated digest + calendar trees
# to the unprotected data branch published/fleet-activity.
#
# main is ruleset-protected; direct pushes from fleet-activity-site.yml are
# refused (GH013).  Pages deploy is the live site; this branch is the
# raw.githubusercontent.com mirror and CI boundary snapshot.
#
# Usage (from repo root, after build-fleet-daily-digest.py and
# build-agent-calendar.py):
#   bash scripts/publish-fleet-activity-artifacts.sh
#
# Env:
#   FLEET_ACTIVITY_PUBLISH_BRANCH — override branch (default published/fleet-activity)
#   FLEET_ACTIVITY_SKIP_PUSH=1    — commit only, no push (for tests)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="${FLEET_ACTIVITY_PUBLISH_BRANCH:-published/fleet-activity}"
STAGING="${ROOT}/.fleet-activity-publish-staging"
WORKTREE="${ROOT}/.fleet-activity-publish-worktree"

for path in site calendar; do
  if [[ ! -d "${ROOT}/${path}" ]]; then
    echo "error: missing ${path}/ (run the builders first)" >&2
    exit 1
  fi
done

rm -rf "${STAGING}"
mkdir -p "${STAGING}"
cp -a "${ROOT}/site" "${ROOT}/calendar" "${STAGING}/"

git -C "${ROOT}" config user.name "github-actions[bot]"
git -C "${ROOT}" config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git -C "${ROOT}" fetch --no-tags origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" 2>/dev/null || true

git -C "${ROOT}" worktree remove --force "${WORKTREE}" 2>/dev/null || rm -rf "${WORKTREE}"

if git -C "${ROOT}" show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
  git -C "${ROOT}" worktree add -B "${BRANCH}" "${WORKTREE}" "origin/${BRANCH}"
else
  mkdir -p "${WORKTREE}"
  git -C "${ROOT}" worktree add --detach "${WORKTREE}" HEAD
  git -C "${WORKTREE}" checkout --orphan "${BRANCH}"
  git -C "${WORKTREE}" rm -rf . 2>/dev/null || true
fi

rm -rf "${WORKTREE}/site" "${WORKTREE}/calendar"
cp -a "${STAGING}/site" "${STAGING}/calendar" "${WORKTREE}/"
rm -rf "${STAGING}"

git -C "${WORKTREE}" add site calendar
if git -C "${WORKTREE}" diff --cached --quiet; then
  echo "No artifact changes on ${BRANCH}."
  git -C "${ROOT}" worktree remove --force "${WORKTREE}" 2>/dev/null || rm -rf "${WORKTREE}"
  exit 0
fi

DATE="$(date -u +%Y-%m-%dT%H:%MZ)"
git -C "${WORKTREE}" commit -m "chore(site): refresh fleet daily digest + calendars ${DATE} [skip ci]"

if [[ "${FLEET_ACTIVITY_SKIP_PUSH:-}" == "1" ]]; then
  echo "FLEET_ACTIVITY_SKIP_PUSH=1 — not pushing ${BRANCH}"
  git -C "${ROOT}" worktree remove --force "${WORKTREE}" 2>/dev/null || rm -rf "${WORKTREE}"
  exit 0
fi

git -C "${WORKTREE}" push origin "HEAD:${BRANCH}"
git -C "${ROOT}" worktree remove --force "${WORKTREE}" 2>/dev/null || rm -rf "${WORKTREE}"
