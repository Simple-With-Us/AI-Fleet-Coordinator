# Lane Map — where every checkout lives (binding — all seats, all apps, all platforms; owner 2026-10-07)

> Layout and seat-token rulings by the owner, 2026-10-07.  Extends the 2026-09-25 `~/Code` integration-tree rule in `AGENT-SYNC.md` (that rule is unchanged) and replaces its flat `~/apps/<app>-<seat>` lane naming for NEW lanes.  Full text lives here; `AGENT-SYNC.md` carries a short pointer.  Board `a7dfde0e`.

> **Rollout status (2026-10-07).**  Merged in PR #355: the registry (`fleet-apps.json`: Monet, Renoir and Harness retired, Clutch added), `scripts/fleet_lanes/` (layout, the report-only `doctor`, the temp-checkout guard hook) and its CI step.  In the second PR: `lane new` (lanes and review checkouts), `install_tools.py` (a stable copy of the tools, hook wiring, and a `verify` that proves the hook denies), `install_rules.py` (the rule text in each platform's rules file), the doctor fixes (fresh-lane rule, tool caches, `cleaner_candidates`, schema 2), this document, the `AGENT-SYNC.md` pointer and seat table, and the onboarding script and doc fixes.  Still pending, each needing the owner's approval: the live installs (`install_tools apply`, then `install_rules apply`, then the Claude desktop and Codex worktree roots), the changes to the live `disk-janitor` and `mac-auto-cleanup` (see Cleaners), and nested-lane support in `otel-lane-tag.sh`.  Until `~/apps/lane` is installed it does not exist: run `lane` from a checkout of this repo (`cd scripts && python3 -m fleet_lanes.lane new <app> <slug>`) or create the lane by hand so that it matches the table below.  The third PR adds Muse Code (`muse`) and Muse Assist: the `muse-repo` managed location, the `muse-seat` wrapper and the plugin hook (both built in `scripts/fleet_lanes/install_tools.py`), the doctor's `muse-code` inference, and the owner checklist and assistant card in `docs/MUSE-ONBOARDING.md`.

## The Rule

Every git checkout on this Mac has exactly one of four homes.  There is no fifth, and a temp directory is never one of them.

| Home | Path | Used for | Branch | Lifetime |
|---|---|---|---|---|
| Integration tree | `~/Code/<App>` | The human's review base.  Never edited by agents; stays on `origin/main`.  No new folder may ever be added at the top of `~/Code`. | `main` | Permanent |
| Lane | `~/apps/lanes/<prefix>/<seat>-<slug>` | One agent task.  A linked worktree of the integration tree. | `<seat prefix>/<slug>` | Until the PR merges, then retire |
| Review checkout | `~/apps/lanes/_review/<prefix>/pr-<n>[-<seat>]` | Read-only verification of someone else's PR or branch.  A linked worktree, never a full clone. | Detached at the PR head | 7 days at most |
| Harness-managed | `~/apps/lanes/_managed/<tool>/...` and the tool-fixed locations in the platform table below | Worktrees a tool creates for itself.  We can relocate some tools, not rename what they create. | Tool-chosen | The tool's own cleanup, plus the janitor |

`<prefix>` is `worktreePrefix` from `fleet-apps.json` (the registry is the single source of truth: `trading`, `congress`, `usage`, `cts`, `dealdex`, `fleet`, `personal`, `autorotate`, `contactlogo`, `botfleet`, `hoghunter`, `fleet-ops`, `clutch`).  An app with lanes but no registry row (codecaps, FleetLink, Simple-With-Us, Kodus-Config) must be added to the registry before it gets lanes.

`<seat>` is the WHOLE name, `worktreeSuffix` from `fleet-apps.json`: `claude`, `codex`, `cursor`, `grok`, `grok-build`, `clutch`, `fx`, `antigravity`, `minimax`, `muse-code`.  `muse-assist` is also a registry seat, but that cloud assistant has no checkout on this Mac, so no lane is ever made for it.  `monet`, `renoir` and `harness` are retired as of 2026-10-07, `deepseek` since 2026-09-19 and `kimi` since 2026-08-21: accepted on old lanes, never used for new ones.  Owner ruling 2026-10-07: LANE FOLDER NAMES use whole names; short tags (`AG`, `MM`) are for chat posts (Zulip, formerly Slack) and board attribution only.  The ruling covers folder names, not branches: branch prefixes are whatever the registry lists (`minimax/`, `ag/`, `claude/`) and are not changed here.  The short tokens `ag` and `mm` already on ~80 existing lanes are accepted as aliases and are never reported as errors, and no lane is renamed.

`<slug>` is lowercase kebab, 1 to 40 characters, derived from the task (a board id or a short purpose).  No seat, app, or date inside the slug.

