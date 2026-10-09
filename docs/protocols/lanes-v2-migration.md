# Lanes v2 Migration — moving the existing lanes (owner decision 2026-10-09)

> **Owner decision 2026-10-09.**  Jay approved layout v2 ("Go with this layout?", "yes"), the new tree in [Lane Map](lane-map.md).  This page is the plan for moving what already exists: the steps, their order, what each one runs, what is skipped and why, and the settings that only the owner can change.  The script is `scripts/lanes-v2-migrate.py`.  It is a **dry run unless given `--apply`**, it reads the disk when it runs (never a saved list), and nothing in it runs until the owner says go.  Board `a7dfde0e`.

## Where Everything Ends Up

```
before                                              after
~/apps/lanes/<prefix>/<seat>-<slug>                 ~/apps/lanes/<Repo>/<seat>-<slug>
~/apps/lanes/_review/<prefix>/pr-<n>[-<seat>]       ~/apps/lanes/<Repo>/review-pr-<n>[-<seat>]
~/apps/lanes/_managed/...                           (gone; Codex's goes to ~/apps/lanes/_codex)
~/.codex/worktrees/<slug>/<Repo>                    ~/apps/lanes/_codex/<slug>/<Repo>
~/Code/<Repo>/.claude/worktrees/<name>              stay (the app owns them), see "What The Script Does Not Move"
```

`<Repo>` is what git says, not what the old folder was called.  For a linked worktree it is the folder of the repo the worktree belongs to (`Socratic-Trade`, never the `Socratic.Trade` symlink); for a full clone it is the GitHub repo in its `origin` (`fleetlink-legacy`).  The old prefix folder is only a cross-check, and a disagreement is printed as a note.

## Order Of Work

Each step that touches a live file or setting needs the owner's go-ahead (owner ruling 2026-10-07).  Do them in this order, because the tools and the rule text must know the new layout before anything moves.

### Part A: Before The Migration

1. **Merge the layout PR** and make a fresh lane at `origin/main` (the old `lane new` still works for this one).
2. **Copy the live mirrors** (the list is in the PR description): `AGENT-SYNC.md`, the janitor, `mac-auto-cleanup.sh`, `otel-lane-tag.sh`, `MAC-LOCAL-PROCESSES.md` (merge, do not overwrite: the live file has newer rows) and its pinned Apple Note.
3. **Refresh the stable tools copy**, which is what `~/apps/lane` and every deny hook run.  It is older than `main` today, so nothing merged reaches it until this is done:
   ```bash
   cd <fresh lane>/scripts
   python3 -m fleet_lanes.install_tools plan tools
   python3 -m fleet_lanes.install_tools apply tools
   python3 -m fleet_lanes.install_tools verify tools     # the only proof that the deny hook still denies
   ```
   From here `~/apps/lane new` makes `~/apps/lanes/<Repo>/<seat>-<slug>`.
4. **Install the rule text.**  `python3 -m fleet_lanes.install_rules plan` first (it also lists lines outside the block that still teach the old layout), then `apply <platform>` for each of `claude` and `home-agents` (both need `--i-own-this-file`; `home-agents` is `~/AGENTS.md`, new in this change because no row refreshed its copy of the block), `codex`, `fx`, `grok`, `antigravity`, `minimax`, `cursor`, then `verify`.  The block is version 2 now and replaces the version 1 block in place, with a backup.  The lines outside the blocks are hand-written and the tool only reports them: `~/.claude/CLAUDE.md` and `~/AGENTS.md` (the "Lanes use the app's prefix" table, the flat lane sentence and the `--where` example), `~/.codex/AGENTS.md`, `~/.fx/AGENTS.md`, `~/.gemini/config/AGENTS.md`, `~/.minimax/AGENTS.md`.
5. **Install the skills** from a fresh lane at `origin/main`: `python3 scripts/install-fleet-skills.py` (the repo copies were re-rendered in the PR).  The orphan folders `~/Desktop/fleet-skills`, `~/.agents/skills` and `~/.claw/skills` are not install targets and stay stale.
6. **Owner settings** (app closed while editing):
   - **Codex:** in `~/.codex/config.toml`, under `[desktop]`, set `git-worktree-root = "/Users/jay/apps/lanes/_codex"` (today `.../lanes/_managed/codex`).
   - **Claude desktop:** the worktree location is already `~/apps/lanes` (`chillingSlothLocation.customPath`), so new desktop worktrees already land at `~/apps/lanes/<Repo>/<slug>-<hex>`.  After the migration, drop the stale `remoteControlPinnedFolders` entry `~/apps/lanes/_managed/claude` and the `remoteControlExcludedFolders` entries `~/apps/lanes/_managed/claude-desktop` and `~/apps/_pr-lanes`.
   - **OpenCode:** set the session directory to `~/apps/lanes`.  Where OpenCode keeps that setting was not found, so this one is the owner's to place.

