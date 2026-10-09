# Lane Map — where every checkout lives (binding — all seats, all apps, all platforms; layout v2, owner 2026-10-09)

> **Owner decision 2026-10-09.**  Jay approved layout v2 ("Go with this layout?", "yes"): every agent copy of a repo, whoever made it, lives in `~/apps/lanes/<Repo>/`, where `<Repo>` is the repo's folder name under `~/Code`.  This replaces the 2026-10-07 `~/apps/lanes/<prefix>/` folders, `_managed` and `_review`.  Everything else from 2026-10-07 stands: whole-name seat tokens, the temp-directory ban, the deny hooks, the doctor and the cleaner contract.  Extends the 2026-09-25 `~/Code` integration-tree rule in `AGENT-SYNC.md` (unchanged).  Full text lives here; `AGENT-SYNC.md` carries a short pointer.  Board `a7dfde0e`.

> **Rollout status (2026-10-09).**  This change ships the lane tool and doctor for v2 (`scripts/fleet_lanes/`), the rule text, this document, the migration plan (`docs/protocols/lanes-v2-migration.md`) and its script (`scripts/lanes-v2-migrate.py`, a dry run unless given `--apply`).  Nothing on the Mac moves until the owner approves the live steps in that plan: refresh the stable tools copy (`install_tools apply tools`), the rules blocks (`install_rules apply`), the tool settings below, then the migration.  Until the stable copy is refreshed, `~/apps/lane` still makes the old `~/apps/lanes/<prefix>/` paths: run `lane` from a checkout of this repo (`cd scripts && python3 -m fleet_lanes.lane new <app> <slug>`).

## The Rule

Every git checkout on this Mac has exactly one of these homes.  A temp directory is never one of them, and nothing new goes inside `~/Code/<Repo>`.

```
~/Code/<Repo>                          the human tree (never edited by agents, stays on origin/main)
~/apps/lanes/
  <Repo>/
    <seat>-<slug>                      a lane, made by `lane new`
    <slug>-<hex>                       a Claude desktop worktree (the app names it)
    review-pr-<n>[-<seat>]             a read-only check of someone else's PR
  _codex/<slug>/<Repo>                 a Codex desktop worktree (Codex nests the other way round)
```

| Home | Path | Used for | Branch | Lifetime |
|---|---|---|---|---|
| Integration tree | `~/Code/<Repo>` | The human's review base.  Never edited by agents.  No new folder may be added at the top of `~/Code`, and no worktree inside it (no `<repo>/.claude/worktrees`). | `main` | Permanent |
| Lane | `~/apps/lanes/<Repo>/<seat>-<slug>` | One agent task.  A linked worktree of the integration tree. | `<seat prefix>/<slug>` | Until the PR merges, then retire |
| Review checkout | `~/apps/lanes/<Repo>/review-pr-<n>` | Read-only verification of someone else's PR.  A linked worktree, never a full clone.  A second seat checking the same PR gets `review-pr-<n>-<seat>`. | Detached at the PR head | 7 days at most |
| Claude desktop worktree | `~/apps/lanes/<Repo>/<slug>-<hex>` | A worktree the Claude desktop app makes for itself.  The app picks the name (`<slug>` plus six hex digits). | `claude/<slug>-<hex>` | The app's own cleanup, plus the janitor |
| Codex desktop worktree | `~/apps/lanes/_codex/<slug>/<Repo>` | A worktree Codex makes for itself. | Tool-chosen | Codex's own cleanup, plus the janitor |
| Other harness-managed | The tool-fixed locations in the platform table below | Worktrees a tool creates for itself that cannot be relocated. | Tool-chosen | The tool's own cleanup, plus the janitor |

