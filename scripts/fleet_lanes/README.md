# fleet_lanes doctor

One report-only command that finds every git checkout on this Mac, wherever it hides, says where it lives, and says whether removing it could lose work.  It answers "where did the ball get dropped" and makes disk cleaning safe.  It never deletes, moves, fetches or modifies anything.

Python 3.11+, standard library only.  Layout rules (where checkouts may live, what lanes are called) come from `layout.py` in this package.  `layout.py` itself also imports on Python 3.9 (it carries a small `StrEnum` fallback), because the temp-dir guard hook imports it under whatever `python3` its PATH finds, and the hook treats every exception as an allow.

## Usage

```
cd scripts
python3 -m fleet_lanes.doctor                      # summary tables, then what needs attention
python3 -m fleet_lanes.doctor --json --no-gh       # machine-readable, no network
python3 -m fleet_lanes.doctor --write /path/report.json --gh-limit 1000
python3 -m fleet_lanes.doctor --strict             # exit 2 if a forbidden or unsanctioned checkout exists
```

| Option | Meaning |
|---|---|
| `--json` | Print the JSON report instead of the text report. |
| `--write PATH` | Also write the JSON report to PATH (atomic: temp file, fsync, rename). |
| `--sizes` | Measure each checkout with `du -sk -x` (30 second cap each).  Off by default because it is slow. |
| `--deep` | Also scan `~/Documents`, `~/Desktop`, `~/Downloads` to depth 4 (iCloud-backed and permission-prompting, so off by default). |
| `--no-gh` | Skip PR lookups.  PR state becomes UNKNOWN for fleet checkouts off the default branch, and those checkouts report UNKNOWN instead of SAFE-TO-REMOVE (an open PR cannot be ruled out). |
| `--gh-limit N` | PRs fetched per repo, default 500, maximum 1000.  A list that fills the limit cannot prove a branch has no PR, so unmatched checkouts report UNKNOWN instead of NONE. |
| `--only-class CLASS` | Show only one location class.  `--strict` still judges the whole machine. |
| `--strict` | Exit 2 if any FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL or UNSANCTIONED checkout exists.  Plugin caches and third-party clones do not count toward UNSANCTIONED. |
| `--home PATH`, `--tmp-root PATH` | Point the sweep at a fake home and fake temp root (tests). |

Exit codes: 0 normally, 2 for `--strict` hits, 64 for a usage error (so a bad flag cannot be mistaken for a strict hit).  The layout mode comes from `FLEET_LAYOUT` (`nested` by default, or `flat`).

## What it scans

Discovery is the union of two methods, deduplicated by real path (so `~/Code/Socratic.Trade`, a symlink to `~/Code/Socratic-Trade`, is one checkout).  Depth 0 is the scan root, and a checkout is found when its own depth is at most the limit.

1. `git worktree list --porcelain` in every discovered full clone and every parent repo that a `.git` file points to.  This finds registered worktrees that sit outside every scan root.
2. A bounded scan for `.git` entries (file or directory) that skips `node_modules`, `.Trash` and `Library`, never follows symlinks, and does not look inside a checkout it has already found.

| Root | Depth |
|---|---|
| `/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/tmp`, `$TMPDIR` | 4 |
| `~/apps` | 2 (every top-level entry, plus one level into non-git folders such as `mkt/`) |
| `~/apps/lanes` | 4 |
| `~/Code` | 1, plus each `~/Code/<App>/.claude/worktrees` at 1 |
| `~/.codex/worktrees`, `~/.cursor/worktrees`, `~/.ag/worktrees` | 3 |
| `~/.grok`, `~/.fx` | 4 |
| `~/.botfleet`, `~/.gemini/antigravity` | 5 |
| `~/.buzz` | 3 |
| `~` | 3 (skipping Documents, Desktop, Downloads, Pictures, Movies, Music, Public, Applications, and the tool caches `.cache`, `.npm`, `.cargo`, `.rustup`, `.nvm`, `.pyenv`, `.rbenv`, `.volta`, `.pnpm-store`, `.yarn`, `.bun`, `.gradle`, `.m2`) |
| `~/Documents`, `~/Desktop`, `~/Downloads` | 4, only with `--deep` |

A registered worktree whose directory is gone is reported as PRUNABLE, not as a checkout.  A `.git` file whose gitdir target is missing is an ORPHAN.  Each checkout carries `found_by` so you can see which method found it.

## Reading the output

**Location class** (from `layout.classify_location`): INTEGRATION_TREE, LANE_NESTED, LANE_FLAT_LEGACY, LANE_FLAT, REVIEW, MANAGED, FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL, UNSANCTIONED.  The Claude harness scratchpad under `/private/tmp/claude-<uid>` is still FORBIDDEN_TMP here, because a checkout inside it is a checkout in a temp directory.  Only the temp guard exempts it.