## Not Checkouts: What Temp Is Still For

Scratch is fine in `/tmp` and in the harness scratchpad (`/private/tmp/claude-<uid>/...`): throwaway test repos made with `git init`, self-cleaning `mktemp -d` scripts, downloads, build output.  The ban is on a checkout of a FLEET repo (a clone, a worktree, a tarball extraction, an `init` plus `fetch`) in `/tmp`, `/private/tmp`, `/var/tmp`, `/var/folders/*/*/T`, or `$TMPDIR`.  Cloning a third-party repo to read it is allowed.

Non-git artifacts that must outlive a session (bundles, patches, DerivedData, tarballs) go in `~/apps/scratch/<seat>/<topic>/`, not in `/tmp`, so they show up in one place.

## Creating A Lane

```bash
export AGENT_SEAT=CLAUDE                      # your seat tag; lane refuses if it is unset or unknown
~/apps/lane new <app> <slug>                  # a lane for your seat; prints its path
~/apps/lane new <app> --review --pr <n>       # a read-only, detached checkout of a PR head
~/apps/lane path <app> <slug>                 # the path a lane would get; changes nothing
~/apps/lane ls                                # every checkout on the Mac, with status (the doctor)
d=$(~/apps/lane new DealDex fix-login) && cd "$d"   # never cd "$(...)" alone: a refusal prints nothing and cd "" stays put
```

`lane new` resolves the prefix from the registry and the seat ONLY from `AGENT_SEAT` (an uppercase registry tag such as `CLAUDE`, `AG`, `MM` or `CLUTCH`).  It never infers a seat from a login, a path or a branch; if the variable is unset or unknown, or names a retired seat, it refuses and you ask the owner.  The folder is `<seat>-<slug>` with the whole seat name, and the branch is the seat's first registry branch prefix plus the slug (`claude/fix-login`, `minimax/fix-login`, folder `antigravity-fix-login` with branch `ag/fix-login`).  It fetches `origin/main`, creates the branch off it with `git worktree add` from the integration tree, writes a small `lane.json` manifest inside that worktree's private git folder, and refuses any path outside the map.  Re-running it for a lane that already exists prints the path and changes nothing.  `--base`, `--reuse-branch`, `--purpose`, `--board`, `--dry-run` and `--json` are described in `scripts/fleet_lanes/README.md`; exit codes are 0 ok, 64 usage error or refusal, 69 git or gh failed.

`~/apps/lane` is the shim that `install_tools.py` writes; it runs the stable copy in `~/apps/lane-tools`.  A brand-new app or seat is refused until its row is in the registry that shim reads, so the first lane for a new app comes from a checkout of this repo with `FLEET_APPS_JSON` pointing at its `fleet-apps.json`, and `install_tools apply tools` refreshes the stable copy after the row merges.  Doing it by hand is allowed only if the result matches the table above.  `lane` never creates a `.janitor-keep` file.

## Existing Lanes

No lane is moved.  Lanes are short-lived (4 of 142 were older than 14 days on 2026-10-07), so legacy flat lanes (`~/apps/<prefix>-<seat>[-<slug>]`) retire in place and the layout is fully in effect within about two weeks.  Legacy flat lanes are reported as `LANE_FLAT_LEGACY` and allowed during the transition.  A flat lane created AFTER the cutover date is reported as a violation.  Bot lanes (BotFleet `kody`, `fixer`, `designer`, `compiler`) have no seat row in the registry; they are reported as `BOT-LANE` until the owner registers a scheme.

## Platform Table

Facts verified 2026-10-07.  `UNVERIFIED` means the discovery sweep could not confirm it.

