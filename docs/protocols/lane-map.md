# Lane Map — where every checkout lives (binding — all seats, all apps, all platforms; owner 2026-10-07)

> Layout and seat-token rulings by the owner, 2026-10-07.  Extends the 2026-09-25 `~/Code` integration-tree rule in `AGENT-SYNC.md` (that rule is unchanged) and replaces its flat `~/apps/<app>-<seat>` lane naming for NEW lanes.  Full text lives here; `AGENT-SYNC.md` carries a short pointer.  Board `a7dfde0e`.

> **Rollout status (2026-10-07).**  In this PR: the registry (`fleet-apps.json`: Monet, Renoir and Harness retired, Clutch added), `scripts/fleet_lanes/` (layout, `doctor`, the tmp guard hook, 256 tests, CI step).  Next PR: `lane new`, `install_rules.py`, the `AGENT-SYNC.md` pointer, onboarding script and doc fixes.  Owner-approved live installs (global rules files, hooks, Claude desktop and Codex worktree roots) come after that.  Until `lane new` exists, create lanes by hand to match the table below.

## The Rule

Every git checkout on this Mac has exactly one of four homes.  There is no fifth, and a temp directory is never one of them.

| Home | Path | Used for | Branch | Lifetime |
|---|---|---|---|---|
| Integration tree | `~/Code/<App>` | The human's review base.  Never edited by agents; stays on `origin/main`.  No new folder may ever be added at the top of `~/Code`. | `main` | Permanent |
| Lane | `~/apps/lanes/<prefix>/<seat>-<slug>` | One agent task.  A linked worktree of the integration tree. | `<seat prefix>/<slug>` | Until the PR merges, then retire |
| Review checkout | `~/apps/lanes/_review/<prefix>/pr-<n>[-<seat>]` | Read-only verification of someone else's PR or branch.  A linked worktree, never a full clone. | Detached at the PR head | 7 days at most |
| Harness-managed | `~/apps/lanes/_managed/<tool>/...` and the tool-fixed locations in the platform table below | Worktrees a tool creates for itself.  We can relocate some tools, not rename what they create. | Tool-chosen | The tool's own cleanup, plus the janitor |

`<prefix>` is `worktreePrefix` from `fleet-apps.json` (the registry is the single source of truth: `trading`, `congress`, `usage`, `cts`, `dealdex`, `fleet`, `personal`, `autorotate`, `contactlogo`, `botfleet`, `hoghunter`, `fleet-ops`, `clutch`).  An app with lanes but no registry row (codecaps, FleetLink, Simple-With-Us, Kodus-Config) must be added to the registry before it gets lanes.

`<seat>` is the WHOLE name, `worktreeSuffix` from `fleet-apps.json`: `claude`, `codex`, `cursor`, `grok`, `grok-build`, `clutch`, `fx`, `antigravity`, `minimax`, `muse-code`, `muse-assist` (`monet`, `renoir` and `harness` are retired as of 2026-10-07: accepted on old lanes, never used for new ones).  Owner ruling 2026-10-07: folders, branches, and anything on disk use whole names; short tags (`AG`, `MM`) are for chat posts (Zulip, formerly Slack) and board attribution only.  The short tokens `ag` and `mm` already on ~80 existing lanes are accepted as aliases and are never reported as errors, and no lane is renamed.  Branch prefixes are unchanged by this document (`minimax/`, `ag/`, `claude/` and so on stay as the registry has them).

`<slug>` is lowercase kebab, 1 to 40 characters, derived from the task (a board id or a short purpose).  No seat, app, or date inside the slug.

## Not Checkouts: What Temp Is Still For

Scratch is fine in `/tmp` and in the harness scratchpad (`/private/tmp/claude-<uid>/...`): throwaway test repos made with `git init`, self-cleaning `mktemp -d` scripts, downloads, build output.  The ban is on a checkout of a FLEET repo (a clone, a worktree, a tarball extraction, an `init` plus `fetch`) in `/tmp`, `/private/tmp`, `/var/tmp`, `/var/folders/*/*/T`, or `$TMPDIR`.  Cloning a third-party repo to read it is allowed.

Non-git artifacts that must outlive a session (bundles, patches, DerivedData, tarballs) go in `~/apps/scratch/<seat>/<topic>/`, not in `/tmp`, so they show up in one place.

## Creating A Lane

```bash
~/apps/lane new <app> <slug>                 # a lane for your seat (AGENT_SEAT must be set)
~/apps/lane new <app> --review --pr <n>      # a read-only review checkout
~/apps/lane ls                               # every checkout on the Mac, with status
```

`lane new` resolves the prefix from the registry and the seat from `AGENT_SEAT`, creates the branch off `origin/main` in the integration tree's worktree list, and refuses any path outside the map.  It is easier than doing it wrong, which is the point.  Doing it by hand is allowed only if the result matches the table above.

## Existing Lanes

No lane is moved.  Lanes are short-lived (4 of 142 were older than 14 days on 2026-10-07), so legacy flat lanes (`~/apps/<prefix>-<seat>[-<slug>]`) retire in place and the layout is fully in effect within about two weeks.  Legacy flat lanes are reported as `LANE_FLAT_LEGACY` and allowed during the transition.  A flat lane created AFTER the cutover date is reported as a violation.  Bot lanes (BotFleet `kody`, `fixer`, `designer`, `compiler`) have no seat row in the registry; they are reported as `BOT-LANE` until the owner registers a scheme.

