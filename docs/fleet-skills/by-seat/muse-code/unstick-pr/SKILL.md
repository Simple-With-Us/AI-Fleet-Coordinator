---
name: unstick-pr
description: Diagnose and repair a PR that will not merge — phantom vs real conflicts, unresolved review threads, CI dispatch misses, required-check failures, and known flakes. Use when mergeable is false, auto-merge sits idle, GitHub says CONFLICTING/BLOCKED/DIRTY, or a Muse Code/peer PR is green but stuck.
---

# Unstick a blocked PR (MC)

> **This install is for `MC`.** Slack `[MC]`.  Notes `Muse Code`.  Branches `muse-code/`.  Worktrees `~/apps/<app>-muse-code`.  Do not inherit another seat's tag from a shared template.

> **Runtime (Muse Code).** Muse Code (`muse` CLI) is the interactive terminal coding agent (`[MC]`).  Notes name `Muse Code`.  Branches `muse-code/`.  New lanes are made with `~/apps/lane new <app> <slug>` at `~/apps/lanes/<prefix>/muse-code-<slug>`, and `AGENT_SEAT=MC` comes from the `muse-seat` wrapper, never from a guess.  Start `muse` inside a lane; `muse -w` appears to make a worktree inside `~/Code/<App>/.muse/worktrees`, so do not use it.  Distinct from **Muse Assist** (`[MA]`, former tag `[MUSE]`), which is the cloud VM batch compute / creative assistant dispatched via Mac/iOS apps.  Project `AGENTS.md` and `CLAUDE.md` load automatically when the workspace is trusted in `~/.config/muse/trust.json`, and the user-level `~/.claude/CLAUDE.md` loads as a fallback.  Skills installed here (`~/.config/muse/skills`) shadow foreign personal skills.  Setup checklist: `docs/MUSE-ONBOARDING.md`.


Works in every fleet repo.  Substitute owner/repo from `gh repo view --json nameWithOwner`.

## 1. Snapshot

```bash
gh pr view <N> --json state,mergeable,mergeStateStatus,autoMergeRequest,statusCheckRollup,url
```

Note `mergeable` and `mergeStateStatus`.  Continue if OPEN and not cleanly mergeable, or if auto-merge is null.

## 2. Classify

### A) CONFLICTING or DIRTY

Distinguish phantom vs real with the **2-argument** `git merge-tree`.  The old 3-argument form always exits 0.

```bash
git fetch origin
git merge-tree --write-tree origin/main origin/<branch>
```

- Exit 0 = **PHANTOM**.  GitHub's mergeability cache stuck (common under concurrent push bursts).
- Exit 1 with conflict markers = **REAL**.

**Phantom fix:** merge `origin/main` in your lane and push a fresh head SHA.

```bash
cd "$(~/apps/lane path <app> <slug>)"   # your lane; never ~/Code/<repo>
git fetch origin
git merge origin/main --no-edit
git push origin muse-code/<slug>
```

GitHub recomputes in ~20–60s and re-dispatches CI.  If several PRs are stuck, push one at a time, ~10–15s apart.

**Real conflict:** merge `origin/main` by hand in the worktree.  `STATUS.md`, `PLAN.md`, and `docs/EFFORT-LOG.md` often have `merge=union` — additive board prose combines.  Never delete another agent's row.  If code intent is unclear, abort and escalate on THE BOARD + `#agent-sync`.

### B) BLOCKED, checks green

Unresolved review threads.  Branch protection requires conversation resolution (`enforce_admins: true`).  Use `codex-triage` (all review bots, not only Codex).  Re-check `gh pr view` after.

### C) Required check FAILURE

Do not guess.  Open the failing job.

- First-time flake on a known-flaky e2e job: `gh run rerun <run-id> --failed` **once**.  A second identical failure is real.
- ST Playwright `smoke` (`test/e2e/dashboard-smoke.spec.ts`) has a documented flake class: `docs/rollouts/2026-06-22-e2e-smoke-auth-fix.md`.
- Money-path / typecheck / gitleaks failures are real until proven otherwise.

CI runs on Coolify self-hosted runners (`congress-ci` / `socratic-ci` / `hetzner-ci` labels).  Do not start or "fix" a local Mac Actions runner — permanently banned for PR checks.  iOS ship is GitHub-hosted `macos-latest` only (Compiler / `GB-COMPILER`).  DealDex's hosted Actions ship stays — do not disable it.  Do not compile or upload from this Mac, and do not force a ship past the gate.

### D) No CI on this head SHA

Same class as phantom.  Push a fresh head as in A.

## 3. Re-arm auto-merge

```bash
gh pr merge <N> --squash --auto
gh pr view <N> --json autoMergeRequest
```

`autoMergeRequest` must be an object, not `null`.  Not `--admin`.

## 4. If it still hangs

Repeat 1–3.  Comment on the matching board item.  Do not force-merge, do not disable protection, do not resolve threads without a reply.

## Canon

- `/Users/jay/apps/AGENT-SYNC.md` — Merge requirements; CI runner policy
- App `AGENTS.md` Pull requests section
- Skills: `codex-triage`, `land-lane`
- ST flake origin: `docs/rollouts/2026-06-22-e2e-smoke-auth-fix.md`