| Platform | Where it creates worktrees today | Can the root move? | Deny hook surface | Rules file to carry this rule |
|---|---|---|---|---|
| Claude Code CLI | `~/Code/<App>/.claude/worktrees/<name>` (`claude -w`, subagent and workflow worktrees) | No setting.  A global `WorktreeCreate` hook could, but it has no matcher and it breaks the desktop warm pool and leaks branches, so it is rejected.  The path is sanctioned as harness-managed and tracked. | `PreToolUse` Bash hook in `~/.claude/settings.json` | `~/.claude/CLAUDE.md` |
| Claude desktop (Code tab) | Same default | Yes, owner setting: Settings, Claude Code, Worktree location = `~/apps/lanes/_managed/claude-desktop` (`chillingSlothLocation.customPath`).  Layout becomes `<custom>/<Repo>/<slug>-<hex>`; branch prefix is `ccBranchPrefix`.  UNTESTED: run the relocation test in the discovery notes first. | Same hook as the CLI | Same |
| Codex | `~/.codex/worktrees/<slug>/<Repo>` | Yes: `[desktop] git-worktree-root` in `~/.codex/config.toml` (CLI `--worktree` behavior UNVERIFIED).  Target `~/apps/lanes/_managed/codex`. | `PreToolUse` Bash in `~/.codex/hooks.json` (re-trust by hash after editing) | `~/.codex/AGENTS.md` |
| Cursor | `~/.cursor/worktrees` (hard-coded, empty today; Cursor opens lanes directly) | No | `beforeShellExecution` in `~/.cursor/hooks.json`; the installed entry is `failClosed: false`, so a broken install fails open and only `install_tools verify` proves it works | `~/.cursor/rules/*.mdc` (loading UNVERIFIED) plus each repo's `AGENTS.md` |
| Grok, Grok Build | `~/.grok/worktrees/<repo>/worktree-<id>` (empty today) | No documented key | `~/.grok/hooks/*.json`; also reads `~/.claude/settings.json` hooks (deny output format differs, UNVERIFIED) | `~/.grok/GROK.md` |
| Antigravity (AG) | `~/.gemini/antigravity/worktrees`, `~/.ag/worktrees` (empty); scratch clones in `~/.gemini/antigravity/scratch` | No | `~/.gemini/config/hooks.json`, matcher `run_command` (CLI; IDE UNVERIFIED) | `~/.gemini/config/AGENTS.md` |
| MiniMax (MM) | None of its own (it has made `/tmp` clones by typing commands) | n/a | None | `~/.minimax/memory/user.md` |
| Fx | None of its own | n/a | None found | `~/.fx/AGENTS.md` |
| Muse Code (`muse`, seat `MC`) | `~/Code/<App>/.muse/worktrees/<name>` for `muse -w` (evidence is strings in the binary, so the branch name and exact layout are UNVERIFIED).  Sanctioned as harness-managed (`muse-repo`) and tracked, but do not use `-w`: start `muse` inside a lane made by `lane new`.  An experimental `agents` plugin (gated off) would create `<repo-parent>/<repo>-threads`, a new folder beside `~/Code/<App>`; keep it off. | No.  No root setting was found (UNVERIFIED), so the answer is to not use the feature. | A user-scope plugin hook: `muse plugins install <dir> --scope user`, then `muse plugins approve fleet-lane-guard`, then a new session.  `~/.config/muse/settings.json` has no hooks key today, and the plugin is the surface the fleet uses.  The deny shape is claude-like and UNVERIFIED until the deny probe in `docs/MUSE-ONBOARDING.md` passes. | `~/.claude/CLAUDE.md`, which Muse Code loads as a user-scope fallback (every past session did), so the Lane Map block installed there reaches it.  A native `~/.config/muse/AGENTS.md` probably exists as a user rules file (UNVERIFIED), but creating it likely suppresses that fallback and drops the whole fleet protocol: do not create it unless it carries the full protocol.  `AGENT_SEAT=MC` comes from the `muse-seat` wrapper, because a past session echoed an empty seat. |
| Muse Assist (`MA`) | Nothing on this Mac.  It is a Meta Muse cloud assistant with its own Debian VM, started from the Mac and iOS apps, and it cannot see local files.  No lane is ever made for it. | n/a | None: no hooks (third-party sources, UNVERIFIED).  It can run CLIs on its VM, so the rule is stated in its own memory files instead. | Its Soul.md, Identity.md and Memory.md in the mobile app (no custom-instructions box; UNVERIFIED).  The owner pastes the card in `docs/MUSE-ONBOARDING.md`.  Credentials go through its secure store, never chat. |
| BotFleet bots | `~/.botfleet/workspaces/<uuid>` (nested clones); leases at `~/.botfleet/worktrees` (feature off) | Only `OMB_DATA_DIR` | `server/command-guard.ts` (backstop, BotFleet repo) | `bots/_shared.md` |
| Cloud agents (Cursor cloud, Codex cloud; Muse Assist has its own row) | Remote VMs | Local rules cannot apply | Project-level hooks only | Each repo's `AGENTS.md` |

## Enforcement

Rules alone were never enough, so four layers back this one:

