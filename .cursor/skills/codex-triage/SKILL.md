---
name: codex-triage
description: Triage unresolved GitHub review threads (chatgpt-codex-connector, Cursor Bugbot, and any other review bot or human) — classify against current HEAD, fix real findings in one batch, reply, then resolve.  Use when gating a merge, when branch protection blocks on conversation resolution, or when a bot re-reviews after push.
---

# Review-thread triage (CURSOR)

> **This install is for `CURSOR`.**  Chat tag `[CURSOR·session8]`.  Notes `Cursor`.  Branches `cursor/`.  Lanes `~/apps/lanes/<Repo>/cursor-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `cursor-bot@simplewithus.zulipchat.com`, credential in Infisical Fleet-OPS `/zulip` (`CURSOR_ZULIP_EMAIL`, `CURSOR_ZULIP_KEY`, `CURSOR_ZULIP_SITE`).  Session tag `[CURSOR·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime fork (Cursor).**  Local Cursor IDE / Auto on this Mac is `[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, your chat tag is `[GB-<NAME>]` (GB-DIRECTOR, GB-FIXER, GB-DESIGNER, GB-COMPILER, GB-HOUSEKEEPER, GB-PUBLISHER, GB-DEPLOYER, GB-MONITOR, GB-PLUMBER, GB-ORACLE, GB-TRADER, GB-ENGINEER) — not `[GROK-BOT]`, not `[CURSOR]`, and not `[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.


Name is historical (`codex-connector`).  Apply to **every** unresolved thread on the PR: Codex, Cursor Bugbot, Copilot, humans.

Unresolved threads block merge on every protected fleet repo even when checks are green.

## 1. Fetch unresolved threads

```bash
OWNER="$(gh repo view --json owner --jq .owner.login)"
REPO="$(gh repo view --json name --jq .name)"
gh api graphql -f query="query { repository(owner:\"$OWNER\", name:\"$REPO\") { pullRequest(number: <N>) { reviewThreads(first: 100) { nodes { id isResolved path line comments(first: 10) { nodes { author { login } body diffHunk } } } } } } }" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes | map(select(.isResolved==false))'
```

## 2. Classify against current HEAD

Three buckets only:

- **addressed** — already fixed on this branch after the comment.
- **false_positive** — bot misread context, or the rule does not apply here.
- **real** — valid.  For money-path / auth / execution / policy / billing files, have a second agent (or a frontier child) adversarially verify before you change behavior.

Do not classify from the stale diff hunk.  Read the file at HEAD.

## 3. Fix reals in one batch

One commit with a regression test per behavior change.  **Push before resolving any thread** (race in step 5).

## 4. Reply, then resolve

Do not resolve without a reply.

```bash
gh api graphql -f query='mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}' -F t=<threadId> -f b=<text>

gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=<threadId>
```

Reply shape (review-thread replies are PR text: a real U+00A0 plus a space between sentences, converted with the `perl` recipe in `sentence-gap`; since the 2026-10-23 owner ruling, prose files take the same U+00A0 plus a space, and code never does):

- **Real:** `Fixed in <short-sha>.  <function>.  Test: test/foo.test.ts.`
- **False positive:** why the rule does not apply, then resolve.
- **Addressed already:** cite the SHA that landed the fix.

Never blind-resolve to force a merge.  The gate exists because some findings are real (commit-author, licenses, money-path).

The reply and the resolution have a credential-safe path: `git-relay`, with the MCP wrapper **GR-MCP** (`git-relay mcp`), which exposes `open_pr`, `edit_pr`, `reply_thread`, `comment_pr`, `merge_pr`, `workflow_dispatch` and `resolve_thread`; `mint_token` is deliberately not exposed over MCP, because it returns a raw credential.  A seat reaches the tools with its own bearer token: ST-MCP shares the same bearer-token arrangement, so a seat may find these tools on its ST-MCP connection, while other seats run GR-MCP directly as its own stdio server — the tool names are the same either way.  **The relay is not deployed yet** (verified Oct 23 2026): there is no daemon, no launchd job and no seat pointed at it, so the `gh` GraphQL calls above are still the path that works, until the owner decides the cutover.

## 5. Auto-merge race

The instant the last thread resolves and CI is green, auto-merge fires.  Bots re-review every push and often never converge.

- Resolving the last thread **is** merging the PR.  Triple-check first.
- Round-2 comments often land on an already-merged PR.  Before resolving as "fixed," confirm the fix reached main.  Fleet repos squash-merge, so the original fix SHA is **not** an ancestor of `origin/main`:

```bash
git fetch origin
# WRONG after squash: git merge-base --is-ancestor <fix-sha> origin/main
gh pr list --head "$BRANCH" --state all --json number,state,mergedAt,url
git diff origin/main...HEAD    # three-dot remaining unique work
# helper: scripts/branch-landed.sh
```

If the PR is not MERGED, open a follow-up from the same branch.  Expect squash-merge conflicts — merge main in; this branch's newer rounds win in its own files.

## 6. Stop at round 2–3

Later rounds on a merged PR are mostly noise.  Triage genuine hazards; surface the rest to the owner (Notes + a Linear comment).  Cursor's job on these is the security/contract read, not infinite bot ping-pong.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` — Merge requirements
- App `AGENTS.md` Pre-Commit / Handoff
- Skills: `unstick-pr`, `land-lane`
