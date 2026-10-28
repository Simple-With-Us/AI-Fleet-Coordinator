# fleet_lanes

The tools behind the Lane Map (`docs/protocols/lane-map.md`): where every git checkout on this Mac may live, how a lane gets created in the right place, and how anything that cleans up learns what is safe to remove.  Python 3.9 or newer, standard library only.  Checked on macOS `/usr/bin/python3` 3.9.6: a doctor report over an empty fake home, the `lane` and doctor help text, and the `install_rules` and `install_tools` test modules (a subset of the `lane` tests).  The full suite is run under the default `python3`.  Layout rules (where checkouts may live, what lanes are called) come from `layout.py` in this package, which carries a small `StrEnum` fallback so the guard hook can import it under whatever `python3` its PATH finds.  The hook treats every exception as an allow.

| Tool | Command | What it does | What it writes |
|---|---|---|---|
| `lane` | `lane new`, `lane path`, `lane ls`, `lane retire`, `lane prune` | Creates a lane or a review checkout and prints its path; retires a seat's merged lanes. | A git worktree under `~/apps/lanes/<Repo>/` and a manifest inside the integration tree's `.git` folder.  Fetches the base branch (and probes origin for the new branch name) into the integration tree. |
| doctor | `lane ls`, `lane doctor`, `python3 -m fleet_lanes.doctor` | Finds every checkout on the Mac, says where it lives and whether removing it could lose work. | Nothing.  `--write PATH` saves the JSON report to that one file. |
| temp guard hook | `lane-guard-hook` (`lane_guard_hook.py`, `guard.py`) | Denies a checkout of a fleet repo in a temp directory and prints the lane path to use. | Nothing. |
| tools and hook installer | `python3 -m fleet_lanes.install_tools` | Installs a stable copy of the tools, wires the hook into each platform, and writes the Muse Code plugin bundle and seat shim. | The stable dir (with the hook shim, `muse-seat` and `muse-plugin/`), the `lane` shim, and one hook entry per platform config (backup first).  Nothing is written for Muse's own config. |
| rules installer | `python3 -m fleet_lanes.install_rules` | Puts the Lane Map rule text into each platform's own rules file. | One marker block per named rules file (backup first). |

The sections below cover `lane`, the doctor, then the two installers.  The safety invariants are stated per tool, because "never modifies anything" is true of the doctor and the hook only.

## lane

Creates the lane for one task and prints its path, or a read-only checkout of someone else's PR.  Layout v2 (owner 2026-10-09): the folder above a lane is the repo's folder name under `~/Code` exactly as spelled there (`Congress.Trade`, `AI-Fleet-Coordinator`), not the lowercase worktree prefix.  The seat comes only from the `AGENT_SEAT` environment variable, an uppercase registry tag such as `CLAUDE`, `AG`, `MM` or `CLUTCH` (any case is accepted).  If it is unset, unknown, a whole name such as `antigravity`, or a retired seat (`MONET`, `RENOIR`, `HARNESS`, `DSH`, `KIMI`), `lane` refuses; it never infers a seat from a login, a path or a branch.

```
~/apps/lane new <app> <slug> [--base REF] [--purpose TEXT] [--board ID] [--reuse-branch] [--dry-run] [--json]
~/apps/lane new <app> --review --pr N [--purpose TEXT] [--board ID] [--dry-run] [--json]
~/apps/lane path <app> <slug>          # prints the would-be path, no side effects
~/apps/lane path <app> --review --pr N # the review checkout path this seat would get
~/apps/lane ls [doctor options]        # every checkout on the Mac (the doctor report)
~/apps/lane doctor [doctor options]    # same as ls
~/apps/lane retire <path-or-slug> [--repo APP] [--dry-run] [--json]
~/apps/lane prune [--seat NAME] [--all-seats] [--repo APP] [--apply] [--json]

d=$(~/apps/lane new DealDex fix-login) && cd "$d"   # never cd "$(...)" alone: a refusal prints nothing and cd "" stays put
```

`~/apps/lane` is the shim that `install_tools apply tools` writes, and it runs the stable copy in `~/apps/lane-tools`.  From a checkout of this repo, `scripts/fleet_lanes/bin/lane` takes the same arguments (it follows symlinks to find its checkout and honours a caller-set `FLEET_APPS_JSON`), and `cd scripts && python3 -m fleet_lanes.lane` is the same thing.  Do not symlink `~/apps/lane` to `bin/lane`: `install_tools` leaves a foreign file at that path alone, `verify` then warns, and a checkout is exactly the staleness the stable copy avoids.  Until the owner approves the install, `~/apps/lane` does not exist.

| Option | Meaning |
|---|---|
| `<app>` | Repo name, repo folder, old worktree prefix or acronym, any case (`Congress.Trade`, `congress`, `CT`).  Dots and dashes are ignored only if nothing matches exactly.  An unknown or ambiguous app exits 64 and lists the candidates. |
| `<slug>` | Lowercase kebab, 1 to 40 characters.  No seat, app or date inside it. |
| `--base REF` | `origin/<branch>` or a bare `<branch>` that exists on origin.  Default `origin/main`.  Cannot be combined with `--reuse-branch`. |
| `--purpose TEXT` | One line, at most 300 characters, saved in the manifest. |
| `--board ID` | A board id saved in the manifest. |
| `--reuse-branch` | Check out a branch that already exists (local or on origin) instead of creating one. |
| `--review --pr N` | A detached, read-only checkout of the head of pull request N at `~/apps/lanes/<Repo>/review-pr-N`.  Takes no slug, `--base` or `--reuse-branch`. |
| `--dry-run` | Change nothing.  A review dry run skips `gh`, so its head sha is null. |
| `--json` | Print the manifest plus `"path"` as one JSON object (`"existing": true` on an idempotent re-run, `"dry_run": true` on a dry run). |