`<Repo>` is the folder name of the human tree under `~/Code`, exactly as it is spelled there: `Socratic-Trade`, `Congress.Trade`, `Usage-Monitor`, `congress-trading-shared`, `DealDex`, `AI-Fleet-Coordinator`, `Personal-Site`, `Autorotate`, `ContactLogo`, `BotFleet`, `HogHunter`, `Fleet-OPS`, `Clutch`, `CodeCaps`.  It is `codeDir` in `fleet-apps.json`, which is the single source of truth.  `~/Code/Socratic.Trade` is a symlink to `Socratic-Trade` and is never a folder name.  A repo with no tree under `~/Code` uses its GitHub repo name instead (`fleetlink-legacy`, `Kodus-Config`).  An app with lanes but no registry row (FleetLink, Simple-With-Us) must be added to the registry before `lane new` serves it.  macOS is case-insensitive, so `botfleet` and `BotFleet` are one folder there; write the exact spelling anyway, because CI is case-sensitive and `lane ls` reports the other spelling as legacy.

`worktreePrefix` (`trading`, `fleet`, `botfleet`, ...) is no longer a folder name.  It stays in the registry for the legacy flat lane names (`~/apps/<prefix>-<seat>-<slug>`), as an accepted name for `<app>` in `lane new`, and in telemetry tags.