**Safety class**, in precedence order.  This says whether removing the checkout could lose work.  It does not say the location is disposable (an integration tree with no lanes can read SAFE-TO-REMOVE).

| Class | Meaning |
|---|---|
| ACTIVE | A live process has it as its cwd, or it is `CLAUDE_PROJECT_DIR` or the doctor's own cwd.  A process in a nested checkout counts only for the deepest one.  Other reasons are still listed. |
| NEEDS-REVIEW | At least one positive reason removal could lose work.  Every reason is listed in `safety_reasons`. |
| UNKNOWN | No known risk, but a needed fact could not be read: an orphan, any git call that timed out or failed (including the clone-only counts of other branches and stashes, and a `git worktree list` on a parent), git resolving the directory to some other repository, an lsof failure, or a PR state that gh could not give. |
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
- an "only copy" of some commits, described next

"Only copy" means commits that nothing else references: a detached HEAD that no branch, remote branch or tag contains, other local branches in a full clone that hold commits not on any remote, or a stash.  This is how the doctor reads "the only checkout of a branch".  A stash lives in the parent repo, so it is counted on the full clone and not on its linked worktrees.

**PR state** comes from one bulk `gh pr list --repo OWNER/NAME --state all --limit N --json number,state,headRefName,headRefOid,mergedAt,closedAt` per repo, matched by branch name and head sha.  Fleet repos squash-merge and delete the branch, so ancestry says nothing.

| State | Meaning |
|---|---|
| OPEN | An open PR matches the branch or HEAD. |
| MERGED | HEAD equals, or is contained in, a merged PR head.  The commits live on GitHub, so "unpushed" and "only copy" reasons are suppressed before reasons are added up. |
| CLOSED | HEAD equals the head of a closed PR that was not merged.  The commits are on GitHub but were never merged. |
| BEYOND-MERGED | A merged PR exists for the branch but HEAD has commits past its head ("commits beyond merged PR"), or the merged head is not in the local repo to compare. |
| NONE | The lookup worked and nothing matches. |
| UNKNOWN | `gh` failed, was skipped, or the list filled the limit with no match.  Never read as NONE.  A checkout in this state reports UNKNOWN (reason `PR state unknown: ...`) unless a known risk already makes it NEEDS-REVIEW. |
| N/A | Default branch, no commits, no GitHub remote, or a third-party repo. |