### Part B: The Migration

Run it from outside the lanes tree (`cd ~`).  A lane with a process in it is skipped, and so is the lane the script itself lives in.  Ask the seats to commit, push and close their shells first; every lane left dirty or open is skipped, not lost.

**Pause the cleaners for the `--apply` window.**  `com.jay.disk-janitor` (every 30 minutes) runs `git worktree prune`, and a prune that lands while a folder is between paths deletes the admin entries of every lane in it, which `git worktree repair` cannot bring back.  The script renames a case-only folder with a single `rename(2)`, so the folder never leaves its path, and goes through a temporary name only if the volume refuses that; the pause covers the refusal case and every other moment of the run.  Boot the janitor out first (`launchctl bootout gui/$(id -u)/com.jay.disk-janitor`, the switch its own header documents) and `com.jay.mac-cleanup` with it, and bring both back when the run ends (`launchctl bootstrap gui/$(id -u) <their plists>`).  `com.jay.mac-process-watch` re-bootstraps scheduled jobs that are not loaded, within 2 minutes, unless it runs with `MAC_PROCESS_WATCH_RESTART=0`, so set that for the window too.

```bash
cd ~
python3 <fresh lane>/scripts/lanes-v2-migrate.py                      # 1. the dry run: read the plan
python3 <fresh lane>/scripts/lanes-v2-migrate.py --apply              # 2. do it
python3 <fresh lane>/scripts/lanes-v2-migrate.py --apply              # 3. again, once the skipped lanes are free
python3 <fresh lane>/scripts/lanes-v2-migrate.py --include-codex --apply   # 4. Codex worktrees, with the Codex app closed and its setting changed
~/apps/lane ls                                                        # 5. the Layout (v2) table: legacy (migrate) falls to what was skipped
python3 <fresh lane>/scripts/lanes-v2-migrate.py --remove-links --apply    # 6. seven days later
```

What `--apply` runs, in this order.  Every lane is checked again immediately before it moves, so a session that started after the dry run is respected.