`<seat>` is the WHOLE name, `worktreeSuffix` from `fleet-apps.json`: `claude`, `codex`, `cursor`, `grok`, `grok-build`, `clutch`, `fx`, `antigravity`, `minimax`, `muse-code`.  (The 2026-10-09 proposal the owner approved listed the Muse Code token as `mc`; this page keeps the registry's `muse-code` under "keep today's token rules", so a Muse Code lane is `muse-code-<slug>`.  **Owner question:** confirm `muse-code`, or say that `mc` was meant literally.)  `muse-assist` is also a registry seat, but that cloud assistant has no checkout on this Mac, so no lane is ever made for it.  `monet`, `renoir` and `harness` are retired as of 2026-10-07, `deepseek` since 2026-09-19 and `kimi` since 2026-08-21: accepted on old lanes, never used for new ones.  Owner ruling 2026-10-07: LANE FOLDER NAMES use whole names; short tags (`AG`, `MM`) are for chat posts (Zulip) and board attribution only.  The ruling covers folder names, not branches: branch prefixes are whatever the registry lists (`minimax/`, `ag/`, `claude/`).  The short tokens `ag` and `mm` already on existing lanes are accepted as aliases and never reported as errors.

`<slug>` is lowercase kebab, 1 to 40 characters, derived from the task (a board id or a short purpose).  No seat, app, or date inside the slug.

One edge: a Claude desktop folder is told from a lane by its name alone, as `<slug>-<6 hex>` with no seat word in front.  A desktop slug that itself starts with a seat word (`claude-foo-e380b8`) reads as a lane, and a lane slug ending in six hex digits stays a lane because the seat word comes first.

## Not Checkouts: What Temp Is Still For

Scratch is fine in `/tmp` and in the harness scratchpad (`/private/tmp/claude-<uid>/...`): throwaway test repos made with `git init`, self-cleaning `mktemp -d` scripts, downloads, build output.  The ban is on a checkout of a FLEET repo (a clone, a worktree, a tarball extraction, an `init` plus `fetch`) in `/tmp`, `/private/tmp`, `/var/tmp`, `/var/folders/*/*/T`, or `$TMPDIR`.  Cloning a third-party repo to read it is allowed.

Non-git artifacts that must outlive a session (bundles, patches, DerivedData, tarballs) go in `~/apps/scratch/<seat>/<topic>/`, not in `/tmp`, so they show up in one place.

## Creating A Lane

```bash
export AGENT_SEAT=CLAUDE                      # your seat tag; lane refuses if it is unset or unknown
~/apps/lane new <app> <slug>                  # a lane for your seat; prints its path
~/apps/lane new <app> --review --pr <n>       # a read-only, detached checkout of a PR head
~/apps/lane path <app> <slug>                 # the path a lane would get; changes nothing
~/apps/lane path <app> --review --pr <n>      # the path a review checkout would get
~/apps/lane ls                                # every checkout on the Mac, with status (the doctor)
d=$(~/apps/lane new DealDex fix-login) && cd "$d"   # never cd "$(...)" alone: a refusal prints nothing and cd "" stays put
```

`<app>` is the repo folder (`Congress.Trade`), the old prefix (`congress`), the repo name or the acronym (`CT`), in any case.  `lane new` takes the seat ONLY from `AGENT_SEAT` (an uppercase registry tag such as `CLAUDE`, `AG`, `MM` or `CLUTCH`).  It never infers a seat from a login, a path or a branch; if the variable is unset or unknown, or names a retired seat, it refuses and you ask the owner.  The folder is `~/apps/lanes/<Repo>/<seat>-<slug>` with the whole seat name, and the branch is the seat's first registry branch prefix plus the slug (`claude/fix-login`, `minimax/fix-login`, folder `antigravity-fix-login` with branch `ag/fix-login`).  It fetches `origin/main`, creates the branch off it with `git worktree add` from the integration tree, writes a small `lane.json` manifest inside that worktree's private git folder, and refuses any path outside the map.  Re-running it for a lane that already exists prints the path and changes nothing, and a lane that still sits in an old folder (`~/apps/lanes/fleet/claude-fix-login`) is printed rather than duplicated until the migration moves it.  `--base`, `--reuse-branch`, `--purpose`, `--board`, `--dry-run` and `--json` are described in `scripts/fleet_lanes/README.md`; exit codes are 0 ok, 64 usage error or refusal, 69 git or gh failed.

`~/apps/lane` is the shim that `install_tools.py` writes; it runs the stable copy in `~/apps/lane-tools`.  A brand-new app or seat is refused until its row is in the registry that shim reads, so the first lane for a new app comes from a checkout of this repo with `FLEET_APPS_JSON` pointing at its `fleet-apps.json`, and `install_tools apply tools` refreshes the stable copy after the row merges.  Doing it by hand is allowed only if the result matches the tree above.  `lane` never creates a `.janitor-keep` file.

### What `lane ls` Says About A Place

Every checkout gets a layout status next to its safety class (`layout_status` in the JSON).  The location classes did not change, so the cleaners and the HogHunter vacuum see what they saw before.

| Status | Meaning | Examples |
|---|---|---|
| correct | Where v2 puts it | `~/apps/lanes/Socratic-Trade/claude-fix`, `.../BotFleet/review-pr-482`, `.../BotFleet/active-engines-display-e380b8` |
| legacy (migrate) | An old shape with its new path shown.  The migration moves the first two; a flat lane is not moved and retires where it is | `~/apps/lanes/fleet/claude-x` (and `botfleet`, `trading`, ...), `~/.codex/worktrees/<slug>/<Repo>` (with `--include-codex`), a flat `~/apps/fleet-claude-x` |
| legacy | A folder v2 abolishes | `~/apps/lanes/_managed/**`, `~/apps/lanes/_review/**` |
| Codex-managed | Codex's own layout | `~/apps/lanes/_codex/<slug>/<Repo>` |
| tool-managed | Another harness's own folder, as before | `~/.cursor/worktrees`, `~/.grok/worktrees`, `~/.gemini/antigravity/worktrees` |
| human tree | The integration tree | `~/Code/<Repo>` |
| wrong | Never allowed | a temp directory, a checkout directly under `~/Code`, a worktree inside `~/Code/<Repo>` (`.claude/worktrees`, `.muse/worktrees`, reported as `WRONG-PLACE`) |

## Tool Settings

The tools that choose their own worktree location are pointed at the v2 tree.  Claude desktop and Codex are changed with the app closed.

| Tool | Setting | Value | Status |
|---|---|---|---|
| Claude desktop | Settings, Claude Code, Worktree location (`chillingSlothLocation.customPath` in `claude_desktop_config.json`) | `~/apps/lanes` | Already set on this Mac.  The app files each worktree as `<location>/<Repo>/<slug>-<hex>`. |
| OpenCode | Session directory | `~/apps/lanes` | Owner decision 2026-10-09.  The sweep did not find where OpenCode keeps this setting, so it is not verified here. |
| Codex | `[desktop] git-worktree-root` in `~/.codex/config.toml` | `~/apps/lanes/_codex` | Codex then writes `_codex/<slug>/<Repo>`.  The current value is `~/apps/lanes/_managed/codex`. |

The other tools are as before (platform table).

## Platform Table

Facts verified 2026-10-07 unless the row says otherwise.  `UNVERIFIED` means the discovery sweep could not confirm it.

| Platform | Where it creates worktrees | Can the root move? | Deny hook surface | Rules file to carry this rule |
|---|---|---|---|---|
| Claude Code CLI | `~/Code/<Repo>/.claude/worktrees/<name>` (`claude -w`, subagent and workflow worktrees) | No setting.  A global `WorktreeCreate` hook could, but it has no matcher and it breaks the desktop warm pool and leaks branches, so it is rejected.  These paths are reported as `WRONG-PLACE` (the tools keep making them) but still count as harness-managed, so the janitor retires them.  The rule is to start in a lane made by `lane new` instead of using `claude -w`.  OWNER QUESTION: say whether subagent `isolation: worktree` stays allowed. | `PreToolUse` Bash hook in `~/.claude/settings.json` | `~/.claude/CLAUDE.md` |
| Claude desktop (Code tab) | The location in the tool settings table | Yes, `~/apps/lanes` (layout `<location>/<Repo>/<slug>-<hex>`; branch prefix is `ccBranchPrefix`) | Same hook as the CLI | Same |
| Codex | `~/apps/lanes/_codex/<slug>/<Repo>` once the setting above is changed (today `~/apps/lanes/_managed/codex/<slug>/<Repo>`; older ones sit in `~/.codex/worktrees/<slug>/<Repo>`) | Yes: `[desktop] git-worktree-root` (CLI `--worktree` behavior UNVERIFIED) | `PreToolUse` Bash in `~/.codex/hooks.json` (re-trust by hash after editing) | `~/.codex/AGENTS.md` |
| OpenCode | Its session directory, `~/apps/lanes` | Yes (setting location UNVERIFIED) | None found | UNVERIFIED |
| Cursor | `~/.cursor/worktrees` (hard-coded, empty today; Cursor opens lanes directly) | No | `beforeShellExecution` in `~/.cursor/hooks.json`; the installed entry is `failClosed: false`, so a broken install fails open and only `install_tools verify` proves it works | `~/.cursor/rules/*.mdc` (loading UNVERIFIED) plus each repo's `AGENTS.md` |
| Grok, Grok Build | `~/.grok/worktrees/<repo>/worktree-<id>` (empty today) | No documented key | `~/.grok/hooks/*.json`; also reads `~/.claude/settings.json` hooks (deny output format differs, UNVERIFIED) | `~/.grok/GROK.md` |
| Antigravity (AG) | `~/.gemini/antigravity/worktrees`, `~/.ag/worktrees` (empty); scratch clones in `~/.gemini/antigravity/scratch` | No | `~/.gemini/config/hooks.json`, matcher `run_command` (CLI; IDE UNVERIFIED) | `~/.gemini/config/AGENTS.md` |
| MiniMax (MM) | None of its own (it has made `/tmp` clones by typing commands) | n/a | None | `~/.minimax/memory/user.md` |
| Fx | None of its own | n/a | None found | `~/.fx/AGENTS.md` |
| Muse Code (`muse`, seat `MC`) | `~/Code/<Repo>/.muse/worktrees/<name>` for `muse -w` (evidence is strings in the binary, so the branch name and exact layout are UNVERIFIED).  Reported as `WRONG-PLACE` and still harness-managed (`muse-repo`) so it stays tracked: do not use `-w`, start `muse` inside a lane made by `lane new`.  An experimental `agents` plugin (gated off) would create `<repo-parent>/<repo>-threads`, a new folder beside `~/Code/<Repo>`; keep it off. | No.  No root setting was found (UNVERIFIED), so the answer is to not use the feature. | A user-scope plugin hook: `muse plugins install <dir> --scope user`, then `muse plugins approve fleet-lane-guard`, then a new session.  `~/.config/muse/settings.json` has no hooks key today, and the plugin is the surface the fleet uses.  The deny shape is claude-like and UNVERIFIED until the deny probe in `docs/MUSE-ONBOARDING.md` passes. | `~/.claude/CLAUDE.md`, which Muse Code loads as a user-scope fallback (every past session did), so the Lane Map block installed there reaches it.  A native `~/.config/muse/AGENTS.md` probably exists as a user rules file (UNVERIFIED), but creating it likely suppresses that fallback and drops the whole fleet protocol: do not create it unless it carries the full protocol.  `AGENT_SEAT=MC` comes from the `muse-seat` wrapper, because a past session echoed an empty seat. |
| Muse Assist (`MA`) | Nothing on this Mac.  It is a Meta Muse cloud assistant with its own Debian VM, started from the Mac and iOS apps, and it cannot see local files.  No lane is ever made for it. | n/a | None: no hooks (third-party sources, UNVERIFIED).  It can run CLIs on its VM, so the rule is stated in its own memory files instead. | Its Soul.md, Identity.md and Memory.md in the mobile app (no custom-instructions box; UNVERIFIED).  The owner pastes the card in `docs/MUSE-ONBOARDING.md`.  Credentials go through its secure store, never chat. |
| BotFleet bots | `~/.botfleet/workspaces/<uuid>` (nested clones); leases at `~/.botfleet/worktrees` (feature off) | Only `OMB_DATA_DIR` | `server/command-guard.ts` (backstop, BotFleet repo) | `bots/_shared.md` |
| Cloud agents (Cursor cloud, Codex cloud; Muse Assist has its own row) | Remote VMs | Local rules cannot apply | Project-level hooks only | Each repo's `AGENTS.md` |

## Existing Lanes And The Migration

Owner decision 2026-10-09: the existing lanes move to the v2 tree in one pass, after this layout merges and the live tools are refreshed.  The plan, the order and the checks that decide what is skipped are in `docs/protocols/lanes-v2-migration.md`; the script is `scripts/lanes-v2-migrate.py` (a dry run unless given `--apply`).

- The old shapes keep working until then: `lane ls` reports them as `legacy (migrate)` with their new path, and `lane new` finds a lane that has not moved yet.
- A moved lane leaves a symlink at its old path, pointing at the new one.  The symlinks stay for 7 days (the removal date is written to the migration log) so a shell or an editor that still has the old path keeps working, then `lanes-v2-migrate.py --remove-links` deletes only those symlinks.
- Lanes with a running process, uncommitted changes, a lock or another checkout below them (a gitignored `.claude/worktrees/<name>`) are skipped and listed; run the script again when they are free.
- `_managed` and `_review` disappear with their last entry.  The agent-sync runtime checkout under `_managed/fleet` is live infrastructure, so the script skips it and it needs its own step (owner decision pending).
- Bot lanes (BotFleet `kody`, `fixer`, `designer`, `compiler`) have no seat row in the registry; they are reported as `BOT-LANE` until the owner registers a scheme.

## Enforcement

Rules alone were never enough, so four layers back this one:

1. **Rule text** in each platform's own rules file (table above), installed by `scripts/fleet_lanes/install_rules.py`: `plan` is read-only and the default, `apply` names its platforms, a backup comes first, and the marker block is idempotent.  `plan` also reports lines outside the block that still state the old layout (flat lanes, `lanes/<prefix>/`, `_managed`, `_review`).  The rule text tells every agent to run `~/apps/lane`, so the tools are installed before the rules.
2. **A deny hook** (`scripts/fleet_lanes/lane_guard_hook.py`, installed by `install_tools.py` from the stable copy in `~/apps/lane-tools`) on every platform that has a hook surface.  It denies a checkout of a fleet repo in a temp directory and tells the agent the exact `~/apps/lane new` command to run instead.  It fails open on anything it cannot parse.  `install_tools verify` is the proof that an installed hook denies and allows correctly; the presence of an entry is not.  Platforms with no hook installed (MiniMax and Fx) rely on layers 1, 3 and 4, and so does Muse Code until the owner installs and approves its plugin hook (`docs/MUSE-ONBOARDING.md`): that approval is a step only the owner can do, so `install_tools` cannot finish it.
3. **`lane ls` / `lane doctor`** (`scripts/fleet_lanes/doctor.py`): report-only.  It finds EVERY checkout (registered worktrees, plus a bounded scan for full clones and hidden ones), says where it lives and whether that place is right (the layout status above), and classifies it `SAFE-TO-REMOVE`, `NEEDS-REVIEW`, `ACTIVE`, `UNKNOWN` or `TOOL-CACHE` with reasons.  A lane younger than 7 days (by age or idle time) with no merged PR reads `NEEDS-REVIEW` ("fresh lane").  `dropped_ball` marks work that exists only on this disk.  `--strict` exits non-zero on any checkout in a temp directory.  `--cleaner-list` prints the only paths a cleaner may remove.  It never deletes, moves, fetches, or rewrites an index.
4. **Cleaners read the doctor.**  `disk-janitor` and `mac-auto-cleanup` act only on `cleaner_candidates` from a fresh doctor report, never on `SAFE-TO-REMOVE` alone; removal is `git worktree remove`, never `rm -rf`.  Anything outside the map is reported, never removed on a guess.  The contract is in the next section.

## Cleaners

Anything that removes a checkout (the disk janitor, `mac-auto-cleanup`, a housekeeper bot, an agent told to "clean up the lanes") follows one contract:

1. **Act only on `cleaner_candidates`** from a fresh doctor report (`lane ls --cleaner-list`, or the `cleaner_candidates` array of a `--json` report).  A candidate is a registered linked worktree in a lane, review or managed location, clean, fully pushed or covered by a merged PR, with merged-PR evidence, no process using it, no read errors, and both its age and its idle time at least 7 days.  A full clone is never a candidate.  The v2 folders (`lanes/<Repo>/...`, `lanes/_codex/...`) are lane, review and managed locations like their predecessors, so the contract needs no change.
2. **Never act on `SAFE-TO-REMOVE` alone.**  A lane created an hour ago with no commits yet reads `SAFE-TO-REMOVE`.  The fresh-lane rule and the candidate list rule that out, but the label says nothing about whether anyone is about to use a lane, so a cleaner never gates on it.
3. **Check the report before trusting it:** `schema` is at least 2, `generated_at` is recent, `lsof` is `"ok"`, and `gh_repos[<owner/repo>]` is `"ok"` for the repo (a failed or skipped PR lookup means no candidates, which is the safe direction).
4. **Remove with `git worktree remove`**, never `rm -rf`, and never anything outside the map.
5. **Never create a `.janitor-keep` file** as protection.  It makes the doctor say `NEEDS-REVIEW` forever, and the dependency reaper ignores it.

The live `disk-janitor` asks the doctor and acts only on `cleaner_candidates`, and its layout test is "anything under the lanes root", so it needs no functional change for v2.  Claude desktop worktrees in `lanes/<Repo>/<slug>-<hex>` and Codex worktrees in `lanes/_codex` are classified as harness-managed on purpose: HogHunter's dependency step leaves harness-managed worktrees alone, and it would otherwise start clearing their `node_modules`.

## Open Questions For The Owner

1. **Review checkout name.**  `review-pr-<n>` has no seat in it.  The tool uses it, and falls back to `review-pr-<n>-<seat>` when another seat already holds that PR.  Say if you would rather have the seat in every name.
2. **The agent-sync runtime checkout** is a locked worktree at `~/apps/lanes/_managed/fleet/agent-sync-runtime`, and v2 abolishes `_managed` without naming a new home.  Moving it stops the Zulip listener for every seat unless the symlink, the sync script, the Claude marketplace entry and the LaunchAgents change together.  The migration skips it; pick its new path.
3. **Claude CLI and subagent worktrees** (`claude -w`, `isolation: worktree`) and `muse -w` still land in `~/Code/<Repo>/.claude/worktrees` and `.muse/worktrees`, which "never inside `~/Code/<Repo>`" forbids, and no setting moves them.  They are reported as `WRONG-PLACE` and still retired by the janitor.
4. **OpenCode.**  Where its session directory is set was not found, so no row is verified.

## Related

`AGENT-SYNC.md` (`~/Code` rule, pointer to this file, seat table) · `docs/protocols/lanes-v2-migration.md` (the migration) · `scripts/fleet_lanes/README.md` (every command and option) · `scripts/fleet_lanes/rules/` (the rule text the installer writes) · `docs/MUSE-ONBOARDING.md` (Muse Code checklist and the Muse Assist card) · `docs/ONBOARDING-NEW-APP.md` · `docs/ONBOARDING-NEW-AGENT.md` · board `a7dfde0e`.