Exit codes: 0 ok, 64 usage error or refusal, 69 git or gh failed, 70 internal error (a command refused by lane's own allowlist, which is a bug).  `lane ls` and `lane doctor` pass the doctor's own codes through (0, 2 for `--strict`, 64).  Stdout is the path alone, or one JSON object with `--json`, so `d=$(lane new ...) && cd "$d"` works.  Do not write `cd "$(lane new ...)"` without the `&&` chain: when `lane` refuses it prints nothing, `cd ""` succeeds without moving, and the next command runs in the folder you were already in.  Every human message is on stderr and starts with `lane: `.

Paths and names (the folder uses the seat's whole `worktreeSuffix`; the branch uses the seat's first registry branch prefix):

| Kind | Path | Branch |
|---|---|---|
| Lane | `~/apps/lanes/<Repo>/<seat>-<slug>` | `<branch prefix><slug>`, for example `claude/fix-login`, `minimax/fix-login`, `ag/fix-login` (folder `antigravity-fix-login`) |
| Review | `~/apps/lanes/<Repo>/review-pr-<N>`; a second seat on the same PR gets `review-pr-<N>-<seat>` | Detached at the PR head |
| Claude desktop worktree | `~/apps/lanes/<Repo>/<slug>-<6 hex>` (the app names it; not made by `lane`) | Tool-chosen |
| Codex desktop worktree | `~/apps/lanes/_codex/<slug>/<Repo>` (not made by `lane`) | Tool-chosen |
| Conductor workspace | `~/apps/lanes/_conductor/<Repo>/<city>` once the app's workspace location is changed (not made by `lane`); until then `~/conductor/workspaces/<Repo>/<city>` | Tool-chosen |
| Legacy flat mode (`FLEET_LAYOUT=flat`), no longer documented as a way to make lanes | `~/apps/<prefix>-<seat>-<slug>`; it has no review folder, so `--review` is refused | Same |

`<Repo>` is `App.lane_dir`: the registry `codeDir` (the folder name under `~/Code`), else the GitHub repo name for a repo with no tree there.  It is not held to the lowercase-kebab rule that seats and slugs follow, and it is validated as one path component (letters, digits, `.`, `-`, `_`).  A lane that has not migrated yet (`~/apps/lanes/fleet/claude-x`, a flat `~/apps/fleet-claude-x`) is found by `lane new`: it prints that path, says the migration will move it, and makes nothing.

What `lane new` refuses (exit 64): an app with no integration tree in `~/Code` or no `fleet-apps.json` row (add the row first); an integration tree that is missing, is not a repo top, or has no `origin`; a path that is not `LANE_NESTED` (`LANE_FLAT` in flat mode) per `layout.classify_location`, or is under `~/Code` or inside another checkout; a folder name the layout would read as a different seat; a branch that already exists locally or on origin, or collides with a parent or child branch (unless `--reuse-branch`); a foreign thing already at the path; a registered worktree whose folder is gone (run `git worktree prune` yourself); a missing `~/apps`.

Flow for a new lane: offline checks, then `git fetch --no-tags origin <base>`, resolve the base sha, `git fetch --no-tags origin <new branch>` as an existence probe (it must answer "couldn't find remote ref"), create the parent folders under the lanes root, `git worktree add -b <branch> <path> <base sha>`, write the manifest.  The branch starts at the sha, so it has no upstream; the first push sets it.  A review runs `gh pr view N --json headRefOid,headRefName,state,isCrossRepository`, fetches `pull/N/head`, checks the head sha arrived (otherwise exit 69, "PR updated, run again"), and adds a detached worktree.  A PR that is not open only prints a note.

Idempotent: if the path is already that tree's worktree on that branch (detached for a review), `lane new` prints the path and exits 0 with no fetch and no write.  It does not move a review to a newer PR head.

Manifest: `lane.json` lives in the new worktree's private git dir, `<tree>/.git/worktrees/<name>/lane.json`, never in the working tree, written atomically.  A lane records `schema` 1, `tool`, `kind` "lane", `seat`, `tag`, `app`, `prefix`, `lane_dir`, `slug`, `branch`, `base`, `base_sha`, `purpose`, `board` and `created_at` (UTC, ISO-8601).  A review records `kind` "review", `pr`, `head_sha`, `head_ref`, `pr_state`, `cross_repo` and `expires_at` (created plus 7 days).  A failure to write the manifest is a warning on stderr and does not change the exit code.  `lane` never creates a `.janitor-keep` file: that marker makes the doctor say NEEDS-REVIEW forever, so it is not a way to protect a lane.

Process rules: every process starts in one runner that checks an allowlist first (`git fetch --no-tags origin <ref>`, `git worktree list --porcelain`, `git worktree add` in exactly three shapes with an absolute path strictly inside the lanes root, `git rev-parse`, `git show-ref --verify`, `git for-each-ref`, `git config --get remote.origin.url`, and `gh pr view` for four JSON fields).  Anything else raises before a process exists.  Credentials in git and gh error text are redacted.  Concurrent fetches into the same `~/Code` tree can fail on a ref lock (exit 69); rerun.  Repo hooks such as `post-checkout` run during `git worktree add`, as they would by hand.

Environment read: `AGENT_SEAT`, `HOME`, `FLEET_LAYOUT` (`nested` default or `flat`), `FLEET_LANES_ROOT`, `FLEET_LANES_EXTERNAL_ROOT` (where lanes may physically live when `~/apps/lanes/<Repo>` is a symlink onto the external disk; unset means `/Volumes/External/Lanes`, empty means off; see External Lanes in `docs/protocols/lane-map.md`), `FLEET_APPS_JSON` (the registry; the installed shim pins it to `~/apps/lane-tools/fleet-apps.json`).  A brand-new app or seat is refused until its row is in the registry the shim reads, so the first lane for a new app comes from a checkout of this repo: `cd scripts && FLEET_APPS_JSON=../fleet-apps.json python3 -m fleet_lanes.lane new <app> <slug>`.  After the row merges, `install_tools apply tools` refreshes the stable copy.

### lane retire and lane prune

`retire.py`.  `lane retire <slug-or-path>` removes one lane of your own seat (`AGENT_SEAT`) once its work is safe on GitHub; run it from outside the lane (`cd ~ && lane retire fix-login`), because a shell parked inside counts as a process using it.  `lane prune` walks every registered repo's `git worktree list`, takes the lanes of one seat (yours by default; `--seat NAME` accepts `CLAUDE` or `claude`; another seat needs `--all-seats`, and `--all-seats` alone means every seat), and prints one row per lane with a decision (`remove`, `keep`, `skip`) and the reason.  It is a dry run unless `--apply` is given.  `--json` prints the same rows plus `notes` and `errors`.

The removal rules, the two extra `prune` guards (activity in the last 15 minutes; an unused lane held until idle 24 hours) and the order of the checks are in the `retire.py` docstring and in `docs/protocols/lane-map.md` (Retiring A Lane).  In short: a registered linked worktree under the lanes root, the chosen seat's by name and manifest, not locked, no process cwd inside, no git operation or `index.lock`, no `.janitor-keep`, clean (including no git-ignored local state that is not build output, by `doctor.split_ignored`), nothing unpushed to a branch that exists on origin now (live `git ls-remote` shas or `origin/<default>`, never another possibly stale tracking ref) unless a merged or closed PR holds HEAD, and done (a MERGED or CLOSED PR for the branch, the upstream name or HEAD whose head holds HEAD, with none open; HEAD in `origin/<default>`; or the upstream branch gone from origin).

Its runner has its own allowlist (`retire.check_allowed`); `lane new`'s still refuses every remove.  Writes: `git worktree remove <path>` (never `--force`), `git branch -d` (never `-D`; "not fully merged" after a squash merge is reported as a kept branch, not an error) and, only for stale entries that are the chosen seat's lanes and never while a `lanes/<Repo>` symlink is dangling, `git worktree prune`.  Reads: `git status --ignored`, `rev-list` (against the live origin shas, `--ignore-missing`), `merge-base --is-ancestor`, `config --get branch.<b>.merge`, `symbolic-ref`, `ls-remote --heads origin` (one per repo), one bulk `gh pr list --state all --limit 1000` per repo (plus `--head <name>` for a lane the bulk list could not place) and one `lsof -d cwd -Fpcn`.  Removals in a repo hold the same lock as `lane new`.  Exit codes: 0 ok, 64 usage or refusal (a `retire` of a lane that is not removable), 69 git, gh or lsof failed, or a removal failed; the table is still printed, and a lane whose facts could not be read is kept.

## doctor

One report-only command that finds every git checkout on this Mac, wherever it hides, says where it lives, and says whether removing it could lose work.  It answers "where did the ball get dropped" and makes disk cleaning safe.  It never deletes, moves, fetches or modifies anything.

### Usage

```
cd scripts
python3 -m fleet_lanes.doctor                      # summary tables, then what needs attention
python3 -m fleet_lanes.doctor --json --no-gh       # machine-readable, no network
python3 -m fleet_lanes.doctor --write /path/report.json --gh-limit 1000
python3 -m fleet_lanes.doctor --strict             # exit 2 if a forbidden or unsanctioned checkout exists
python3 -m fleet_lanes.doctor --cleaner-list       # only the paths a cleaner may remove, one per line
```

| Option | Meaning |
|---|---|
| `--json` | Print the JSON report instead of the text report. |
| `--write PATH` | Also write the JSON report to PATH (atomic: temp file, fsync, rename). |
| `--sizes` | Measure each checkout with `du -sk -x` (30 second cap each).  Off by default because it is slow. |
| `--deep` | Also scan `~/Documents`, `~/Desktop`, `~/Downloads` to depth 4 (iCloud-backed and permission-prompting, so off by default). |
| `--no-gh` | Skip PR lookups.  PR state becomes UNKNOWN for fleet checkouts off the default branch, and those checkouts report UNKNOWN instead of SAFE-TO-REMOVE (an open PR cannot be ruled out). |
| `--gh-limit N` | PRs fetched per repo, default 500, maximum 1000.  A list that fills the limit cannot prove a branch has no PR, so unmatched checkouts report UNKNOWN instead of NONE. |
| `--only-class CLASS` | Show only one location class.  `--strict` still judges the whole machine.  `cleaner_candidates` follows what is shown. |
| `--strict` | Exit 2 if any FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL or UNSANCTIONED checkout exists.  Plugin caches and third-party clones do not count toward UNSANCTIONED. |
| `--fresh-days N` | Number, 0 to 3650, default 7.  Sets the fresh-lane rule below.  The cleaner age floor is `max(N, 7)`, so no value makes a younger lane a candidate.  `nan`, `inf`, a negative or non-number exits 64. |
| `--cleaner-list` | Print only the `cleaner_candidates` real paths, one per line (nothing at all when the list is empty).  Notes about skipped or failed `gh` and `lsof` go to stderr.  Cannot be combined with `--json` (exit 64); use `--write PATH` to keep the JSON.  `--write`, `--strict` and `--only-class` still work with it. |
| `--home PATH`, `--tmp-root PATH` | Point the sweep at a fake home and fake temp root (tests). |

Exit codes: 0 normally, 2 for `--strict` hits (also with `--cleaner-list`), 64 for a usage error (so a bad flag cannot be mistaken for a strict hit).  The layout mode comes from `FLEET_LAYOUT` (`nested` by default, or `flat`).  The text report has a line `Cleaner candidates N (merged PR, clean, idle 7+ days; --cleaner-list prints the paths)` under the dropped-ball line.

### What it scans

Discovery is the union of two methods, deduplicated by real path (so `~/Code/Socratic.Trade`, a symlink to `~/Code/Socratic-Trade`, is one checkout).  Depth 0 is the scan root, and a checkout is found when its own depth is at most the limit.

1. `git worktree list --porcelain` in every discovered full clone and every parent repo that a `.git` file points to.  This finds registered worktrees that sit outside every scan root.
2. A bounded scan for `.git` entries (file or directory) that skips `node_modules`, `.Trash` and `Library`, never follows symlinks, and does not look inside a checkout it has already found.

| Root | Depth |
|---|---|
| `/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/tmp`, `$TMPDIR` | 4 |
| `~/apps` | 2 (every top-level entry, plus one level into non-git folders such as `mkt/`) |
| `~/apps/lanes` | 4 (reaches `lanes/<Repo>/<lane>` and `lanes/_codex/<slug>/<Repo>`) |
| each `~/apps/lanes/<Name>` that is a symlink onto the external lanes disk | 3, rooted at the link (so hits keep their `~/apps/lanes` spelling; the scan does not follow symlinks otherwise) |
| `~/Code` | 1, plus each `~/Code/<App>/.claude/worktrees` and each `~/Code/<App>/.muse/worktrees` at 1 |
| `~/.codex/worktrees`, `~/.cursor/worktrees`, `~/.ag/worktrees` | 3 |
| `~/.grok`, `~/.fx` | 4 |
| `~/.botfleet`, `~/.gemini/antigravity` | 5 |
| `~/.buzz` | 3 |
| `~` | 3 (skipping Documents, Desktop, Downloads, Pictures, Movies, Music, Public, Applications, and the tool caches `.cache`, `.npm`, `.cargo`, `.rustup`, `.nvm`, `.pyenv`, `.rbenv`, `.volta`, `.pnpm-store`, `.yarn`, `.bun`, `.gradle`, `.m2`) |
| `~/Documents`, `~/Desktop`, `~/Downloads` | 4, only with `--deep` |

Lanes stored on the external disk read the same on both spellings: git and `lsof` print `/Volumes/External/Lanes/<Repo>/<lane>`, and `layout.resolve_path` reports it as `~/apps/lanes/<Repo>/<lane>`, so `path`, `realpath`, `lane_root`, the cleaner list and the process-cwd match all use the lanes spelling.  `warnings` names a `lanes/<Repo>` link whose target is missing (the disk is not mounted), one that points outside the external root, one whose folder has another name, and a folder on the disk that no link reaches.

A registered worktree whose directory is gone is reported as PRUNABLE, not as a checkout.  A `.git` file whose gitdir target is missing is an ORPHAN.  Each checkout carries `found_by` so you can see which method found it.

### Reading the output

**Location class** (from `layout.classify_location`): INTEGRATION_TREE, LANE_NESTED, LANE_FLAT_LEGACY, LANE_FLAT, REVIEW, MANAGED, FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL, UNSANCTIONED.  Under layout v2 a review checkout `lanes/<Repo>/review-pr-<n>` is REVIEW and a Claude desktop worktree `lanes/<Repo>/<slug>-<6 hex>` and anything under `lanes/_codex` or `lanes/_conductor` (and the app's default `~/conductor/workspaces`) are MANAGED (so HogHunter's dependency step leaves them alone); the old `lanes/_review/**` and `lanes/_managed/**` keep REVIEW and MANAGED.  The class values never change.  The Claude harness scratchpad under `/private/tmp/claude-<uid>` is still FORBIDDEN_TMP here, because a checkout inside it is a checkout in a temp directory.  Only the temp guard exempts it.

**Layout status** (from `layout.explain_layout`; `layout_status`, `layout_reasons` and, for a legacy place, `layout_target` in the JSON; a "Layout (v2)" table in the text report): `correct` (`lanes/<Repo>/<seat>-<slug>`, `<slug>-<hex>`, `review-pr-<n>`), `legacy-migrate` (an old prefix folder such as `lanes/fleet`, a flat `~/apps/<prefix>-<seat>-<slug>`, `~/.codex/worktrees`; the new path is shown), `legacy` (`lanes/_managed/**`, `lanes/_review/**`), `codex-managed` (`lanes/_codex/**`), `tool-managed` (another harness's own folder, including `lanes/_conductor/**` and `~/conductor/workspaces/**`), `human` (the integration tree), `wrong` (a temp directory, a checkout directly under `~/Code`, a worktree inside `~/Code/<Repo>`) and `unsanctioned`.  A worktree inside `~/Code/<Repo>` (`.claude/worktrees`, `.muse/worktrees`) is also the `WRONG-PLACE` anomaly; it stays MANAGED so the cleaners still retire it.  `--strict` and the cleaner list do not change.

**Safety class**, in precedence order.  This says whether removing the checkout could lose work.  It does not say the location is disposable (an integration tree with no lanes can read SAFE-TO-REMOVE), and **a cleaner must never act on SAFE-TO-REMOVE alone**: before the fresh-lane rule, a lane created an hour ago with no commits yet read SAFE-TO-REMOVE, and the label still says nothing about whether anyone is about to use a lane.  Cleaners read `cleaner_candidates` instead (see the cleaner contract below).

| Class | Meaning |
|---|---|
| TOOL-CACHE | A clone a tool manages for itself (a plugin or marketplace cache under `~/.grok`, `~/.codex/memories` and similar), not a working copy.  It wins over everything, ACTIVE included.  `dropped_ball` is always false, and `safety_reasons[0]` says what it would read otherwise.  It stays in `checkouts`, `summary.tool_cache` and `summary.by_safety`, but never in the dropped-ball, NEEDS-REVIEW or UNKNOWN sections.  A tool cache inside a temp directory is still FORBIDDEN_TMP and still counts for `--strict`. |
| ACTIVE | A live process has it as its cwd, or it is `CLAUDE_PROJECT_DIR` or the doctor's own cwd.  A process in a nested checkout counts only for the deepest one.  Other reasons are still listed. |
| NEEDS-REVIEW | At least one positive reason removal could lose work.  Every reason is listed in `safety_reasons`. |
| UNKNOWN | No known risk, but a needed fact could not be read: an orphan, any git call that timed out or failed (including the clone-only counts of other branches and stashes, and a `git worktree list` on a parent), git resolving the directory to some other repository, an lsof failure, a PR state that gh could not give, or the age of a lane that cannot be ruled fresh. |
| SAFE-TO-REMOVE | Clean (ignored files included, see below), every commit reachable from a server's remote-tracking ref or covered by a merged PR, no open PR, nothing running.  `safety_reasons` holds the evidence. |

A failed `gh` or `lsof` call never hides a dirty tree: known risks win over unknowns.

NEEDS-REVIEW reasons:

- dirty files (tracked-modified and untracked counted separately)
- ignored local state: files git ignores that are not regenerable build output, such as a SQLite database and its `-wal` and `-shm` files, a dataset or a `.env`.  The doctor runs `git status --ignored` and sets aside these names as rebuildable: `node_modules`, `.DS_Store`, `.build`, `build`, `dist`, `.next`, `__pycache__`, `DerivedData`, `.venv`, `venv`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `dist-*`, `*.tsbuildinfo` and `*.pyc`.  Everything else is listed in `ignored_local` (first 50; `ignored_local_count` has the total) and shown as `ignored local state: data/app.db (+2 more)`
- a `.janitor-keep` marker in the directory (the housekeeper never retires these without asking the owner first)
- unpushed commits
- an open PR
- commits beyond a merged PR
- no `origin` remote, or an `origin` that is a local path
- a tracking ref of any other remote whose url is a local path (a peer lane, a scratch bare repo) or unreadable.  Only `origin` and server urls vouch for a push, so commits seen only through such a remote count as unpushed and the remote is named in the reasons
- a parent of registered linked worktrees (removing it breaks them)
- a locked worktree, or one the parent's worktree list does not know
- a fresh lane (see the fresh-lane rule below)
- an "only copy" of some commits, described next

"Only copy" means commits that nothing else references: a detached HEAD that no branch, remote branch or tag contains, other local branches in a full clone that hold commits not on any remote, or a stash.  This is how the doctor reads "the only checkout of a branch".  A stash lives in the parent repo, so it is counted on the full clone and not on its linked worktrees.

**Fresh-lane rule.**  A checkout whose location class is LANE_NESTED, LANE_FLAT_LEGACY, LANE_FLAT, REVIEW or MANAGED (not an orphan, not a tool cache) is a fresh lane when `lane_age_days` is under N days or `idle_days` is under N days (N is `--fresh-days`, default 7) and it has no merged evidence: PR state MERGED, or CLOSED (the doctor reports CLOSED only on an exact head match; BEYOND-MERGED, OPEN, NONE and UNKNOWN do not count).  It gets the risk reason `fresh lane: age 2d, idle 1d, not merged` and so reads NEEDS-REVIEW (ACTIVE still wins, with the reason still listed).  This is what stops a lane created an hour ago, with no commits yet, from reading SAFE-TO-REMOVE.  The rule does not set `dropped_ball` by itself.  If the age or the idle time of such a lane cannot be read, the reason is `lane age unknown: creation time and last activity could not be read, so a fresh lane cannot be ruled out` and the lane reads UNKNOWN unless a known risk makes it NEEDS-REVIEW.  Integration trees, UNSANCTIONED and FORBIDDEN_* checkouts ignore age.

**PR state** comes from one bulk `gh pr list --repo OWNER/NAME --state all --limit N --json number,state,headRefName,headRefOid,mergedAt,closedAt,isDraft,labels,createdAt` per repo, matched by branch name and head sha.  The three added fields only feed the auto-draft signal below.  The PR bodies are not in this call (a slow call would turn a whole repo UNKNOWN);  a repo with an open draft for a checkout that lacks the `fleet-autopush` label gets one more small call, `gh pr list --repo OWNER/NAME --state open --limit 100 --json number,body`, to look for the auto-draft body marker, and if that call fails only that answer is lost.  Fleet repos squash-merge and delete the branch, so ancestry says nothing.

| State | Meaning |
|---|---|
| OPEN | An open PR matches the branch or HEAD. |
| MERGED | HEAD equals, or is contained in, a merged PR head.  The commits live on GitHub, so "unpushed" and "only copy" reasons are suppressed before reasons are added up. |
| CLOSED | HEAD equals the head of a closed PR that was not merged.  The commits are on GitHub but were never merged. |
| BEYOND-MERGED | A merged PR exists for the branch but HEAD has commits past its head ("commits beyond merged PR"), or the merged head is not in the local repo to compare. |
| NONE | The lookup worked and nothing matches. |
| UNKNOWN | `gh` failed, was skipped, or the list filled the limit with no match.  Never read as NONE.  A checkout in this state reports UNKNOWN (reason `PR state unknown: ...`) unless a known risk already makes it NEEDS-REVIEW. |
| N/A | Default branch, no commits, no GitHub remote, or a third-party repo. |

**dropped_ball** is `true` for NEEDS-REVIEW checkouts that have dirty files or unpushed commits (HEAD's, or other local branches in a full clone that hosts no worktrees; not covered by a merged PR) and no open PR.  It is `null` when the PR state is UNKNOWN, because "no open PR" cannot be asserted; the summary counts those separately as `dropped_ball_unknown`.  It is `false` otherwise.  An open PR covers the lane, with one exception: an auto-draft that nobody has committed to since it opened (`pr_is_auto_draft` true and `auto_draft_seat_commit` false, see Lane signals) holds only what the auto-commit saved, so a lane with dirty files or unpushed commits under it is a dropped ball.  When either of those two is `null` the old rule stands and the open PR covers the lane.

`unpushed` is measured against remote-tracking refs already on disk.  The doctor never fetches, so a stale ref can overstate or understate it.

**Creating tool** is a guess from the path first, then the lane's seat token, then the branch prefix.  `creating_tool_basis` says which.  A Claude worktree is called claude-desktop when it sits at `lanes/<Repo>/<slug>-<6 hex>` (the desktop app's location under layout v2) or, under `.claude/worktrees`, when its name ends in `-<6 hex>`, and claude-cli otherwise, so treat that split as a hint.  Anything under `lanes/_codex` is codex.

**Tool caches** (plugin and marketplace clones under `~/.grok`, `~/.codex/memories` and similar) are kept in `checkouts` and the summary with `tool_cache: true` and the safety class TOOL-CACHE, but they do not raise UNSANCTIONED anomalies or `--strict`.  Third-party clones (a remote owned by someone other than the registry owner) are treated the same way for UNSANCTIONED only.  FORBIDDEN_TMP and FORBIDDEN_CODE_TOPLEVEL always apply.

### Lane signals (open-loops F4)

Report-only facts for the open-loops digest, the auto-push sweep and the lane retirer.  They add fields to every checkout and one NEEDS-REVIEW reason.  They add no command to the allowlist: the files are read directly, and the git calls are forms the doctor already used.

| Field | Meaning |
|---|---|
| `git_op_in_progress` | Names of `MERGE_HEAD`, `rebase-merge`, `rebase-apply`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `BISECT_LOG`, `sequencer` and `index.lock` that exist in the checkout's **own** git dir (`.git/worktrees/<name>` for a linked worktree, which is where git keeps them).  Any of them gives the NEEDS-REVIEW reason `git operation in progress (...)`, so the lane is never a cleaner candidate and an auto-commit must leave it alone. |
| `index_lock_age_hours` | Age of `index.lock` when it exists.  Over an hour the reason adds "may be left over from a crash".  It is still held back: the doctor cannot tell a crash from a running command, and it never removes the lock.  Clearing a stale lock is a person's decision (C5-2 refuses any lane with one). |
| `dirty_oldest_age_hours`, `dirty_newest_mtime` | Age of the oldest dirty path and the newest dirty path's modification time (UTC), over tracked edits, staged changes, **untracked files** and, for a deletion, the folder the file left.  Untracked files count because `fleet-autopush` commits and pushes them, so a lane that only created new files is not idle.  Ignored files do not count.  A new untracked folder is listed file by file (one more `git status --untracked-files=all`), and every path is read with `lstat`, so a symlink is never followed.  `dirty_times_partial` is true when more than 3000 paths were dirty or that full listing failed. |
| `last_change_at`, `last_change_basis` | The newest of the dirty file times, the index mtime, the HEAD reflog mtime and the last commit time, and which one won (`dirty-file`, `index`, `reflog`, `last-commit`).  **The index mtime is weak evidence**: any tool's plain `git status` rewrites the index, so it can make a lane look busier than it is but never idler.  `null` when the dirty listing could not be read: an unknown must not read as quiet.  Consumers (C1-5's 2-hour rule, C2) also check the files themselves before acting. |
| `never_pushed`, `never_pushed_age_hours` | A branch with no upstream whose commits no trusted remote has.  `null` for a detached HEAD or an unreadable fact, `false` when a merged PR covers the commits.  The age is the committer date of the oldest of those commits (skipped above 5000 commits). |
| `keep` | The `.janitor-keep` marker parsed as `key: value` lines: `present`, `valid`, `reason`, `by`, `set_on`, `expires`, `expired`, `board`, `problem`.  Valid needs reason, by, set and expires.  A bare `expires` date lasts through that day, UTC.  The file is read only when it is a regular file (never through a symlink, never a FIFO), at most 4096 bytes and 40 lines.  `reason`, `by` and `board` are free text someone else wrote: control characters are removed and the length is capped, but a consumer still renders them as quoted, untrusted text. |
| `pr_is_auto_draft`, `auto_draft_seat_commit` | `pr_is_auto_draft`: the checkout's open PR is a draft carrying the `fleet-autopush` label or the body marker `<!-- fleet-autopush:auto-draft v1 -->`.  True only when every open PR for the branch is one, false when any is an ordinary PR (or no PR is open), `null` when it cannot be told (a draft without the label whose body could not be read).  `auto_draft_seat_commit`: the branch **tip** is a commit made after the draft opened that has no `Fleet-Autopush: v1` trailer.  Only the tip is looked at, so a seat commit under a later auto-commit reads as "no seat commit", and a seat that forges the trailer only makes its own lane look unattended. |

A legacy empty `.janitor-keep` is `valid: false` and still honored.  Any present marker, valid or not, expired or not, keeps `janitor_keep` true: an expired marker tells a person to look again, never that a delete is allowed.

### The cleaner contract

`cleaner_candidates` is the only list a cleaner may act on.  It is a sorted array of real paths, computed over the checkouts shown (so `--only-class` narrows it), and a checkout is on it only when ALL of these hold:

- safety is SAFE-TO-REMOVE and the PR state is MERGED or CLOSED (merged evidence, not "no PR found");
- `lane_age_days` and `idle_days` are both known and at least `cleaner_min_days` (the larger of `--fresh-days` and 7);
- no `.janitor-keep` marker, no `git_op_in_progress`, `cwd_procs` is an empty list (lsof worked and found no process) and `active` is false;
- the location class is one of the five lane classes, it is not a tool cache, and `read_errors` is empty;
- its `owner_repo` has `gh_repos[repo] == "ok"` (the PR list for that repo was read in full);
- it is a LINKED-WORKTREE that is registered in its parent, has no other discovered checkout inside it, and has a path with no newline, carriage return or NUL.  A full clone in a lane folder can read SAFE-TO-REMOVE but is never a candidate.

A cleaner that reads a saved report checks first: `schema` is at least 2, `generated_at` is recent, `lsof` is `"ok"`, and `gh_repos[repo]` is `"ok"` for the repo.  It acts only on `cleaner_candidates`, removes with `git worktree remove` (never `rm -rf`), and never creates a `.janitor-keep` file.  `docs/protocols/lane-map.md` § Cleaners has the owner-facing statement.

### JSON schema (schema 2)

Schema 2 is schema 1 plus `fresh_days`, `cleaner_min_days`, `gh_repos`, `cleaner_candidates` and `summary.cleaner_candidates`; `by_safety` can now hold `TOOL-CACHE`.  The lane signals (open-loops F4) are additive checkout fields in the same schema: nothing was renamed, removed or retyped, and a consumer that ignores them reads what it always read.

```
{
  "schema": 2,
  "generated_at": "2026-10-07T16:15:00+00:00",   # UTC, from an injected clock
  "host": "...", "layout_mode": "nested|flat", "home": "...", "only_class": null,
  "fresh_days": 7, "cleaner_min_days": 7,        # the option, and max(option, 7)
  "lsof": "ok|unavailable", "gh": "ok|partial|failed|skipped", "warnings": [...],   # partial/failed name the repos
  "gh_repos": {"owner/repo": "ok|failed"},       # every repo that needed a PR lookup
  "scan": [{"label", "path", "depth", "found"}],
  "checkouts": [ <checkout> ],
  "anomalies": [{"type", "path", "realpath", "detail"}],   # most urgent first
  "cleaner_candidates": ["/real/path", ...],     # see the cleaner contract
  "summary": {
    "total", "total_found", "strict_violations", "dropped_ball", "dropped_ball_unknown",
    "forbidden_tmp", "tool_cache", "cleaner_candidates",
    "by_location_class", "by_safety", "by_creating_tool", "by_repo", "by_kind", "by_pr_state",
    "anomalies_by_type"
  }
}
```

A checkout holds: `path`, `realpath`, `kind` (LINKED-WORKTREE, FULL-CLONE, ORPHAN), `parent_repo`, `owner_repo`, `remote_url` (credentials redacted), `scope` (fleet, third-party, unknown), `location_class`, `lane_root`, `creating_tool`, `creating_tool_basis`, `tool_cache`, `found_by`, `branch`, `detached_sha`, `head_sha`, `last_commit`, `lane_age_days` (since the directory was created), `idle_days` (since the last commit or HEAD move), `dirty_tracked`, `dirty_untracked`, `ignored_local_count`, `ignored_local`, `ignored_regenerable`, `unpushed`, `unpushed_all_branches`, `has_stash`, `upstream`, `upstream_state` (none, in-sync, ahead, behind, diverged, gone), `pr_state`, `pr_number`, `pr_basis`, `registered`, `registered_worktrees`, `locked`, `cwd_procs` (null when lsof failed), `active`, `janitor_keep` (a `.janitor-keep` marker exists, in any state; NEEDS-REVIEW), `keep`, `git_op_in_progress`, `index_lock_age_hours`, `dirty_oldest_age_hours`, `dirty_newest_mtime`, `dirty_times_partial`, `last_change_at`, `last_change_basis`, `never_pushed`, `never_pushed_age_hours`, `pr_is_auto_draft`, `auto_draft_seat_commit` (these twelve are the lane signals below), `size_mb` (only with `--sizes`), `name_verdict` (null outside lane locations), `name_reasons`, `branch_verdict`, `safety`, `safety_reasons`, `dropped_ball`, `read_errors`.

Anomaly types, most urgent first: FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL, ORPHAN, PRUNABLE, UNSANCTIONED, FULL-CLONE-IN-LANE, NAME-DRIFT, NON-LANE.  ALIAS-ONLY names (`ag`, `mm`) are allowed and are not anomalies.

### Safety invariants (doctor)

- Every subprocess starts in `run_cmd(argv, cwd, timeout)`, which refuses (raises `CommandRefused` before any process exists) anything off this allowlist: `git status | rev-parse | rev-list | log | for-each-ref | branch --show-current | config --get | remote get-url | worktree list`, `gh pr list`, `lsof -d cwd -Fpcn`, `du -sk -x`.  The lane signals added no entry: they use the existing `git status --untracked-files=all`, `git rev-list HEAD --not --remotes=NAME`, `git log -1 --no-show-signature --format=...` and `gh pr list --state open` forms.  Flags are checked too, so `git log --output=FILE`, `git branch -D`, `git config --unset` and `git worktree prune` all fail.  `git status` accepts `--ignored` (it only adds listing lines) but not `--ignored=matching`, and `git rev-list` accepts `--remotes=NAME` only for a plain remote name.
- `git diff`, `fetch`, `gc`, `checkout`, `reset`, `stash`, `clean`, `worktree add/remove/prune/move` and `rm` are not on the list.  `git diff` is excluded on purpose: it refreshes and rewrites `.git/index` in other seats' checkouts.
- git runs with `--no-optional-locks -c core.fsmonitor=false` added by `run_cmd` (callers cannot pass global options), `GIT_OPTIONAL_LOCKS=0`, `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0` and `GIT_NO_LAZY_FETCH=1`.  `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR` and `GIT_OBJECT_DIRECTORY` are stripped from the inherited environment, so a hook that exported one cannot redirect `git status` at another repo's index.
- Timeouts are enforced per call (git and lsof 20 seconds, du 30, gh 60 because it lists over the network) and the whole process group is killed.  A repo that times out or cannot be read becomes UNKNOWN and the sweep continues.
- Credentials in remote URLs and in stored stderr are replaced (`user:token@host` becomes `REDACTED@host`), including passwords that contain `/` or `@` and a token in the user slot of an scp-style remote (`TOKEN@github.com:o/r.git`).  The ssh user `git` is kept.
- The test suite proves the refusals, then runs the doctor over real temp repos with a stale index and compares a hash and mtime of every file before and after, with a control that shows plain `git status` does rewrite that index.
- The cleaner contract only ever narrows the doctor's answer.  The new options change what is printed (`--cleaner-list`) or how a lane is labelled (`--fresh-days`); none of them adds a command to the allowlist.

### Known limits (doctor)

- On a system without a file birth time (Linux CI), `lane_age_days` falls back to the directory's modification time.  The age tests use an injected clock and were run on macOS only, so the Linux fallback is UNVERIFIED.
- A checkout nested inside another checkout is not reported separately unless git registers it (for example `/private/tmp/um-relay/repo` inside `/private/tmp/um-relay`).
- A checkout deeper than a scan allows (see the table) is invisible.  The `~` scan stops at depth 3 and skips the tool caches above, so a clone four levels under `~` is not found.
- A directory whose `.git` git does not accept (empty, or no HEAD) is reported UNKNOWN, with the enclosing repository's path in `read_errors`, not as that repository's state.  One `git rev-parse --show-toplevel --git-dir` per checkout checks that git resolves this very directory and the git dir its `.git` names, and a rejected clone is not asked for its worktree list (git would answer for the enclosing repository).
- Ignored files are read with a second `git status --ignored` call per checkout, so a timeout there leaves the dirty counts intact and only adds an `ignored:` read error.  Detection is a name list, not a judgment.  A regenerable-looking name (`build`, `dist`) hides everything under it, and a path git folds into one entry (an untracked directory that is wholly ignored shows as `data/`) is listed as that directory.
- Plugin caches outside the scan roots (`~/.claude/plugins`, `~/.cursor/plugins`, `~/.codex/plugins`) are not scanned.
- "Only the checkout of a branch" is interpreted as the "only copy" cases above.  Changing that is a one-place edit in `assess`.
- `gh` listing is capped at 1000 PRs.  Repos with more than that report UNKNOWN for branches the list cannot match.

## install_tools

Installs the lane tools and the temp-checkout guard hook from a STABLE copy, wires the hook into each platform that has a hook surface, then proves the install works.  Why a stable copy: a hook that ran from a lane checkout would change whenever that branch changed, and would break the day the lane is retired.  Run it from a fresh worktree at `origin/main`, because `VERSION` records the sha of whatever `--source` points at and `~/Code/<App>` lags `main`.

```
cd scripts
python3 -m fleet_lanes.install_tools plan   [TARGET ...]    # read-only; default all
python3 -m fleet_lanes.install_tools apply  TARGET ...      # a target is required
python3 -m fleet_lanes.install_tools verify [TARGET ...]    # default all
python3 -m fleet_lanes.install_tools --self-test            # temp fake home, never the real one
```

`TARGET` is `tools`, `claude`, `codex`, `grok`, `antigravity`, `cursor`, `minimax` (alias `mm`), `muse`, `clutch`, `kimi`, `copilot`, `opencode`, `fx` or `all` (`tools` plus the five platforms that get a hook entry in a config file, plus `minimax`, `copilot` and `opencode`; `fx` is reached by `all` only to report its blocker, and `apply` for it writes nothing).  `clutch` and `kimi` are marked blocks in a file the tool does not own (see Fleet Hook below); neither is in `all`.  `muse` is a different kind of target: Muse Code takes hooks from a plugin, so its files are part of the stable copy; the `muse` target plans them, installs that copy, and verifies them (see Muse Code below).  There is no separate dry-run flag: `plan` is the dry run, and it prints the files, both shim texts, and a unified diff of ONLY the edited `hooks` subtree of each config (secret-looking values masked, and nothing else from a config file, because `settings.json` has an `env` block).  The owner approves each file: read the plan, then `apply` one target at a time.

| Option | Meaning |
|---|---|
| `--home H` | Read and write under H instead of `$HOME` (tests and demos use a temp fake home; the library functions always take an explicit home). |
| `--stable-dir D` | Default `<home>/apps/lane-tools`. |
| `--source S`, `--registry F`, `--sha SHA` | The directory that holds `fleet_lanes/` (default: the checkout this runs from), the `fleet-apps.json` to install (default: the one beside the source), and the sha recorded in `VERSION` (default: `git rev-parse HEAD` of the source, else `unknown`). |
| `--timeout SEC` | Per-probe timeout, default 5.  Use 30 on a heavily loaded machine; a timeout reads as FAIL. |
| `--no-minimal-path` | Probe once, not also under `PATH=/usr/bin:/bin:/usr/sbin:/sbin`.  The minimal-PATH run needs `/usr/bin/python3`. |
| `--no-verify` | (`apply`) Skip the verify that normally follows. |
| `--strict` | (`apply`, `verify`) A WARN, such as Codex trust pending, counts as a failure. |

Exit codes: 0 ok, 1 any FAIL or refusal (and a NAMED platform that is not installed; the same platform reached through `all` is only SKIP), 64 usage error.  `plan muse` exits 0; `apply muse` exits 1 only when the stable copy cannot be installed.

What `apply tools` writes under `<home>`: `apps/lane-tools/fleet_lanes/*.py` (top-level modules only, no tests) and `apps/lane-tools/fleet_lanes/rules/*` (the rule templates `install_rules` reads beside itself, so the installed copy can run that tool at all), `fleet-apps.json`, `VERSION` (`sha=`, `package=`, `files=`, `tree=` digest), an executable `lane-guard-hook`, an executable `muse-seat`, the Muse plugin bundle `muse-plugin/fleet-lane-guard/` (`.muse-plugin/plugin.json` and `hooks/lane-guard.sh`), and `apps/lane`.  The hook shim and the `lane` shim are `/bin/sh` scripts that run `/usr/bin/env python3 -I -c` with a bootstrap that puts the stable dir first on `sys.path`, so a lane with its own half-edited `fleet_lanes` in the working directory can never shadow the installed copy.  The shim points `FLEET_APPS_JSON` at the stable copy of the registry, which is why a new app or seat needs `apply tools` again after its registry row merges.  The new copy is staged beside the stable dir, probed there in every supported format (Muse's included), and swapped in with two renames only if the probes pass; the Muse files travel in the same swap.  `apps/lane` is written only when the package has `lane.py`, and only when nothing foreign is at that path (a file, folder or symlink there is left alone and `verify tools` warns).  Nothing here creates a `.janitor-keep` file.

Platform entries (each platform's config dir must already exist, else the target is skipped; the hook command is the absolute stable `lane-guard-hook` path plus `--format FMT`, timeout 5):

| Target | File | Entry |
|---|---|---|
| `claude` | `~/.claude/settings.json` | New last group in `hooks.PreToolUse`, matcher `Bash` |
| `codex` | `~/.codex/hooks.json` | Same shape, appended last so the trust recorded for the existing hooks stays valid.  The new hook still needs trusting once through `/hooks` in Codex. |
| `grok` | `~/.grok/hooks/lane-guard.json` | Its own file; `imported-from-claude.json` is untouched |
| `antigravity` | `~/.gemini/config/hooks.json` | New top-level group `lane-guard`, matcher `run_command` |
| `cursor` | `~/.cursor/hooks.json` | `hooks.beforeShellExecution` entry with `failClosed: false` (a broken install fails open; only `verify` proves it works) |
| `copilot` | `~/.copilot/hooks/fleet-lane-guard.json` | A new fleet-owned file (never `~/.copilot/settings.json`): version 1, one `hooks.PreToolUse` group, matcher `Bash`, one command hook running `<stable>/fleet-guard-hook --format claude` with `timeoutSec` 5 |
| `opencode` | `~/.config/opencode/opencode.json` plus `<stable>/opencode-plugin/index.ts` | The stable plugin DIRECTORY listed by absolute path in the config's `plugins` array; the plugin registers `tool.execute.before` and denies by throwing (V2 rejects a file entry and never runs V1 plugin hooks) |
| `fx` | none | Nothing is written.  `plan fx` prints the blocker and a prepared compensating control; `verify fx` is FAIL UNSUPPORTED |

Config writes: the file must parse as a JSON object (otherwise refused, file untouched).  A backup `<file>.bak-lane-guard-<stamp>` (original bytes and mode) is made only when content changes; the original is re-read just before the replace and the write aborts if it changed; the temp file is in the same directory, fsynced, given the original mode, and renamed over the target; a symlinked config is written through; and a failed post-write check restores the original.  Merges only append or update an entry that already contains `lane-guard-hook`, so a second apply changes nothing.

`verify` takes the command string back out of the config by event and matcher, runs it through `/bin/sh -c` with a deny payload and an allow payload (a clean env, `HOME` set to `--home`, 5 second timeout, once with the inherited PATH and once with the minimal one), and checks the exact output format of that platform.  Deny must exit 0 with the platform's deny shape naming the probe destination; allow must exit 0 with EMPTY stdout.  A missing command (127), a non-executable one (126), a non-zero exit, a timeout, or silence is FAIL.  `verify tools` also checks that the digest matches the files, the registry parses, both shims are byte-identical to what this version writes, and `lane --help` exits 0.  The line `verify: N PASS, N FAIL, N WARN, N SKIP  -> healthy|NOT HEALTHY` is the answer; the presence of an entry is not.

UNVERIFIED (also printed by `plan`): whether Antigravity IDE builds fire `PreToolUse` at all and where its matcher key sits; whether Grok also runs the claude-format entry and picks up `hooks/*.json` without a restart; whether a running Claude session reloads an edited `settings.json` (apply prints "start a new session"); that Cursor reloads without a restart.  Cursor-created worktrees never pass through a shell hook.  The Muse items are listed under Muse Code below.  The Copilot and OpenCode items are listed under their own sections below: neither has a live firing proof on this Mac, and `verify` says so with a SKIP instead of claiming one.

Known limits: the stable-dir swap is two renames, so a hook call landing in the gap fails open, and a crash between them leaves a `.old-<pid>` folder; a duplicate `lane-guard-hook` entry is reported, not removed; a config whose formatting does not round-trip (compact one-line JSON) is re-indented whole on apply, and `plan` says so.

### Shell prefilter in the hook shim

The hook runs on every Bash call on five platforms, and starting Python costs about 0.2 s of each at the machine's current load.  The guard already returns early unless a command holds one of a few plain words (`guard._TRIGGER`), so the shim makes that call in the shell first: it reads the payload with `cat` and, when none of the words is in it, exits 0 with no output without starting Python.  Measured at load 500: about 60 ms for a benign Bash call through the shim, about 230 ms for one that reaches Python.

- **Derived, not typed.**  The words come from `guard.py` when the shim is generated (`read_guard_facts`: the imported guard for this checkout, the syntax tree of `guard.py`, read and never run, for any other tree, including the stable copy `verify` checks).  If `_TRIGGER` is not a plain alternation of lower-case literal words compiled with exactly `re.IGNORECASE`, the shim has NO prefilter, every call reaches the guard (correct, slower), and `plan`, `apply tools` and `verify tools` say so.
- **A superset of the guard's early exit, so it never allows what the guard would deny.**  The shell matches the raw payload, so it passes each word in any ASCII letter case (bracket pairs like `[cC][lL]...`: no `tr`, nothing to be missing from a minimal `PATH`); any JSON `\u` escape (`cl\u006fne` decodes to `clone`; a test covers that bypass); the three non-ASCII characters `re.IGNORECASE` folds onto a trigger letter (U+0130, U+0131, U+212A, found by scanning the Basic Multilingual Plane, with a test that nothing above it folds onto an ASCII letter); and any payload over `guard.MAX_COMMAND_CHARS`.
- **Bytes, whatever the locale.**  The shim sets `LC_ALL=C` before it reads the payload.  In a multibyte locale such as `ja_JP.SJIS` or `zh_CN.GB18030` an ASCII byte can be the second byte of a character, and bash (the macOS `/bin/sh`) and zsh then read the last byte of a UTF-8 character and the next letter as one character, so a trigger word glued to a non-ASCII character would never match; dash compares bytes anyway.  The caller's `LC_ALL` (set, empty or unset) is put back before Python starts.
- **Python gets the original bytes** (trailing newlines kept, written with `printf '%s'`), so its answer is the guard's, byte for byte.  A NUL byte cannot live in a shell variable and is dropped; NUL is not valid JSON, so the guard allowed such a payload anyway.
- **`verify` stays honest.**  The deny probe contains `clone`, so it reaches the guard and must come back as the platform's deny shape; a shim whose patterns were emptied (`empty_prefilter`, also a phase of `--self-test`) gives it no output and fails, and a shim with one word missing differs from the generated text and fails `verify tools`.
- **Tests** (`GuardFactsTests`, `PrefilterShimTests`): every row of `fixtures_guard.py` under every `--format` through the shim against the module's own `main()` (plus a sample of rows through `python3 -m fleet_lanes.lane_guard_hook`); a fake `python3` first on `PATH` that records each start (none for benign payloads; one for each word in four letter cases, a `\u` escape, each lookalike under three locales, and a payload one byte over the cap); byte-identical stdin at Python; malformed input; a trigger word glued to a non-ASCII character under `ja_JP.SJIS` and `zh_CN.GB18030` set through `LC_ALL`, `LC_CTYPE` or `LANG` (also zsh running as `sh`), and Python seeing the caller's `LC_ALL` unchanged; a latency smoke test that records and asserts nothing.  The shim runs under each installed shell among `sh`, `dash`, `bash` and `ksh`.  On a Linux image without those locales the locale tests pass trivially.
- **Limit.**  A payload that JSON-escapes non-ASCII text (`caf\u00e9`) costs a Python start, because a `\u` escape could spell a trigger letter.  Encoders that write UTF-8 directly, as Claude Code does, do not.

### Muse Code

Muse Code takes hooks from a plugin, not from a settings file.  From a read-only look at `muse` 1.4.3 (UNVERIFIED unless a test or `verify` repeats it): a plugin directory with `.muse-plugin/plugin.json` declaring a `PreToolUse` hook (`id`, `event`, `command` as an argv array, `timeoutMs`, `statusMessage`) passes `muse plugins validate`; a `matcher` field is rejected, so the hook fires on every tool call; the deny contract is the Claude shape and an allow is empty stdout with exit 0; hooks run outside Muse's sandbox with a cleared environment, so every path in the bundle is absolute.

- **The bundle** `muse-plugin/fleet-lane-guard/` is part of the stable copy, written with it by `apply tools`.  Its one hook has no matcher and the command `["/bin/sh", "<stable>/muse-plugin/fleet-lane-guard/hooks/lane-guard.sh"]`.  The wrapper exits 0 at once only when it can PROVE from the raw bytes that `guard._extract` will not see a shell call: the top-level key the guard reads (`tool_name`, or `toolName` when there is no `tool_name` anywhere) occurs exactly once, starts in the first 4096 bytes, has nothing between the first `{` and it that could nest or escape (`{`, `}`, `[`, `]`, a backslash), is followed by `:"` or `: "` (a string value), and no shell tool word from `guard._SHELL_TOOL_HINTS` plus `bash` appears after it in any ASCII letter case (nor U+0130, which `lower()` turns into an `i`), and the payload has no `\u` escape.  Everything else goes to the stable `lane-guard-hook --format muse` as the original bytes: a missing or `null` tool key (the guard reads both as a shell call), a key that only occurs nested or as a value, duplicate keys, a number, spacing before the colon, a payload whose key comes after a nested object.  So the filter is a superset of what the guard treats as a shell call, and a non-shell tool whose input mentions `bash` after the key still reaches the guard, which allows it.  It matches bytes under `LC_ALL=C`, like the shim, and gives the guard the caller's `LC_ALL` back.  Only `case` globs scan the whole payload, because `${x#*key}` and `${x%%key*}` are quadratic in dash and bash (seconds on a 256 KB payload) and run only on a key inside the 4096-byte window.  It always exits 0 (a failing guard can never read as a block) and needs nothing but `/bin/sh`.
- **This tool never edits Muse's config, and the only muse it runs is `muse plugins validate`, in the muse verify** (a bare `verify`, `verify muse`, `apply muse` unless `--no-verify`, and `--self-test`; see `verify muse` below).  `plan` never runs it.  `plan muse` (also in a bare `plan`) shows the bundle, its state, the manifest and the wrapper, and prints the OWNER ACTIONS: 1. `muse plugins install <stable>/muse-plugin/fleet-lane-guard --scope user`  2. `muse plugins approve fleet-lane-guard`  3. start a new Muse Code session; plus a note that `MUSE_EXPERIMENTAL_PLUGINS=1` may be required (UNVERIFIED).  `apply muse` is `apply tools` (a current stable copy is left alone) followed by the same actions, then the verify that runs `muse plugins validate` unless `--no-verify` is passed.
- **`verify muse`** (also in a bare `verify`) takes the hook command back out of the installed manifest and runs it the way Muse does: directly, with an EMPTY environment (`--no-minimal-path` gives it your `PATH` instead), from `/`.  A `bash` deny payload and the same payload as `Bash` must print the Claude deny shape naming the destination; an allow payload, and a `write` call whose content is the denied command, must print nothing; all must exit 0.  It checks `plugin.json`, the wrapper and `muse-seat` against what this version writes, that the manifest has exactly one hook and no matcher, and, when `muse` is on `PATH`, runs `muse plugins validate <bundle> --json`: PASS, or FAIL when Muse rejects the bundle.  That runs the real Muse launcher, which finds its install dir from its own path and, unless auto-update is off, stamps that dir and starts a background self-update; muse also creates a lock file under its config dir even to validate.  So it runs confined (`muse_validate_env`): `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`, `TMPDIR` and Muse's credential-file path inside a throwaway directory; none of the caller's `XDG_*` or `MUSE_*` variables except `MUSE_EXPERIMENTAL_PLUGINS`; `MUSE_NO_AUTO_UPDATE=1` and `MUSE_LOGIN=0`.  Confined is not read-only: the launcher still runs from its install dir (launcher version 3, from its source and a run of a sandboxed copy: with auto-update off it writes nothing there; what the Muse binary writes during `validate` is UNVERIFIED).  The test that runs the real muse is opt-in (`FLEET_LANES_TEST_REAL_MUSE=1`); the others use a fake launcher that leaves a stamp when the environment leaks.  With no `muse` the line is SKIP, never a failure.  Whether the owner has installed and approved the plugin cannot be seen from here, so the last line is a SKIP.
- **Tests** (`MuseWrapperTests`, `MuseWrapperRealGuardTests`, `MuseSeatTests`, `MusePlanApplyVerifyTests`): a table of raw payload shapes (null, missing, nested, duplicated and oddly spaced tool keys, a key only as a value, a `\u`-escaped or non-ASCII-glued shell word) that must reach the guard, plain non-shell calls that must not, under each installed shell; the same table through the installed wrapper with the REAL guard behind it, byte for byte against the guard's own `main()`; a shell word glued to a non-ASCII character under `ja_JP.SJIS` and `zh_CN.GB18030`; 256 KB payloads under every shell, which must stay fast; a script named `muse` that starts muse-seat again, with and without `exec`; and a fake launcher that leaves a stamp when the validate environment leaks.
- **Cost per call.**  The wrapper exits 0 in `sh` for a call that is provably not a shell call, and `fleet-guard-hook` does the same for a shell call that holds none of the lane guard's trigger words, none of the secret guard's (`SECRET_WORDS`: bearer, authorization, pgrep, .secrets, credentials, client_info, .env, id_rsa, .pem, .p12, key, token, secret, passw, dsn), no `ps` followed by white space and no JSON `\u` escape.  So an ordinary shell call never starts Python.  `timeoutMs` is 15000.  `FleetShimPrefilterTests` run every rule of the real secret guard through the shim in both JSON spellings, check the words against the guard's own tables, and require the whole corpus to be denied end to end.
- **UNVERIFIED:** what Muse does when the hook times out or crashes (a timeout that blocks shell calls under load, or one that fails open and switches the guards off then; the binary's strings did not settle it); whether `MUSE_EXPERIMENTAL_PLUGINS=1` is required; whether a running session picks up a newly approved plugin; whether `muse plugins install`, which copies the bundle into its cache, keeps the absolute command (the cached manifest points back at the stable wrapper either way); that Muse names its only shell tool `bash`; that Muse puts `tool_name` before any nested object in its payload (if not, every call reaches the shim: correct, slower); that the Muse binary itself writes nothing outside the throwaway directory during `validate`; and, in a container, that the guard finds a home under an empty environment (it falls back to the passwd entry).

### MiniMax Code

MiniMax Code loads `~/.minimax/plugins/fleet-guard/` as a real directory, using the supported `.claude-plugin/plugin.json` compatibility manifest and `hooks/hooks.json`.  This is an MM-owned hook bundle, not a copy of Claude's settings or home-local hooks.  One `PreToolUse` group matches `Bash|Read|Write|Edit|bash|read|write|edit` and runs `<stable>/minimax-guard-hook` with a 10-second timeout.  The stable shim starts `fleet_lanes.minimax_guard_hook` under isolated Python, without a shell prefilter; file payloads always reach the adapter.

The adapter shares the lane-checkout and shell secret policies, adds a bare `ps` denial, and checks compatible file-tool paths without interpreting file content as commands.  Read, write and edit of secret-bearing paths or registered integration trees under `~/Code` are denied, including symlink targets and secret-named symlinks.  Writes and edits of review lanes are denied.  Another seat's lane is denied for writes when the trusted seat differs; a lane write without trusted identity is refused; launcher identity wins over ambient identity, and no identity is inferred from the engine or destination directory.

- **Runtime contract:** the installed MiniMax Code release `0.6.2-repair-51995-21372` maps native `bash`, `read`, `write`, `edit` to compatible `Bash`, `Read`, `Write`, `Edit`; read/write `path` becomes absolute `file_path`, and edit retains `file_path`.  The adapter also matches unchanged lowercase native names because extra native arguments can prevent compatible remapping; on every file tool both `path` and `file_path` are checked, and a relative path with no usable `cwd` is resolved against `HOME` instead of skipping the file policy.  Its compatible hook runner accepts `hookSpecificOutput.permissionDecision = deny`.  Source evidence is in the installed package's `chunk-UI7OHI22.js` (`ape`, `zEn`, `FSn`, `jSn`) and `chunk-PUFWXRWS.js` (compatible plugin loader).  These are runtime implementation details, not a promise that every version has the same contract.
- **Identity boundary:** MiniMax's hook subprocess allowlist excludes `AGENT_SEAT`, `AGENT_LAUNCHER` and `AGENT_LAUNCH_SEAT`.  Normal installed MM hooks therefore refuse writes to seat-owned lanes when trusted identity is missing.  A static plugin seat would not attest a launched session, so launcher-bound authorization remains unsupported until the host carries per-launch identity.  The adapter never pins MM or replaces a launcher assignment.  No SessionStart seat hook is installed.
- **Coverage boundary:** the matcher covers the four tools above.  Other tools, MCP calls and arbitrary shell programs that read files are not comprehensively covered.  Shared shell policy catches its established incident patterns; this is not a filesystem sandbox.
- **Install:** `apply minimax` (alias `mm`, included in `all`) installs the stable tools first, runs shell and file probes, and swaps an owned plugin directory atomically.  Foreign contents, symlinks and changed ownership markers are refused.  A missing `~/.minimax` skips the platform.  Stable source digests and installer isolation remain in force.
- **Verify:** `verify minimax` reads the command back from the written bundle and checks shell lane/secret denials, file secret/integration denials and safe allows, including the restricted hook environment.  These subprocess probes do not prove MiniMax dispatch; `fires` remains `SKIP`.  Crash, timeout, malformed input and unsupported tools can fail open.  An unavailable or malformed `SECRET_FILE_PATTERNS` table refuses file operations not already denied by an independent path policy.  A registry read failure refuses file operations not already denied by an independent path policy and identifies the unavailable `FLEET_APPS_JSON` source.  Installation and verification refuse an unavailable or malformed required registry, and staging also probes a module-backed `.env` denial.  A new real session and loaded-plugin dispatch remain separate proof steps; no live configuration or running session is changed by tests.
- Tests: `fleet_lanes.tests.test_minimax_guard_hook` and `fleet_lanes.tests.test_install_tools.MinimaxBundleTests`.

### Copilot CLI

Copilot CLI reads hooks from JSON files under `~/.copilot/hooks/`.  The fleet's guard is a NEW file this tool owns, `~/.copilot/hooks/fleet-lane-guard.json` (version 1, one `hooks.PreToolUse` group, matcher `Bash`, one `type: command` hook running `<stable>/fleet-guard-hook --format claude` by absolute path, `timeoutSec` 5).  The PascalCase `PreToolUse` event gets Claude matcher semantics and reports the Claude tool name, which is exactly the payload the fleet hook already parses, so the deny is the Claude shape.

- **It never edits `~/.copilot/settings.json`**, which the fleet does not own, and never touches an `agent-hook-otlp.json` in that directory (none is there now:  the Oct 20, 2026 deletion of `~/.copilot` took it, and the Oct 21 re-wiring did not restore it).  A file at our path that holds any hook this tool did not write, any other event or any other top-level key is refused and left alone.
- **A command `preToolUse` hook is fail-closed**: exit 2, a crash or any non-zero exit denies the call, and only a timeout fails open.
- **`verify copilot`** takes the command back out of the written file, checks the file against what this version writes, and runs it with the lane deny, the secret deny and the allow payloads under the login and minimal PATH.  It ends with `SKIP fires`: whether Copilot CLI fires a `preToolUse` hook at all is UNVERIFIED on this Mac.  With the CLI installed and `~/.copilot` present, the written file is checked (on Wed, Oct 21, 2026 that was 9 PASS and 1 SKIP, the SKIP being the live hook proof).  If `~/.copilot` is missing, the target has no config to check, and the outcome is the missing-directory one described below.
- **OWNER-GATED.**  There is no live `preToolUse` record on this Mac: a `postToolUse` firing from `agent-hook-otlp.json` was recorded before the Oct 20, 2026 deletion of `~/.copilot`, that evidence is no longer on disk, and the file was not restored with the Oct 21, 2026 re-wiring (see `docs/MAC-LOCAL-PROCESSES.md`).  So the live proof needs Jay to start a new Copilot session and ask it for a process listing, which must be refused.  The install is written and proven to deny by `verify`; the live firing is not claimed.
- **Copilot CLI stays a fleet platform**:  owner Jay reversed the 2026-10-20 retirement the same day.  If the Copilot configuration directory does not exist, the target has no config to write until Copilot has created it (the CLI alone does not:  `copilot --version` leaves `~/.copilot` absent, so on Wed, Oct 21, 2026 the directory was made by hand, mode 0700, before the installers would write).  Nothing else recreates the directory.  **Only when `~/.copilot` is missing:  named explicitly, `plan copilot` and `verify copilot` report a FAIL** (reached through `all` it is a SKIP), because asking to verify a platform that is not there is a mistake worth reporting rather than a healthy state.  With `~/.copilot` present they report the installed result above.
- **UNVERIFIED:** that Copilot CLI fires a `PreToolUse` hook from this file at all; that a running Copilot session picks up the new file (start a new session); the exact wildcard matching Copilot applies to a `matcher`.
- Tests: `fleet_lanes.tests.test_muster_guards.CopilotTests`.

### OpenCode

OpenCode's config schema has no `hooks` key, so the guard is a PLUGIN.  `apply tools` writes `opencode-plugin/index.ts` into the stable copy, and `apply opencode` lists that plugin DIRECTORY by ABSOLUTE path as a string entry in the `plugins` array of `~/.config/opencode/opencode.json`.  OpenCode V2 rejects a plugin FILE entry ("configured plugin path must be a directory") and does not run V1 plugin implementations, so the directory entry with an `index.ts` module is the only shape that loads; `~/.config/opencode/plugin/` directory auto-discovery is not relied on.  OpenCode is the OPENCODE seat, so this guard is for a real seat.

- **The plugin is `Plugin.define({ id, setup })` and implements `tool.execute.before`**, the V2 hook slot.  It keys on the presence of a command in the tool args rather than on a tool name, because OpenCode's shell tool name is not confirmed.  A throw from `tool.execute.before` is the deny path: OpenCode surfaces the error to the model and does not run the tool.  There is no `permission.ask` slot in V2.
- **Empty guard output is an ALLOW only when the hook exited cleanly** (code 0, no signal); a crashed, signalled or timed-out guard is a denial, never a silent allow.
- **It runs the stable `fleet-guard-hook`** with a Claude-shaped payload and reads the Claude deny shape back, so there is one guard implementation and one set of rules.  The plugin derives the hook path from its own file, so the text holds no home path and no credential.
- **Nothing else in `opencode.json` is changed or printed.**  The file holds a literal Bearer token under `mcp.verde.headers.Authorization` and a `password` key in `service.json`, so the merge touches only the `plugins` array (moving a fleet entry out of the legacy `plugin` key), a backup is owner-only (0600) whatever the file's mode, and the plugin text holds no credential: `verify opencode` fails if it ever names one.
- **`verify opencode`** checks the plugin file against what this version writes, that it holds both hook names and the deny, that it holds no credential, that the `plugin` array lists the absolute path exactly once, and it runs the guard with the lane deny, the secret deny and the allow payloads.  It ends with `SKIP fires`.
- **UNVERIFIED:** whether `permission.ask` is reached for a shell call that `tool.execute.before` judged, and whether it sees the command text (its input is a `Permission` with type, pattern, title and metadata, and the plugin judges whatever command text those carry); whether a throw from `tool.execute.before` denies; that OpenCode loads an absolute-path local plugin entry in a live session (the live proof is a new OpenCode session asked for a process listing, which must be refused, and only Jay can run it).
- Tests: `fleet_lanes.tests.test_muster_guards.OpenCodeTests`.

### fx

fx has NO hook, plugin or extension surface: `fx --help` names none, and the published docs index has no hooks page.  The only enforcement surface is static `permission` rules in `~/.fx/settings.json` (wildcard rules, last match wins), and the live file has `permission_mode` `yolo` with no `permission` block at all.

- **So fx stays RED (UNSUPPORTED) with the blocker recorded.**  `plan fx` prints the blocker and a PREPARED compensating control, and writes nothing: `apply fx` is a skip and `verify fx` is a FAIL that names the blocker.  A static rule cannot resolve `d=$(mktemp -d)`, cannot read a repo's fleet status and cannot see through a `bash -c` layer, all of which `guard.py` handles, so a static list is a weaker control and is labelled as one.
- **The compensating control is reviewable, not applied.**  It moves `permission_mode` off `yolo` to `ask` and adds a `permission.bash` deny list over the provable temp-destination shapes (a `git clone`, a `gh repo clone`, a `git worktree add` or `move`, a `git archive` into `/tmp`, `/private/tmp`, `/var/tmp` or `/private/var/tmp`), with a top-level `permission."*"` rule of `ask` so the deny list is the only thing that blocks and nothing else is silently allowed.  Jay merges it into `~/.fx/settings.json` by hand after approving it; this tool never writes `~/.fx`.
- **UNVERIFIED:** the exact wildcard matching fx applies to a `permission.bash` pattern (the shape is taken from the published permissions documentation example); that the control, once applied, is ever exercised.  fx stays RED until Jay approves the control or approves an exception.
- Tests: `fleet_lanes.tests.test_muster_guards.FxTests`.

### Fleet Hook

`<stable>/fleet-guard-hook` runs the lane guard and then the secret guard (`scripts/hooks/secret-guard-pretooluse.py`, copied into the stable copy as `fleet_lanes/secret_guard.py`; any `ps`, a dump of a secret-bearing file, an authorization value in arguments, a byte-dump of a key variable), and the first deny wins.  It takes the same `--format FMT` as `lane-guard-hook` plus `--format kimi` (the plain exit-2 contract: reason on stderr, exit code 2, nothing on stdout; `--exit2` does the same for any format).  Its shell prefilter is the lane guard's trigger words plus `SECRET_WORDS` (bearer, authorization, pgrep, .secrets, credentials, client_info, .env, id_rsa, .pem, .p12, key, token, secret, passw, dsn) and a `ps` followed by white space, so an ordinary shell call is answered in `sh` without starting Python (see the tests below), and it fails open: a crash in either guard allows, and never switches off the other.  Muse Code's plugin wrapper, Clutch and Kimi Code use it; Claude Code and Codex keep `lane-guard-hook` because they already run the secret guard as a hook of their own.  Every probe of these platforms (`apply` and `verify`) runs a lane deny, a secret deny (`ps aux`) and an allow.

- **`clutch`** is one marked block (`# fleet:begin hooks-fleet-guards`) in `~/.clutch/dsh/cordis.patch.yml`, the home-level patch that applies to every profile: an `- insert:` entry that mounts `@deepseek-ai/dsh-hooks-claude-code` with `configPath` set to `<stable>/clutch/hooks.json` (a fleet-only file, matcher `bash`; never `~/.claude/settings.json`, which carries Claude-only hooks).  Measured with the pinned engine on 2026-10-10: an empty or comment-only patch makes `dsh --dump-config` exit 1, so the file this tool creates always holds a block, and removing the last block removes the file.  Before it writes, `apply clutch` refuses unless the plugin package resolves under the real `~/.clutch/dsh/profiles/node_modules` (a link to a path that is gone does not; the engine repairs links only when it boots, so restart `clutch-web` first), then runs `dsh --profile P --dump-config` for every profile on a scratch copy of `~/.clutch/dsh/profiles` (never the credentials or settings) and refuses unless each one's merged config names the plugin.  That is a merge-and-parse check: `--dump-config` never resolves a package.  The scratch run keeps the caller's `PATH` and PREPENDS the directories that hold `node` (`/opt/homebrew/bin`, `/usr/local/bin`), because the pinned `dsh` is a `#!/usr/bin/env node` symlink: a caller whose `PATH` has no node (a launchd job, a hook, a bot shell) otherwise failed the run with `env: node: No such file or directory` (exit 127).  `--dsh PATH` names another engine binary, `--no-dsh-check` skips both.  A profile with `patchReload: live` (clutch-web's `web`) watches the patch file, so the write reaches the running engine at once; a `startup` profile reads it at its next start.  `apply` prints each profile's mode, and `verify clutch` runs the same two checks on the file as it is.
- **`kimi`** is one marked `[[hooks]]` block (`event = "PreToolUse"`, `matcher = "Bash"`, the fleet hook with `--format kimi`, `timeout = 5`; Kimi allows exactly those four fields) appended to `~/.kimi-code/config.toml`.  The new file must parse as TOML (when `tomllib` exists) and, when `kimi` is on `PATH`, `kimi doctor` must accept a scratch copy of it in a scratch home with auto-install off.
- A bare `verify` reads an installed but unwired `clutch` or `kimi` (no block yet) as one `SKIP not wired`, because both are opt-in like `muse`; naming the platform (`verify clutch`) turns that lone SKIP into a FAIL.
- **UNVERIFIED** for both: whether the tool then fires the hook.  `verify` says so (`SKIP fires`); the live proof is to ask a new session to run `ps` and expect a refusal.

### muse-seat

`<stable>/muse-seat` starts Muse Code as seat `MC`: it sets `AGENT_SEAT=MC` and `AGENT_TAG=MC` (replacing any value already set) and `exec`s the real `muse` that `command -v muse` finds, passing every argument through.  It exits 127 with a message on stderr when there is no executable `muse` on `PATH`; when the `muse` it finds is the wrapper itself (a symlink named `muse` earlier on `PATH`, compared with `-ef`); and when muse-seat already ran in this chain of processes, which it marks by exporting `_MUSE_SEAT_ACTIVE=1` before the `exec`.  The last one catches a wrapper SCRIPT named `muse` that starts muse-seat again (with or without `exec`), which `-ef` cannot see because it is a different file; without it muse-seat would exec itself forever.  The cost: inside a Muse session that muse-seat started the variable is still set, so muse-seat refuses there too.  `AGENT_SEAT` is already `MC` in that session, so start another one with `muse` itself.  `plan muse` documents it.  Nothing puts it on `PATH` or aliases `muse`; run it by its path, or add `alias muse=<stable>/muse-seat` to your interactive shell's rc file yourself (safe: a script does not expand aliases, so muse-seat still finds the real `muse`).  A symlink or a script named `muse` earlier on `PATH` is refused, as above.

## install_rules

Puts the Lane Map rule text into each platform's own rules file as one marker block, so every platform reads it every session.

```
cd scripts
python3 -m fleet_lanes.install_rules plan   [PLATFORM ...] [--home PATH] [--create] [--i-own-this-file]
python3 -m fleet_lanes.install_rules apply  PLATFORM ...    [--home PATH] [--create] [--i-own-this-file] [--dry-run]
python3 -m fleet_lanes.install_rules verify PLATFORM ...    [--home PATH]
```

A bare invocation, or one whose first argument is a flag, runs `plan` for all platforms.  `apply` and `verify` need at least one platform name and never default to all.  `apply --dry-run` is `plan`.  Platforms (path under `--home`; aliases `ag` and `gemini` for antigravity, `mm` for minimax, `grok-build` for grok):

| Platform | File | Text |
|---|---|---|
| `claude` | `.claude/CLAUDE.md` | Full.  This is the owner's own file: `plan` and `apply` need `--i-own-this-file` when it is named (a default `plan` skips it without reading it). |
| `home-agents` | `AGENTS.md` (the home folder) | Full.  Also the owner's own file (`--i-own-this-file`).  Tools that load project instructions read it, and it carried a hand-copied block that no row refreshed until layout v2. |
| `codex` | `.codex/AGENTS.md` | Full.  Warns above 30720 bytes and refuses a result above 32768 (limits chosen by the tool's author, not checked against Codex itself). |
| `fx` | `.fx/AGENTS.md` | Full |
| `grok` | `.grok/GROK.md` | Full; covers Grok and Grok Build |
| `antigravity` | `.gemini/config/AGENTS.md` | Full |
| `minimax` | `.minimax/memory/user.md` | The 3-line minimal text.  The file is already about 20 KB and goes into every prompt; it looks agent-managed and may be regenerated (UNVERIFIED), so `verify minimax` is how to catch a lost block. |
| `cursor` | `.cursor/rules/fleet-lane-map.mdc` | Full, under `alwaysApply: true` frontmatter.  Whether Cursor loads user-level `.mdc` rules is UNVERIFIED. |
| `clutch` | `.clutch/dsh/AGENTS.md` | Fleet Basics (FLEET-PROTOCOL pointer, seat identity with the CLUTCH default and a launcher's seat first, coordination, landing, secrets, writing) then the Lane Map.  Refuses a result over 65536 bytes: the engine caps its whole instruction baseline there and drops the broadest file first. |
| `opencode` | `.config/opencode/AGENTS.md` | A Seat Identity with the fixed default seat OPENCODE, then the Lane Map.  OpenCode is the OPENCODE seat (owner, #440):  the default is for a session Jay opens in a terminal, a launcher's seat or a seat Jay names comes first, and a headless run or a session inside Conductor has no default.  It is not the whole Fleet Basics:  the live file is a whole copy of `~/AGENTS.md` (`docs/OPENCODE-ONBOARDING-PROMPT.md`), which already holds coordination, landing, secrets and writing, so this tool does not repeat them.  The row is the owner's (#438); `.config` is shared, so `.config/opencode` has to exist before `--create` writes.  OpenCode also walks AGENTS.md up to `$HOME`, so a session under `$HOME` may load this file and `~/AGENTS.md` (the same Lane Map twice; read from the binary, not run with a model), and `plan` says so. |
| `kimi` | `.kimi-code/AGENTS.md` | Fleet Basics with NO default seat (take a launcher's seat or ask Jay), then the Lane Map |
| `copilot` | `.copilot/copilot-instructions.md` | The same no-default text; the file name is from GitHub's documentation and is UNVERIFIED on this Mac (`plan` and `apply` print that) |
| `conductor`, `muse-code` | none | Unsupported on purpose: `plan` lists each with the reason, `apply` exits 2, `verify` exits 1.  Conductor has no instruction file of its own (its Prompts setting is the owner's), and Muse Code reads `~/.claude/CLAUDE.md` as its only user-level source, so the `claude` platform covers it. |

The block is `<!-- fleet-lane-map:begin v1 -->` to `<!-- fleet-lane-map:end -->`.  It is appended after one blank line when absent, replaced in place when present and different, and left alone when identical; bytes outside it are never touched (CRLF files stay CRLF).  Markers must be one balanced pair; anything else, or a block version above 1, is refused.  Creating a file needs `--create`, the platform's root folder (`.cursor`, `.fx` and so on) must already exist, and a new file gets mode 0644 with no backup.

Guards (exit 2, nothing written): a symlink that does not resolve to a regular file inside `--home` (it is never read, so its target cannot leak into plan output), a dangling or looping link, a non-regular, unreadable, NUL-containing or non-UTF-8 file, malformed markers, and a read-only file or unwritable folder when a change is actually needed.  A symlink that resolves inside `--home` is written through.  Before any change to an existing file, a backup `<real target>.bak-lane-map-YYYYmmdd-HHMMSS` (UTC, original mode, read back and compared, `-2` suffix on a same-second collision) is made; the write goes through a temp file, fsync, `os.replace`, and a read-back check, and a failed write removes its backup.  A second apply reports UNCHANGED and writes no backup.  Platforms are independent: `apply` keeps going after a refusal and the exit is the worst code.

Exit codes: 0 ok; 1 `verify` found no current block, or a write failed; 2 refused by a guard (`plan` exits 2 only when an owner file is NAMED without `--i-own-this-file`; other predicted refusals are informational); 64 usage error.

`plan` prints, per platform: the file, size, symlink and block state, what `apply` would do, WARNING and "would be REFUSED" lines, a unified diff, then `CONTRADICTIONS (report only; never edited automatically)`.  The scanner reports lines that state the old flat-lane rule (`flat-lane`), name a pre-v2 lane folder such as `~/apps/lanes/trading/`, `~/apps/lanes/<prefix>/`, `_managed` or `_review` (`old-lane-layout`; a repo's exact folder name such as `BotFleet` is never reported), advise a checkout in a temp directory (`tmp-checkout`), or mention an `agent/<name>` branch prefix (`legacy-agent-prefix-mention`, no ruling made), and never edits them.  Everything printed from a file goes through a redactor for URL credentials and common token shapes.

The rule text is in `rules/` (`lane-map.full.md`, `lane-map.minimal.md`, `lane-map.cursor.mdc`, and for the Fleet Basics platforms `fleet-basics.clutch.md`, `fleet-basics.nodefault.md` plus the shared `fleet-intro.md` and `fleet-common.md`; and `seat-identity.opencode.md` for OpenCode, a fixed-seat Seat Identity without the rest of the Fleet Basics).  The Fleet Basics block still uses the `fleet-lane-map` markers, so one family of markers is all `find_block` knows.  It says the branch is "your branch prefix plus the slug", not `<seat>/<slug>`, because the Antigravity folder is `antigravity-x` and its branch is `ag/x`.

Known limits: the atomic rename does not preserve extended attributes, ownership or hard links, and there is a small window between the byte compare and the rename (the backup still holds the pre-install bytes).

## install_mcp

Registers the fleet's two stdio MCP servers in each harness's own config, in its own format (OpenCode `mcp.servers`, Kimi `mcpServers`, Copilot `mcpServers` with `tools: ["*"]`, Muse `mcpServers` with `mode: optional`, MiniMax, fx, and for Clutch a marked `dsh-mcp-client` block in the cordis patch).

```
cd scripts
python3 -m fleet_lanes.install_mcp plan   [TARGET ...] [--with-simple-chat] [--home H]
python3 -m fleet_lanes.install_mcp apply  TARGET ...   [--with-simple-chat] [--dry-run]
python3 -m fleet_lanes.install_mcp verify [TARGET ...] [--with-simple-chat] [--probe]
python3 -m fleet_lanes.install_mcp remove TARGET ...   [--with-simple-chat]
```

Minimal by default: `fleet-recall` only.  `--with-simple-chat` adds `simple-chat mcp --default-seat <SEAT>` for the targets with a fixed seat (opencode OPENCODE, muse MC, minimax MM, fx FX, clutch CLUTCH) and never for a tool with no default seat (Kimi, Copilot CLI).  OpenCode is a target in code but is left out of the live batch:  a session OpenCode runs inside Conductor has no default seat and shares the same global file, so `simple-chat` is registered there only after the owner answers that question (#440).  No token is written and no `AGENT_SEAT` is pinned.  A tool whose home folder is missing is skipped, an entry with the same command is left as it is (`present`), a different command is `skipped-foreign`, a file that is not a JSON object is left alone, and an env block that pins `AGENT_SEAT` is reported.  One `*.bak-fleet-mcp-<stamp>` backup per file (always mode 0600, whatever the original's mode, so a config with a plaintext token never gets a second readable copy; `plan` and `apply` warn, without printing the value, when the file holds a literal Authorization, Bearer, token, key, password or secret string), an atomic replace, and a second apply writes nothing.  `verify --probe` starts each registered command and sends MCP `initialize` and `tools/list`, nothing else, in an environment without the seat, launcher and Zulip credential variables.  A `simple-chat` entry is probed only from a session that IS its seat (the registration's `--default-seat` reads that seat's credential file and acts as its bot), and is a SKIP from any other session.  `scripts/install-fleet-rag.sh` calls it for the six non-Clutch targets.  Clutch's patch is checked before it is written: `apply clutch --with-simple-chat` refuses unless `@deepseek-ai/dsh-mcp-client` resolves under the real `~/.clutch/dsh/profiles/node_modules` (restart `clutch-web` first if a link dangles), then runs the pinned `dsh --profile P --dump-config` for every profile on a scratch copy of the profiles (`--dsh PATH`, `--no-dsh-check`).  `--dump-config` is a merge-and-parse check, not a plugin load, so `install_tools verify clutch` on the final file comes next.  A profile with `patchReload: live` (clutch-web's `web`) watches the patch, so the write reaches the running engine at once; `apply` prints each profile's mode.  Tests: `fleet_lanes.tests.test_install_mcp`.

## Rollout order for live installs

Each step is owner-approved, read the `plan` first, and nothing here is installed until that happens (nothing under `~/apps/lane-tools`, no `~/apps/lane`, no hook entry and no rules block exists yet).

1. `install_tools apply tools`, then `verify tools`.  This creates `~/apps/lane-tools` and `~/apps/lane`.  It comes first because the rule text tells every agent to run `~/apps/lane`, and a rules block pointing at a missing command is worse than none.
2. `install_tools apply <platform>` for each platform, then `verify`.  Codex needs one `/hooks` trust, and Claude needs a new session.  Muse Code needs the owner actions that `plan muse` prints (`muse plugins install ... --scope user`, then `muse plugins approve fleet-lane-guard`, then a new session); `apply tools` has already written the bundle.
3. `install_rules apply <platform>` for each platform (`--i-own-this-file` for `claude` and `home-agents`, `--create` for a file that does not exist).
3a. The harnesses added Sat, Oct 10, 2026, in this order, each after reading its `plan`: `install_rules apply clutch kimi copilot --create` (a tool that is not installed is skipped); `install_tools apply clutch kimi` (it runs `apply tools` first, then checks the cordis patch: the plugin link must resolve and the pinned engine must merge it, and `kimi doctor` must accept the Kimi file, before writing; `--no-dsh-check` and `--no-kimi-check` skip them); `install_mcp apply muse minimax fx kimi opencode copilot` and `install_mcp apply muse minimax fx clutch --with-simple-chat` (rotate the Muse GitHub token first: the apply rewrites `settings.json`; its backup is owner-only now, but the token should not sit in plaintext at all); `install_tools verify clutch kimi muse`; then the owner's own steps (before the Clutch applies: `pm2 restart clutch-web` if a plugin link under `~/.clutch/dsh/profiles/node_modules` dangles; after: `muse plugins install` and `approve`, a new Kimi session, and the `ps` probe in each).  OpenCode's `AGENTS.md` is not in this list: its live file already carries hand-written Fleet Basics and a hand-written seat preface, so `install_rules apply opencode` (the OPENCODE Seat Identity block, then the Lane Map) is a separate, owner-confirmed step.  A live `install-fleet-skills.py` re-renders every seat's skill home, not only Clutch's.
4. After every merge that changes the package, `apply tools` again to refresh the stable copy.  After any change to a platform config, `verify` again.

Changes to the live cleaners (`disk-janitor`, `mac-auto-cleanup`) are a separate step; see `docs/protocols/lane-map.md` § Cleaners.

## Tests

```
cd scripts
python3 -m unittest fleet_lanes.tests.test_layout -v
python3 -m unittest fleet_lanes.tests.test_guard -v
python3 -m unittest fleet_lanes.tests.test_doctor -v
python3 -m unittest fleet_lanes.tests.test_lane -v
python3 -m unittest fleet_lanes.tests.test_retire -v
python3 -m unittest fleet_lanes.tests.test_install_rules -v
python3 -m unittest fleet_lanes.tests.test_install_tools -v
python3 -m unittest fleet_lanes.tests.test_install_mcp -v
python3 -m unittest fleet_lanes.tests.test_fleet_guard_hook -v
python3 -m unittest fleet_lanes.tests.test_dsh_check -v
```

The tests build real throwaway git repos with a hermetic git config, fake `gh` and `lsof`, and never scan the real `/tmp`.  They take minutes rather than seconds because every report or lane creation runs real git, and `/usr/bin/git` on macOS costs 60 to 100 milliseconds per call, more on a loaded machine (`test_lane` took 100 to 230 seconds at a load average near 900).  The installers are tested against temp fake homes only; never run `apply` against the real home from a test or a demo.  `test_guard` has a timing assertion on the allowed-command path that can fail once on a very loaded machine; rerun before treating it as a regression.