## Platform Table

Facts verified 2026-10-07.  `UNVERIFIED` means the discovery sweep could not confirm it.

| Platform | Where it creates worktrees today | Can the root move? | Deny hook surface | Rules file to carry this rule |
|---|---|---|---|---|
| Claude Code CLI | `~/Code/<App>/.claude/worktrees/<name>` (`claude -w`, subagent and workflow worktrees) | No setting.  A global `WorktreeCreate` hook could, but it has no matcher and it breaks the desktop warm pool and leaks branches, so it is rejected.  The path is sanctioned as harness-managed and tracked. | `PreToolUse` Bash hook in `~/.claude/settings.json` | `~/.claude/CLAUDE.md` |
| Claude desktop (Code tab) | Same default | Yes, owner setting: Settings, Claude Code, Worktree location = `~/apps/lanes/_managed/claude-desktop` (`chillingSlothLocation.customPath`).  Layout becomes `<custom>/<Repo>/<slug>-<hex>`; branch prefix is `ccBranchPrefix`.  UNTESTED: run the relocation test in the discovery notes first. | Same hook as the CLI | Same |
| Codex | `~/.codex/worktrees/<slug>/<Repo>` | Yes: `[desktop] git-worktree-root` in `~/.codex/config.toml` (CLI `--worktree` behavior UNVERIFIED).  Target `~/apps/lanes/_managed/codex`. | `PreToolUse` Bash in `~/.codex/hooks.json` (re-trust by hash after editing) | `~/.codex/AGENTS.md` |
| Cursor | `~/.cursor/worktrees` (hard-coded, empty today; Cursor opens lanes directly) | No | `beforeShellExecution` in `~/.cursor/hooks.json` with `failClosed` | `~/.cursor/rules/*.mdc` (loading UNVERIFIED) plus each repo's `AGENTS.md` |
| Grok, Grok Build | `~/.grok/worktrees/<repo>/worktree-<id>` (empty today) | No documented key | `~/.grok/hooks/*.json`; also reads `~/.claude/settings.json` hooks (deny output format differs, UNVERIFIED) | `~/.grok/GROK.md` |
| Antigravity (AG) | `~/.gemini/antigravity/worktrees`, `~/.ag/worktrees` (empty); scratch clones in `~/.gemini/antigravity/scratch` | No | `~/.gemini/config/hooks.json`, matcher `run_command` (CLI; IDE UNVERIFIED) | `~/.gemini/config/AGENTS.md` |
| MiniMax (MM) | None of its own (it has made `/tmp` clones by typing commands) | n/a | None | `~/.minimax/memory/user.md` |
| Fx | None of its own | n/a | None found | `~/.fx/AGENTS.md` |
| Muse Code | Undocumented (UNVERIFIED) | UNVERIFIED | `~/.config/muse/settings.json` hooks (deny format undocumented) | Project `AGENTS.md` |
| BotFleet bots | `~/.botfleet/workspaces/<uuid>` (nested clones); leases at `~/.botfleet/worktrees` (feature off) | Only `OMB_DATA_DIR` | `server/command-guard.ts` (backstop, BotFleet repo) | `bots/_shared.md` |
| Cloud agents (Cursor cloud, Codex cloud, Muse Assist VM) | Remote VMs | Local rules cannot apply | Project-level hooks only | Each repo's `AGENTS.md` |

## Enforcement

Rules alone were never enough, so four layers back this one:

1. **Rule text** in each platform's own rules file (table above), installed by `scripts/fleet_lanes/install_rules.py`: dry-run by default, backup first, idempotent marker block.
2. **A deny hook** (`scripts/fleet_lanes/lane_guard_hook.py`) on every platform that has a hook surface.  It denies a checkout of a fleet repo in a temp directory and tells the agent the exact lane path to use instead.  It fails open on anything it cannot parse.  Platforms with no hook (MiniMax, Fx) rely on layers 1, 3 and 4.
3. **`lane doctor`** (`scripts/fleet_lanes/doctor.py`): report-only.  It finds EVERY checkout (registered worktrees, plus a bounded scan for full clones and hidden ones), says where it lives, and classifies it `SAFE-TO-REMOVE`, `NEEDS-REVIEW`, `ACTIVE` or `UNKNOWN` with reasons.  `dropped_ball` marks work that exists only on this disk.  `--strict` exits non-zero on any checkout in a temp directory.  It never deletes, moves, fetches, or rewrites an index.
4. **Cleaners read the doctor.**  `disk-janitor` and `mac-auto-cleanup` act only on checkouts the doctor lists as `SAFE-TO-REMOVE`; removal is `git worktree remove`, never `rm -rf`.  Anything outside the map is reported, never removed on a guess.

## What This Fixes

- 19 checkouts and about 12 GB in `/private/tmp` (8.5 GB of that is the owner's own Photos video and is never touched by any tool).  They came from agents typing ad hoc commands, not from any platform default.
- 459 entries in a flat `~/apps`; 20% of lanes matched the old naming rule.
- Harness-default folders (`~/Code/<App>/.claude/worktrees`, `~/.codex/worktrees`) that no rule covered.
- Work that existed only on this disk: see board `c4329937`.

## Related

`AGENT-SYNC.md` (`~/Code` rule, pointer to this file) · `scripts/fleet_lanes/README.md` · `docs/ONBOARDING-NEW-APP.md` · `docs/ONBOARDING-NEW-AGENT.md` · board `a7dfde0e`.
