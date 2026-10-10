# fleet-work-backup

Backs up the fleet's work that GitHub does not hold, to an encrypted, deduplicated restic
repository on Backblaze B2.  Owner request, Sat, Oct 10, 2026:  Time Machine and `/Volumes/External`
are the same physical SSD, and Time Machine has never covered `~/apps`, so back up only the things
that matter and are not on GitHub.

Restore steps are in [RESTORE.md](RESTORE.md).

## What is backed up

**(a) Git work**, for every checkout the fleet uses:  lanes under `~/apps/lanes/**` (the
`<Repo>/*` entries are symlinks onto `/Volumes/External/Lanes`, and so are read as one place), the
old prefix folders (`botfleet`, `fleet`, `usage` and the rest), `_managed` and `_codex`, flat
`~/apps/<prefix>-<seat>` lanes, other clones under `~/apps` (to depth 3), and the human trees
`~/Code/<Repo>`.  Checkouts are grouped by `git rev-parse --git-common-dir`, so a repository and all
of its linked worktrees are one group.

| Per group (repository) | What |
|---|---|
| `git/<repo>-<hash>/unpushed.bundle` | One `git bundle` of every local branch and tag whose commits no remote-tracking ref holds (`--not --remotes`).  A branch whose merge into the default branch is a no-op (a squash-merged lane) is skipped as landed.  A repo with no remote is bundled whole and flagged in the dry run. |
| `git/<repo>-<hash>/stashes/NN/` | Every `stash@{N}` as one patch per file (`tracked/`, `untracked/`) plus `stash.json` (base sha, subject).  No ref is created in the repository. |
| `git/<repo>-<hash>/group.json` | Common dir, remotes (credentials stripped from URLs), the refs bundled, the count of landed branches skipped. |

| Per worktree | What |
|---|---|
| `worktrees/<name>-<hash>/patches/NNNN-<path>.patch` | One patch per changed tracked file, `git diff --binary HEAD` (staged and unstaged together), so a secret hit drops one file and not the repo. |
| `worktrees/<name>-<hash>/untracked/<path>` | Files `git ls-files --others --exclude-standard` lists (git-ignored files are never copied). |
| `worktrees/<name>-<hash>/worktree.json` | Path, `HEAD` sha and branch (the base for `git apply`). |
| `worktrees/<name>-<hash>/head.bundle` | Only for a detached `HEAD` that no remote has. |

A checkout with nothing unpushed, no stash and nothing dirty leaves nothing behind.

**(b) Non-git files** under `~/apps`, outside any checkout:  `AGENT-SYNC.md`, the effort logs,
`MAC-LOCAL-PROCESSES.md`, helper scripts, `mac-collab/findings.db` and its `backups/` folder, runtime
configs.  They are staged under `files/`, which mirrors the path relative to the home directory
(`files/apps/AGENT-SYNC.md`).  The loose files directly in `~/Code` are included too.  SQLite files
are copied with the online backup API (a live WAL-mode database is never torn;  `-wal` and `-shm`
sidecars are skipped).  Symlinks are stored as symlinks, never followed.

**Skipped on purpose:**  `node_modules`, `.venv`, `venv`, `__pycache__`, caches, `dist`, `build`,
`.next`, `Pods`, `DerivedData`, `logs/` and `*.log`, `*.pyc`, `.DS_Store`, `*.sock`, `*.pid`, the
`~/apps/lanes` folder itself, this job's install and staging folders, and any file over 200 MB
(listed in the dry run and the detail log instead).

## What is never backed up

Secrets.  Infisical is the source of truth, and a restic repository is a second place for a secret to
leak from.  Two layers:

1. **Name filter, before anything is copied.**  Strict names are dropped everywhere, tracked-file
   patches included:  `.env`, `.env.*`, `*.env`, `*.pem`, `*.p8`, `*.p12`, `*.pfx`, `*.key`,
   `*.keystore`, `*.jks`, `*.keychain*`, `id_rsa*` / `id_ed25519*` and the other `id_*` keys, `*zuliprc*`,
   `credentials.json`, `.netrc`, `.npmrc`, `global-api-keys*`, `infisical-machine-identity*`,
   `service-account*.json`, and anything under `.secrets`, `.ssh`, `.gnupg`, `.aws`, `.infisical`,
   `Keychains`.  Broad names (`*secret*`, `*token*`, `*credential*`, `*passwd*`, and a directory
   named like `*secret*`) are dropped for untracked and non-git files, as the owner asked.  For the
   patch of a file git already tracks the broad names do not apply (`tokenizer.py` is source, and
   losing its uncommitted edits would defeat the backup);  the content scan still guards it.
   `.env.example`, `.env.sample` and `.env.template` pass the name filter and still get scanned.