**dropped_ball** is `true` for NEEDS-REVIEW checkouts that have dirty files or unpushed commits (HEAD's, or other local branches in a full clone that hosts no worktrees; not covered by a merged PR) and no open PR.  It is `null` when the PR state is UNKNOWN, because "no open PR" cannot be asserted; the summary counts those separately as `dropped_ball_unknown`.  It is `false` otherwise.

`unpushed` is measured against remote-tracking refs already on disk.  The doctor never fetches, so a stale ref can overstate or understate it.

**Creating tool** is a guess from the path first, then the lane's seat token, then the branch prefix.  `creating_tool_basis` says which.  A Claude worktree under `.claude/worktrees` is called claude-desktop only when its name ends in `-<6 hex>` like the desktop app's, and claude-cli otherwise, so treat that split as a hint.

**Tool caches** (plugin and marketplace clones under `~/.grok`, `~/.codex/memories` and similar) are kept in `checkouts` and the summary with `tool_cache: true`, but they do not raise UNSANCTIONED anomalies or `--strict`.  Third-party clones (a remote owned by someone other than the registry owner) are treated the same way for UNSANCTIONED only.  FORBIDDEN_TMP and FORBIDDEN_CODE_TOPLEVEL always apply.

## JSON schema (schema 1)

```
{
  "schema": 1,
  "generated_at": "2026-10-07T16:15:00+00:00",   # UTC, from an injected clock
  "host": "...", "layout_mode": "nested|flat", "home": "...", "only_class": null,
  "lsof": "ok|unavailable", "gh": "ok|partial|failed|skipped", "warnings": [...],   # partial/failed name the repos
  "scan": [{"label", "path", "depth", "found"}],
  "checkouts": [ <checkout> ],
  "anomalies": [{"type", "path", "realpath", "detail"}],   # most urgent first
  "summary": {
    "total", "total_found", "strict_violations", "dropped_ball", "dropped_ball_unknown",
    "forbidden_tmp", "tool_cache",
    "by_location_class", "by_safety", "by_creating_tool", "by_repo", "by_kind", "by_pr_state",
    "anomalies_by_type"
  }
}
```

A checkout holds: `path`, `realpath`, `kind` (LINKED-WORKTREE, FULL-CLONE, ORPHAN), `parent_repo`, `owner_repo`, `remote_url` (credentials redacted), `scope` (fleet, third-party, unknown), `location_class`, `lane_root`, `creating_tool`, `creating_tool_basis`, `tool_cache`, `found_by`, `branch`, `detached_sha`, `head_sha`, `last_commit`, `lane_age_days` (since the directory was created), `idle_days` (since the last commit or HEAD move), `dirty_tracked`, `dirty_untracked`, `ignored_local_count`, `ignored_local`, `ignored_regenerable`, `unpushed`, `unpushed_all_branches`, `has_stash`, `upstream`, `upstream_state` (none, in-sync, ahead, behind, diverged, gone), `pr_state`, `pr_number`, `pr_basis`, `registered`, `registered_worktrees`, `locked`, `cwd_procs` (null when lsof failed), `active`, `janitor_keep` (a `.janitor-keep` marker exists; NEEDS-REVIEW), `size_mb` (only with `--sizes`), `name_verdict` (null outside lane locations), `name_reasons`, `branch_verdict`, `safety`, `safety_reasons`, `dropped_ball`, `read_errors`.

Anomaly types, most urgent first: FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL, ORPHAN, PRUNABLE, UNSANCTIONED, FULL-CLONE-IN-LANE, NAME-DRIFT, NON-LANE.  ALIAS-ONLY names (`ag`, `mm`) are allowed and are not anomalies.

## Safety invariants

- Every subprocess starts in `run_cmd(argv, cwd, timeout)`, which refuses (raises `CommandRefused` before any process exists) anything off this allowlist: `git status | rev-parse | rev-list | log | for-each-ref | branch --show-current | config --get | remote get-url | worktree list`, `gh pr list`, `lsof -d cwd -Fpcn`, `du -sk -x`.  Flags are checked too, so `git log --output=FILE`, `git branch -D`, `git config --unset` and `git worktree prune` all fail.  `git status` accepts `--ignored` (it only adds listing lines) but not `--ignored=matching`, and `git rev-list` accepts `--remotes=NAME` only for a plain remote name.
- `git diff`, `fetch`, `gc`, `checkout`, `reset`, `stash`, `clean`, `worktree add/remove/prune/move` and `rm` are not on the list.  `git diff` is excluded on purpose: it refreshes and rewrites `.git/index` in other seats' checkouts.
- git runs with `--no-optional-locks -c core.fsmonitor=false` added by `run_cmd` (callers cannot pass global options), `GIT_OPTIONAL_LOCKS=0`, `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0` and `GIT_NO_LAZY_FETCH=1`.  `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR` and `GIT_OBJECT_DIRECTORY` are stripped from the inherited environment, so a hook that exported one cannot redirect `git status` at another repo's index.
- Timeouts are enforced per call (git and lsof 20 seconds, du 30, gh 60 because it lists over the network) and the whole process group is killed.  A repo that times out or cannot be read becomes UNKNOWN and the sweep continues.
- Credentials in remote URLs and in stored stderr are replaced (`user:token@host` becomes `REDACTED@host`), including passwords that contain `/` or `@` and a token in the user slot of an scp-style remote (`TOKEN@github.com:o/r.git`).  The ssh user `git` is kept.
- The test suite proves the refusals, then runs the doctor over real temp repos with a stale index and compares a hash and mtime of every file before and after, with a control that shows plain `git status` does rewrite that index.

## Known limits

- A checkout nested inside another checkout is not reported separately unless git registers it (for example `/private/tmp/um-relay/repo` inside `/private/tmp/um-relay`).
- A checkout deeper than a scan allows (see the table) is invisible.  The `~` scan stops at depth 3 and skips the tool caches above, so a clone four levels under `~` is not found.
- A directory whose `.git` git does not accept (empty, or no HEAD) is reported UNKNOWN, with the enclosing repository's path in `read_errors`, not as that repository's state.  One `git rev-parse --show-toplevel --git-dir` per checkout checks that git resolves this very directory and the git dir its `.git` names, and a rejected clone is not asked for its worktree list (git would answer for the enclosing repository).
- Ignored files are read with a second `git status --ignored` call per checkout, so a timeout there leaves the dirty counts intact and only adds an `ignored:` read error.  Detection is a name list, not a judgment.  A regenerable-looking name (`build`, `dist`) hides everything under it, and a path git folds into one entry (an untracked directory that is wholly ignored shows as `data/`) is listed as that directory.
- Plugin caches outside the scan roots (`~/.claude/plugins`, `~/.cursor/plugins`, `~/.codex/plugins`) are not scanned.
- "Only the checkout of a branch" is interpreted as the "only copy" cases above.  Changing that is a one-place edit in `assess`.
- `gh` listing is capped at 1000 PRs.  Repos with more than that report UNKNOWN for branches the list cannot match.

## Tests

```
cd scripts && python3 -m unittest fleet_lanes.tests.test_doctor -v
```

The tests build real throwaway git repos with a hermetic git config, fake `gh` and `lsof`, and never scan the real `/tmp`.  They take a minute or two because every report runs real git against every repo, and `/usr/bin/git` on macOS costs 60 to 100 milliseconds per call.
