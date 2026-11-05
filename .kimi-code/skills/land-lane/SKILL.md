---
name: land-lane
description: Land a Kimi feature branch to main across the fleet — seat worktree, docs, verification gate, PR, auto-merge, then closeout.  Use when finishing a unit, opening a PR, merging, or when the owner says land/ship/commit/push.  Never wait for the owner to ask.  Covers apps with and without scripts/land.sh.
---

# Land a feature branch (KIMI)

> **This install is for `KIMI`.**  Chat tag `[KIMI·session8]`.  Notes `Kimi`.  Branches `kimi/`.  Lanes `~/apps/lanes/<Repo>/kimi-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot for seat `KIMI`, credential in Infisical Fleet-OPS `/zulip` (`KIMI_ZULIP_EMAIL`, `KIMI_ZULIP_KEY`, `KIMI_ZULIP_SITE`).  Session tag `[KIMI·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime (Kimi Code).**  Kimi Code CLI runs from `~/.local/bin/kimi` with config in `~/.kimi-code`.  Skills installed here (`~/.kimi-code/skills`) are loaded by Kimi Code.  Start Kimi inside a lane (`~/apps/lanes/<Repo>/kimi-<slug>`).  A seat a launcher assigned beats the KIMI default.


Always-commit is standing policy.  After each coherent finished unit: commit → push → PR → merge when CI is green.  A remote branch with no PR is unfinished.  Pause only for force-push, prod data wipe, or live-key revoke.

Seat: **KIMI**.  Branch: `kimi/<slug>`.  Never another seat's branch prefix.  Never land from `~/Code/<repo>` or from branch `main`.

## Preconditions

1. You are in your lane (`~/apps/lanes/<Repo>/<seat>-<slug>`, which on the server is the lanes root the environment names in `FLEET_LANES_ROOT`, made with `~/apps/lane new` on the Mac and with `lane new` on the server; a lane in an old place (`~/apps/lanes/<prefix>/`, which the migration moves) that already exists is fine).  See `session-start`.
2. `git status` is clean except `.env.local` / `.dev.vars` (never commit those).
3. `git config user.email` is `12656028+jaywedgeworth22@users.noreply.github.com`.

```bash
git config user.email "12656028+jaywedgeworth22@users.noreply.github.com"
```

## Docs before the PR

1. Linear issue → In Progress with honest status (the MCP tool `linear_claim` with `id`, branch `<seat>/<id>-<slug>`).  The effort logs are archived, so there is no `docs/EFFORT-LOG.md` row to mirror.  The PR body says `Fixes <ID>` if it finishes the issue, `Refs <ID>` if not.
2. `STATUS.md` stanza: what landed, next action.
3. `docs/rollouts/YYYY-MM-DD-slug.md` — summary, why, files, verification commands actually run, follow-ups.
4. Substantial owner-facing work: living Apple Note via the `apple-notes` skill, title `[TEAM_KEY, Agent] …`.

Prose:  commit bodies, rollout docs, Notes source, PR titles, bodies and comments, and chat replies to the owner all take a real U+00A0 plus a normal space between sentences (owner ruling 2026-10-23; convert with the `perl` recipe in `sentence-gap`).  The one exception: the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the `&nbsp;` entity plus a normal space.  See `owner-copy`.

## Gate

### Socratic.Trade — `scripts/land.sh`

Homebrew default `node` may be `v26`.  ST `better-sqlite3` is Node 24 (MODULE_VERSION 137).  Confirm `node --version` is v24.x:

```bash
export PATH=/opt/homebrew/opt/node@24/bin:$PATH
node --version
PATH=/opt/homebrew/opt/node@24/bin:$PATH bash scripts/land.sh
```

The script (idempotent) refuses the integration worktree and branch `main`, refuses a dirty tree, fetches, refuses auto-merge on stale file overlap with `origin/main`, merges `origin/main`, runs `npx tsc --noEmit` → `npm test` → `npm run build`, pushes, opens a PR.

ST `land.sh` may still mention a retired path (`~/Code/Agentic Trading`).  The binding keepout is **any** `~/Code/<repo>` checkout.

If the workflow still needs a dispatch, use the relay tool `gr_workflow_dispatch` (`identity` = your own seat tag):  `repo`, `workflow`, `ref`.

```bash
bash scripts/land.sh
```

**AI-Fleet-Coordinator also has a `land.sh` — do not run it.**  It is an ST clone (Node 24 + tsc/test/build) and this repo has no `package.json`.  Docs-only: commit, push, then open the PR with the relay tool `gr_open_pr`.

### Other apps — merge main, run *that* repo's verify, then PR

```bash
git fetch origin
git merge origin/main --no-edit
# then the gate below — do not invent npm test
git push -u origin HEAD
```
Open the PR with the relay tool `gr_open_pr` (`identity` = your own seat tag):  `repo`, `base`, `head`, `title`, `body`.

| App | Verify (from current AGENTS.md / ci.yml) |
|-----|------------------------------------------|
| CT | `cd app && npm run typecheck && npm test` (Deno).  CI job `typecheck + test`. |
| UM | `npm run verify` (eslint, tsc, vitest, build).  Node from `.node-version` (24). |
| CTS | `npm run typecheck && npm test && npm run build` (plus lint:package / pack:dry if you cut a release).  CI Node 20, job `verify`. |
| DealDex | `npm run lint && npm run typecheck && npm test && npm run build`.  CI Node 22, job `verify`.  Do not use `dealdex.vercel.app` (different site). |
| Personal-Site | CI `verify` is file-existence + About-copy grep.  `site/` is the TanStack Start source (README).  `AGENTS.md` may still say "static snapshot" — believe README + the tree.  Preserve `Earlier work included` and the Doximity `/profiles/…/view` URL or the daily mirror reverts them. |
| AFC | No app test gate.  `python3 scripts/check-fleet-registry.py` if you touched registries. |

CTS "prod" is an annotated tag `vX.Y.Z` after merge — announce on `#Work-In-Progress` then tag.  Consumers pin the exact tag.

`STATUS.md` / `PLAN.md` (and the archived `docs/EFFORT-LOG.md`) often carry `merge=union` in `.gitattributes`.  Additive edits usually combine.  Never delete another agent's row while resolving.  Up to 3 land attempts are fine.

## Request the Kody review

Kodus is in manual mode, so a push starts nothing.  Once the head is ready, read the PR's comments for an existing request on that head, then post a top-level comment that starts with `@kody start-review` (the relay tool `gr_comment_pr`).  A head was reviewed only when its `Kody Code Review` check is `success` and its output says `Code Review Complete` or `Review finished successfully`; treat every other result as no review.  Answer every finding with its fix, or a dispute based on the code, before you merge.  The full rule is in `AGENTS.md`, section "Kody reviews are requested by hand".

## Arm auto-merge

Arm auto-merge with the relay tool `gr_wf_automerge_enable` (`repo`, `number`, `method` = `SQUASH`).

Not `--admin`.  Branch protection is `enforce_admins: true` plus conversation resolution.  Unresolved review threads block forever.  Use `codex-triage` / `unstick-pr`.

If the box is gating several lanes, announce the full gate in #builds topic `gates`: the MCP tool `zulip_post` (`channel` = `builds`, `topic` = `gates`, `text` = "gating now (<repo>, <branch or purpose>)"), then post `gate clear` in the same topic when it finishes.  The server writes the tag.  Wait on a peer's open `gating now` with the CLI fallback `simple-chat wait --channel builds --topic gates --timeout 900` (it needs a seat:  `AGENT_SEAT`, `--as NAME`, `--default-seat NAME` or `--rc`).  Use the fleet wake (`@**all**` in #Work-In-Progress topic `fleet`) only when every listening seat on every platform must spend time.  `AFC` and `OPS` are repo acronyms, never a sender or a signature.

## After merge

- Linear issue: **Completed** (Done) means merged to `main`, not "PR opened."  A `Fixes <ID>` PR moves it on merge once Linear's GitHub automation is on; otherwise run the MCP tool `linear_status` (`id` = the issue, `status` = `completed`).
- **Deployed** only after the app's production target is verified (`deploy-verify`).
- Merge to `main` auto-deploys ST / CT / UM on Coolify and DealDex on Vercel.  Do not double-trigger.  Personal-Site live origin is Vercel behind Cloudflare and is **not** "merge = live" — do not mint a second Vercel project.
- Retire the lane once the PR is merged: `cd ~ && ~/apps/lane retire <slug>` (from outside the lane; it refuses a dirty, unpushed, open-PR or in-use lane and says why).  `~/apps/lane prune` lists all your lanes and `--apply` removes the merged ones.
- Closeout: `closeout` skill.

## Do not

- `LAND_SKIP_VERIFY` / `LAND_FORCE_PUSH` as an agent.  Humans only.
- Force-push shared history.
- Park a finished green PR "just in case."
- Claim Completed while threads are unresolved.

## Canon

- `~/apps/FLEET-PROTOCOL.md` — Always commit + land; merge requirements; branch/worktree naming
- App `AGENTS.md` Pre-Commit / Handoff + Verify before claiming done
- `scripts/land.sh` when present
- Skills: `session-start`, `unstick-pr`, `codex-triage`, `deploy-verify`, `closeout`, `owner-copy`