1. **Per lane** (a registered linked worktree): make `~/apps/lanes/<Repo>/` if it is missing, then `git -C <main repo> worktree move <old> <new>`.  Git rewrites its own bookkeeping.  A full clone is a plain rename instead, and only if no other worktree hangs off it.  A lane that holds another checkout below it (see the skip table) is never moved.
2. **A symlink at the old path**, pointing at the new one, written to the log `~/apps/lanes/.lanes-v2-migration.json` with a removal date 7 days out (`remove_after`).  A shell, an editor or a script that still has the old path keeps working until then.
3. **A folder that differs only in letter case** (`botfleet` and `BotFleet` are one folder on APFS): the whole folder is renamed with one `rename(2)` (through a temporary name only if the volume does not take that), then `git worktree repair` runs from each repo with the new paths, and the one `gitdir` line git leaves alone (git ignores a case-only difference when `core.ignorecase` is on) is rewritten to the new spelling.  No symlink is made: the two spellings are one path.  This happens only when every checkout in the folder is safe to touch.  Otherwise the folder is **deferred** as a whole, because one lane in a case-only folder cannot be moved by itself.  A lane in such a folder that also needs a new name is renamed after the folder with `git worktree move` (a full clone with a plain rename, because git refuses to move a main working tree).  If another repo's checkout is still in the folder when the rename is about to run, the rename waits.
4. **Empty leftovers** (`_managed/<tool>`, `_review/<prefix>`, `_notes`) are removed with `rmdir`, which refuses anything that is not empty.  A `.DS_Store` is the only file the script ever deletes.
5. **Codex** (`--include-codex`): `~/.codex/worktrees/<slug>/<Repo>` **and** `~/apps/lanes/_managed/codex/<slug>/<Repo>` (Codex's current `git-worktree-root`, so an open Codex app is still using it) move to `~/apps/lanes/_codex/<slug>/<Repo>` the same way as a lane, with a symlink left behind.  Neither moves without the flag.  Codex keeps a `.codex-worktree-name` marker beside `<Repo>`; the script copies it into the new `<slug>` folder and never deletes the original, which is why the old `<slug>` folder stays.  Whether Codex's own registry of worktree paths follows the move is **UNVERIFIED**: change the setting and test one worktree before moving the rest.  A `<slug>` folder that holds only a marker is listed as a note.

### What Gets Skipped

A skip is never a failure: it is printed with its reason, nothing is changed, and the next run picks it up.

| Reason | Why |
|---|---|
| In use | A process has its working directory inside the lane (read with `lsof -d cwd`), or lsof could not be read at all.  The lane holding the script counts. |
| Uncommitted work | Any tracked change or untracked file.  The migration never moves work that only exists on this disk and is not saved. |
| Locked or prunable | A locked worktree, or one git lists as prunable. |
| Holds an inner checkout | A linked worktree, a submodule or a clone with worktrees hanging off it below the lane (four levels deep, `node_modules` and symlinks not entered): a `.claude/worktrees/<name>` from `claude -w` or a subagent with `isolation: worktree`, for example.  A plain nested clone such as SwiftPM's `.build/checkouts/<pkg>` carries no path back to the lane and does not block.  `.claude/` is gitignored in every fleet repo, so `git status` reads clean while the lane holds a whole checkout, and moving the lane would leave that checkout's admin entry pointing at the old path (the symlink hides it for 7 days, then `git worktree prune` strands it).  Close or remove the inner checkout, then run again. |
| Target exists | Something is already at the new path.  Nothing is overwritten. |
| Git refuses | `worktree move` refuses a worktree with submodules, for example.  The message is shown. |
| Cannot place it | A full clone with no GitHub `origin`, or a repo outside `~/Code` that is not a clone. |
| A symlink or a non-checkout | A symlink (already migrated, or not ours) is not followed.  A folder that is not a checkout and is not empty is left alone. |
| `agent-sync-runtime` | Always: see the next section. |

The only git commands the script can run are the read-only ones it needs plus `worktree move OLD NEW` and `worktree repair PATH...`.  There is no `--force`, no `remove`, no `prune`, no `reset`, no `push`, and no `rm -rf`; a unit test pins that list.

### The Runtime Checkout Is Not Moved

`~/apps/lanes/_managed/fleet/agent-sync-runtime` is a locked, detached worktree of this repo that the always-on Zulip listener runs from.  The `~/.local/bin/agent-sync` symlink, the `RUNTIME` default in `~/apps/agent-sync-runtime-sync.sh` (the `com.jay.agent-sync-runtime-sync` LaunchAgent, every 120 seconds), `~/.claude/settings.json` (the `afc` marketplace path), `scripts/agent_sync/install.sh` and its test, and the process list rows all hard-code that path.  Moving it breaks the listener for every Mac seat, and v2 names no new home for it.  The script skips it by name even if the lock is gone.  **Owner decision pending:** pick its path (outside the lanes tree, or `~/apps/lanes/AI-Fleet-Coordinator/agent-sync-runtime`, which makes it a lane-like object the doctor and the cleaners can see), then change all the couplings together and restart `com.jay.agent-sync-listener` and `com.jay.agent-sync-runtime-sync`.  Until then `_managed` keeps that one folder.

## An Example Plan

The disk on Fri, Oct 9, 2026, from a dry run of the script (lanes are being created and removed all day, so a real run will differ; the script reads the disk, not this table):

| Old folder | Becomes | What happens | Lanes in the plan |
|---|---|---|---|
| `fleet` | `AI-Fleet-Coordinator` | move + symlink | 44 move; 3 skipped (the lane running the script, one with a running process, one with uncommitted work) |
| `trading` | `Socratic-Trade` | move + symlink | 3 |
| `congress` | `Congress.Trade` | move + symlink | 5 move; 1 skipped (uncommitted work) |
| `usage` | `Usage-Monitor` | move + symlink | 5 move; 1 skipped (uncommitted work) |
| `cts` | `congress-trading-shared` | move + symlink | 4 |
| `personal` | `Personal-Site` | move + symlink | 2 |
| `botfleet` | `BotFleet` | case-only folder rename | **deferred** while any of its 40 or so lanes has a process or uncommitted work (several do) |
| `hoghunter`, `clutch`, `dealdex`, `autorotate`, `contactlogo`, `codecaps`, `fleet-ops` | `HogHunter`, `Clutch`, `DealDex`, `Autorotate`, `ContactLogo`, `CodeCaps`, `Fleet-OPS` | case-only folder rename | one rename each, when every lane in it is safe |
| `fleetlink` | `FleetLink` (the `minimax-zulip-stanza-FleetLink` worktree, renamed `minimax-zulip-stanza`) and `fleetlink-legacy` (the `claude-legacy-hotfix` clone) | the clone moves out first, then the folder is renamed | 2 |
| `_managed/codecaps-cursor-163` | `CodeCaps/codecaps-cursor-163` | move + symlink (the name is kept; it does not read as `<seat>-<slug>`, so the doctor will call it a name problem) | 1 |
| `_managed/fleet/agent-sync-runtime` | stays | skipped by name | 1 |
| ten empty `_managed/<tool>` folders, `_review`, `_notes` | removed | `rmdir` | 12 |
| `~/.codex/worktrees/*/*` | `_codex/<slug>/<Repo>` | with `--include-codex` only | 6 (5 CodeCaps, 1 BotFleet) |

The dry run also lists the lanes that need action before the case-only folders can be renamed: commit and push, or close the shell.  `botfleet` is the likely long pole.

## What The Script Does Not Move

- **`~/Code/<Repo>/.claude/worktrees/*` and `.muse/worktrees/*`** (34 Claude worktrees, mostly BotFleet).  The Claude desktop and CLI own them, their registry (`git-worktrees.json`, `~/.claude.json` project keys) is app state that is never hand-edited, and the merged ones are being removed by the cleanup.  New desktop worktrees already go to `~/apps/lanes/<Repo>/<slug>-<hex>`.  `lane ls` reports the rest as `wrong` (the `WRONG-PLACE` anomaly) so they stay visible.
- **Flat lanes** (`~/apps/<prefix>-<seat>-<slug>`, about 126 on the Mac), `~/apps/mkt/*`, `~/apps/<Repo>-mm-rename`, `~/codecaps-pages`.  They are reported as `legacy (migrate)` by `lane ls` with their new path, and a later pass can move them with the same script.
- **Anything with uncommitted work, a process or a lock**, until it is free.

## Doing One Lane By Hand

For a lane the script skipped and you have since made safe:

```bash
git -C ~/Code/<Repo> worktree move ~/apps/lanes/<old>/<name> ~/apps/lanes/<Repo>/<name>   # a linked worktree
ln -s ~/apps/lanes/<Repo>/<name> ~/apps/lanes/<old>/<name>                                 # optional, for 7 days
# a case-only folder (cleaners paused): one rename, so no lane leaves its path, then repair
mv ~/apps/lanes/botfleet ~/apps/lanes/BotFleet
git -C ~/Code/BotFleet worktree repair ~/apps/lanes/BotFleet/<lane> ...
```

## If A Step Fails

- **A `worktree move` failed:** git checks before it moves and the script makes no symlink until the new place checks out, so the lane is where it was.  Read the message, fix the cause, run the script again.
- **A move finished but the new place does not check out:** the script stops that lane and says so.  Move it back with `git -C <main repo> worktree move <new> <old>`, then `git -C <main repo> worktree repair <old>`.
- **A folder rename stuck at `.<name>-case-rename-<pid>`:** rename it back by hand (`mv ~/apps/lanes/.<name>-case-rename-<pid> ~/apps/lanes/<name>`), then `git worktree repair` on its lanes.
- **A symlink in the way:** `--remove-links` removes only the symlinks the log recorded, only when due, and only when each still points where it was made to point.  A real folder or a symlink that points elsewhere is never touched.  A link whose lane the janitor has since retired still points where it was made to point, leads nowhere, and goes on its date like the others, so the old prefix folders can empty.

## After The Migration

- Add the 2026-10-09 ruling to the Claude memory (`lane-map-owner-rulings-2026-10-07.md`) and refresh its index line.
- Update the process list and its pinned Apple Note: the `lane` shim row (new refresh date), the `otel-lane-tag.sh` row (now tracked, layout v2) and the new `lanes-v2-migrate.py` row (on-demand).
- Run `python3 -m fleet_lanes.doctor --only-class LANE_NESTED --json` and check `by_layout_status`: `legacy-migrate` should be down to the skipped lanes and the flat ones.

## Related

[Lane Map](lane-map.md) · `scripts/fleet_lanes/README.md` · `scripts/lanes-v2-migrate.py` · `scripts/fleet_lanes/tests/test_migrate_v2.py` · board `a7dfde0e`.