2. **Content scan, over the staged set.**  A regex scan always runs (private key blocks, AWS, GitHub,
   Slack, Stripe, Google, Anthropic, OpenAI, JWT, bearer headers, and `*_KEY` / `*_SECRET` /
   `*_TOKEN` / `*_PASSWORD` assignments with a high-entropy value);  `gitleaks dir` runs as well when it
   is installed.  Binary files are scanned too (a SQLite page holds plain text).  A match drops the
   file (or, for a tracked file's diff, that file's patch alone) and the path goes to the detail log
   with a reason.  The values are never logged.  Unpushed commits are scanned with the same regex
   over `git log -p` (first 128 MB) before they are bundled;  a hit drops the bundle and names the
   repository.

If the gate drops `mac-collab/findings.db` or its backups, that is reported in the detail log under
`dropped`;  the board's database not being backed up is not something to leave silent.

## Where it goes

Restic repository `s3:https://s3.eu-central-003.backblazeb2.com/<B2_FLEET_BUCKET_NAME>/fleet-work-backup`
(the endpoint the fleet's litestream config already uses).  Tag `fleet-work`, host `jay-mac`.
Retention on every run:  `--keep-hourly 24 --keep-daily 14 --keep-weekly 8 --keep-monthly 6`.
`forget` runs each time;  `--prune` at most once a day and `restic check` at most once a week, because
list and delete calls are B2 transactions (board PD #388 is a B2 request spike).

The staging folder is fixed at `~/Library/Caches/fleet-work-backup/stage` (restic picks the parent
snapshot by path, so a timestamped folder would break deduplication and retention).  Files are
cloned with `clonefile(2)` where the volume allows (free, and a point-in-time copy:  what the gate
scanned is what restic reads), copied otherwise, and the folder is removed after the run.  restic
runs with `--ignore-inode --ignore-ctime`, because a rebuilt folder has new inodes.

## Credentials

Read by the job from `~/.secrets/global-api-keys` (or the environment) into the restic
subprocess environment only.  Names, never values:

| Name | Use |
|---|---|
| `B2_FLEET_BUCKET_NAME` | bucket (already in the handoff file) |
| `B2_FLEET_WORK_KEY_ID`, `B2_FLEET_WORK_APPLICATION_KEY` | a **bucket-scoped** B2 application key, preferably also limited to the file-name prefix `fleet-work-backup/`.  Capabilities:  `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` (prune needs it), and `listBuckets`. |
| `RESTIC_FLEET_WORK_PASSWORD` | the repository password |

The master key (`BACKBLAZE_MASTER_KEY_ID` / `BACKBLAZE_MASTER_APPLICATION_KEY`) is used only when
`FLEET_WORK_BACKUP_ALLOW_MASTER=1`, and only for names the scoped key leaves empty.

All three secrets live in Infisical (project `AI Fleet Coordinator`, `prod`) and reach the handoff
file through the one-way `com.jay.infisical-secrets-sync` job:  `scripts/infisical-secrets-sync.sh`
has a `MAP` row for each, and the sync reads exactly the rows in that array.  The password must be a
urlsafe token of at least 16 characters, with no whitespace and no backslash (the sync's plausibility
check rejects anything else, and its `awk -v` mangles backslashes):
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, piped straight into Infisical and
never into a terminal log.  **Losing the password means losing every backup**, which is why
Infisical, not the handoff file, is the copy of record.

## Schedule and safety

LaunchAgent `com.jay.fleet-work-backup`, every 3 hours (`StartInterval` 10800), `RunAtLoad` false,
`ProcessType Standard` (the background class starves jobs on this Mac under load).

- **Lock:**  `flock` on `~/Library/Application Support/fleet-work-backup/lock`;  an overlapping run
  logs `skip reason=lock-held`.
- **Pressure gate:**  skips when load1 is over 60 or swap use is 95% or more.  This Mac has seen load
  300 to 460, so after 8 consecutive skips (about 24 hours) the log line carries
  `warn=no-backup-for-about-24h`.  `--ignore-pressure` overrides for a manual run.
- **Disk:**  skips when the internal disk has under 5 GB free.
- **Runtime cap:**  2 hours.
- **Read-only git:**  `GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false`, `safe.directory=*`, no fetch, no
  gc, no ref writes.  The one side effect is git's own:  the landed-branch check runs `git merge-tree`
  with objects written to a scratch directory, and git refreshes the mtime of objects it finds again.

### Removable Volumes (TCC)

launchd-spawned processes on this Mac cannot read `/Volumes/External` (removable-volume privacy
denied `CoreSimulatorService` and `npx` the same way), and the lanes are symlinks onto it.  The job
therefore probes first.  If it cannot read a lane it backs up everything it can reach, logs
`status=partial warn=removable-volumes-denied grant=<binary>`, and writes the instruction to the
detail log.  To fix it, open System Settings > Privacy & Security, grant **Removable Volumes** (or
**Full Disk Access**) to the interpreter named in `grant=`, and run

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py check-access

`git` children inherit the interpreter's grant;  if one does not, set `FLEET_WORK_BACKUP_GIT` in the
plist to a git binary that has the grant.  `/opt/homebrew/bin/python3` resolves to a versioned Cellar
path (`.../python@3.14/3.14.8_1/.../python3.14`), so **a brew upgrade can drop the grant**;  the log
line then names the new binary.  The staging folder is on the internal disk, so restic itself never
reads `/Volumes/External`.

## Logs

- `~/Library/Logs/fleet-work-backup.log`:  one line per run, key=value, 12-hour local time.
  `status` is `ok`, `partial` (some path unreadable), `skip` (`reason=` load, swap, lock-held, low-disk)
  or `error` (`reason=` restic-missing, missing-credentials with the NAMES, restic-backup-failed, timeout).
  Fields:  `checkouts groups work unpushed stashes dirty files staged dropped large blocked snapshot
  added forget prune check elapsed`.
- `~/Library/Logs/fleet-work-backup.detail.log`:  per run, the paths the gate dropped (with reason),
  large files skipped, blocked paths, notes.  Paths only.
- `.out.log` / `.err.log`:  launchd stdout and stderr.

## Commands

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py run --dry-run --json /tmp/x.json
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py init            # once, explicit
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py check-access
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py snapshots
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py restic -- ls latest --tag fleet-work

`--dry-run` builds the staged tree in `stage-dry`, prints counts and sizes, uploads nothing, and takes
neither the lock nor the pressure gate.  A scheduled run never runs `restic init`.

## Install

Order matters;  nothing here runs by itself:

1. `brew install restic` (or `install.py --install-restic`).
2. Create the bucket-scoped B2 application key.
3. Write `RESTIC_FLEET_WORK_PASSWORD`, `B2_FLEET_WORK_KEY_ID` and `B2_FLEET_WORK_APPLICATION_KEY` to
   Infisical (project `AI Fleet Coordinator`, `prod`).
4. `install -m 755 scripts/infisical-secrets-sync.sh ~/bin/` and wait one tick;  `~/.secrets/infisical-sync.log`
   shows `updated <KEY> (new_len=N)` for each.
5. `python3 scripts/fleet-work-backup/install.py --skip-launchd`, then `python3 ~/apps/fleet-work-backup/fleet_work_backup.py init`.
6. `python3 scripts/fleet-work-backup/install.py` (loads the LaunchAgent;  refuses while a prerequisite is missing).
7. Grant Removable Volumes to the interpreter and run `check-access`.
8. Update the live `~/apps/MAC-LOCAL-PROCESSES.md` and its Apple Note.

## Tests

    python3 scripts/fleet-work-backup/test_fleet_work_backup.py

Fake repositories in a temp home:  an unpushed commit, a landed branch, a repo with no remote, a
detached `HEAD`, two stashes, a dirty linked worktree, tracked and untracked files, an ignored file, and
secret-looking names and contents, plus a live WAL-mode SQLite file.  restic and gitleaks are stubs.  The
restore commands in RESTORE.md are exercised by the tests, and a test checks that the `.git`
directories are unchanged by a run.  Runs in CI (ubuntu).
