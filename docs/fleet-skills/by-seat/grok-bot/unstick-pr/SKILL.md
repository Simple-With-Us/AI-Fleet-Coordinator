---
name: unstick-pr
description: Diagnose and repair a PR that will not merge — phantom vs real conflicts, unresolved review threads, CI dispatch misses, required-check failures, and known flakes.  Use when mergeable is false, auto-merge sits idle, GitHub says CONFLICTING/BLOCKED/DIRTY, or a Grok Bot/peer PR is green but stuck.
---

# Unstick a blocked PR (GB role)

> **This install is for Grok Bot roles.**  Chat tag is `[GB-<NAME>]` — `[GB-DIRECTOR]`, `[GB-FIXER]`, `[GB-DESIGNER]`, `[GB-COMPILER]` (Compiler), `[GB-HOUSEKEEPER]`, `[GB-PUBLISHER]`, `[GB-DEPLOYER]`, `[GB-MONITOR]`, `[GB-PLUMBER]`, `[GB-ORACLE]`, `[GB-TRADER]`, `[GB-ENGINEER]`.  Notes name is the role in Title Case (`Director`, `Monitor`, …).  Cloud branches are often `cursor/`.  Never `[GROK-BOT]`, `[CURSOR]`, `[GROK]`, or `[MONET]`.


Works in every fleet repo.  Substitute owner/repo from `gh repo view --json nameWithOwner`.

## 1. Snapshot

```bash
gh pr view <N> --json state,mergeable,mergeStateStatus,autoMergeRequest,statusCheckRollup,url
```

Note `mergeable` and `mergeStateStatus`.  Continue if OPEN and not cleanly mergeable, or if auto-merge is null.

## 2. Classify

### A) CONFLICTING or DIRTY

Distinguish phantom vs real with the **2-argument** `git merge-tree`.  The old 3-argument form always exits 0.

```bash
git fetch origin
git merge-tree --write-tree origin/main origin/<branch>
```

- Exit 0 = **PHANTOM**.  GitHub's mergeability cache stuck (common under concurrent push bursts).
- Exit 1 with conflict markers = **REAL**.

**Phantom fix:** merge `origin/main` in your lane and push a fresh head SHA.

```bash
cd "$(~/apps/lane path <app> <slug>)"   # your lane; never ~/Code/<repo>
git fetch origin
git merge origin/main --no-edit
git push origin cursor/<slug>
```

GitHub recomputes in ~20–60s and re-dispatches CI.  If several PRs are stuck, push one at a time, ~10–15s apart.

**Real conflict:** merge `origin/main` by hand in the worktree.  `STATUS.md`, `PLAN.md`, and the archived `docs/EFFORT-LOG.md` often have `merge=union` — additive prose combines.  Never delete another agent's row.  If code intent is unclear, abort and escalate with a Linear comment + `#Work-In-Progress`.

### B) BLOCKED, checks green

Unresolved review threads.  Branch protection requires conversation resolution (`enforce_admins: true`).  Use `codex-triage` (all review bots, not only Codex).  Re-check `gh pr view` after.

### C) Required check FAILURE

Do not guess.  Open the failing job.

- First-time flake on a known-flaky e2e job: `gh run rerun <run-id> --failed` **once**.  A second identical failure is real.
- ST Playwright `smoke` (`test/e2e/dashboard-smoke.spec.ts`) has a documented flake class: `docs/rollouts/2026-06-22-e2e-smoke-auth-fix.md`.
- Money-path / typecheck / gitleaks failures are real until proven otherwise.

CI for this repo runs on Coolify self-hosted runners (see the `runs-on` labels in `.github/workflows/` — do not copy runner hostnames or labels into public skill text).  Do not start or "fix" a local Mac Actions runner — permanently banned for PR checks. iOS ship is GitHub-hosted `macos-latest` only (Compiler / `GB-COMPILER`).  DealDex's hosted Actions ship stays — do not disable it.  Do not compile or upload from this Mac, and do not force a ship past the gate.

### D) No CI on this head SHA

Same class as phantom.  Push a fresh head as in A.

## 3. Re-arm auto-merge

```bash
gh pr merge <N> --squash --auto
gh pr view <N> --json autoMergeRequest
```

`autoMergeRequest` must be an object, not `null`.  Not `--admin`.

The merge, reply and thread-resolution actions have a credential-safe path: `git-relay`, with the MCP wrapper **GR-MCP** (`git-relay mcp`), which exposes `open_pr`, `edit_pr`, `reply_thread`, `comment_pr`, `merge_pr`, `workflow_dispatch` and `resolve_thread`; `mint_token` is deliberately not exposed over MCP, because it returns a raw credential.  A seat reaches the tools with its own bearer token: ST-MCP shares the same bearer-token arrangement, so a seat may find these tools on its ST-MCP connection, while other seats run GR-MCP directly as its own stdio server — the tool names are the same either way.  **The relay is not deployed yet** (verified Oct 23 2026): there is no daemon, no launchd job and no seat pointed at it, so the interim `gh` and shell path above is still the one that works, until the owner decides the cutover.

## 4. If it still hangs

Repeat `1–3`.  Comment on the matching Linear issue (`simple-board comment <id> ...`).  Do not force-merge, do not disable protection, do not resolve threads without a reply.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` — Merge requirements; CI runner policy
- App `AGENTS.md` Pull requests section
- Skills: `codex-triage`, `land-lane`
- ST flake origin: `docs/rollouts/2026-06-22-e2e-smoke-auth-fix.md`