1. **Rule text** in each platform's own rules file (table above), installed by `scripts/fleet_lanes/install_rules.py`: `plan` is read-only and the default, `apply` names its platforms, a backup comes first, and the marker block is idempotent.  The rule text tells every agent to run `~/apps/lane`, so the tools are installed before the rules.
2. **A deny hook** (`scripts/fleet_lanes/lane_guard_hook.py`, installed by `install_tools.py` from the stable copy in `~/apps/lane-tools`) on every platform that has a hook surface.  It denies a checkout of a fleet repo in a temp directory and tells the agent the exact `~/apps/lane new` command to run instead.  It fails open on anything it cannot parse.  `install_tools verify` is the proof that an installed hook denies and allows correctly; the presence of an entry is not.  Platforms with no hook installed (MiniMax and Fx) rely on layers 1, 3 and 4, and so does Muse Code until the owner installs and approves its plugin hook (`docs/MUSE-ONBOARDING.md`): that approval is a step only the owner can do, so `install_tools` cannot finish it.
3. **`lane ls` / `lane doctor`** (`scripts/fleet_lanes/doctor.py`): report-only.  It finds EVERY checkout (registered worktrees, plus a bounded scan for full clones and hidden ones), says where it lives, and classifies it `SAFE-TO-REMOVE`, `NEEDS-REVIEW`, `ACTIVE`, `UNKNOWN` or `TOOL-CACHE` with reasons.  A lane younger than 7 days (by age or idle time) with no merged PR reads `NEEDS-REVIEW` ("fresh lane").  `dropped_ball` marks work that exists only on this disk.  `--strict` exits non-zero on any checkout in a temp directory.  `--cleaner-list` prints the only paths a cleaner may remove.  It never deletes, moves, fetches, or rewrites an index.
4. **Cleaners read the doctor.**  `disk-janitor` and `mac-auto-cleanup` act only on `cleaner_candidates` from a fresh doctor report, never on `SAFE-TO-REMOVE` alone; removal is `git worktree remove`, never `rm -rf`.  Anything outside the map is reported, never removed on a guess.  The contract is in the next section.

## Cleaners

Anything that removes a checkout (the disk janitor, `mac-auto-cleanup`, a housekeeper bot, an agent told to "clean up the lanes") follows one contract:

1. **Act only on `cleaner_candidates`** from a fresh doctor report (`lane ls --cleaner-list`, or the `cleaner_candidates` array of a `--json` report).  A candidate is a registered linked worktree in a lane, review or managed location, clean, fully pushed or covered by a merged PR, with merged-PR evidence, no process using it, no read errors, and both its age and its idle time at least 7 days.  A full clone is never a candidate.
2. **Never act on `SAFE-TO-REMOVE` alone.**  Before the fresh-lane rule, a lane created an hour ago with no commits yet read `SAFE-TO-REMOVE`.  The rule and the candidate list now both rule that out, but the label says nothing about whether anyone is about to use a lane, so a cleaner never gates on it.
3. **Check the report before trusting it:** `schema` is at least 2, `generated_at` is recent, `lsof` is `"ok"`, and `gh_repos[<owner/repo>]` is `"ok"` for the repo (a failed or skipped PR lookup means no candidates, which is the safe direction).
4. **Remove with `git worktree remove`**, never `rm -rf`, and never anything outside the map.
5. **Never create a `.janitor-keep` file** as protection.  It makes the doctor say `NEEDS-REVIEW` forever, and the dependency reaper below ignores it.

What the live `disk-janitor` does today (read-only trace, 2026-10-07; this contract has not reached it yet):

- It retires a clean lane whose branch is 0 commits ahead of `origin/main` after 7 idle days by file mtime, nested and flat paths alike.  A fresh lane is not retired on its next tick.
- Its dependency reaper deletes `node_modules`, `.next` and `.turbo` from lanes 4 hours after the last file change whenever free disk is under 65 GB (always, as of today).  It fails open on a scan timeout and ignores `.janitor-keep`.  An idle lane therefore loses its installed dependencies overnight and needs `npm ci` again; that costs time but no commits.
- `mac-auto-cleanup` only globs flat `~/apps/*`, so it does not see nested lanes.

The plan, pending the owner's approval and not yet built: the janitor's retire step takes its list from `cleaner_candidates` instead of its own check; its scan-timeout path fails closed (skip, not delete); the dependency reaper stops using a 4-hour window on a lane that has unmerged work (the new window is the owner's call when that change is made); and `mac-auto-cleanup` reads the same list instead of globbing.

## What This Fixes

- 19 checkouts and about 12 GB in `/private/tmp` (8.5 GB of that is the owner's own Photos video and is never touched by any tool).  They came from agents typing ad hoc commands, not from any platform default.
- 459 entries in a flat `~/apps`; 20% of lanes matched the old naming rule.
- Harness-default folders (`~/Code/<App>/.claude/worktrees`, `~/.codex/worktrees`) that no rule covered.
- Work that existed only on this disk: see board `c4329937`.

## Related

`AGENT-SYNC.md` (`~/Code` rule, pointer to this file, seat table) · `scripts/fleet_lanes/README.md` (every command and option) · `scripts/fleet_lanes/rules/` (the rule text the installer writes) · `docs/MUSE-ONBOARDING.md` (Muse Code checklist and the Muse Assist card) · `docs/ONBOARDING-NEW-APP.md` · `docs/ONBOARDING-NEW-AGENT.md` · board `a7dfde0e`.
