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
`~/apps/<prefix>-<seat>` lanes, other clones under `~/apps` (to depth 3), the human trees
`~/Code/<Repo>`, and any linked worktree `git worktree list` names, wherever it lives
(`~/.codex/worktrees`).  Checkouts are grouped by `git rev-parse --git-common-dir`, so a repository and
all of its linked worktrees are one group.

"Pushed" means a **network** remote has the commit (an `https://`, `ssh://` or `user@host:` URL).  A remote
that is a local path, a `file://` URL or a bundle file is the same disk and does not count;  a repository
with only such remotes has its whole history bundled (the dry run lists it, the detail log says
`local-only-remotes`).

| Per group (repository) | What |
|---|---|
| `git/<repo>-<hash>/bundles/<hash>-<ref>.bundle` | `git bundle` files holding every local branch and tag no network remote has.  Refs whose commits overlap share one bundle;  independent branches get their own, so a restore of one never depends on another, and an unchanged branch is byte-identical from run to run (single-threaded pack) and costs restic nothing.  Each bundle is **bounded by the default branch** of the network remote (`origin/HEAD`, else `origin/main`, `origin/master`), not by every remote-tracking ref:  its prerequisites are commits GitHub keeps for good, so a branch deleted or force-pushed on GitHub after the snapshot cannot make the bundle unusable.  The price is that a bundle also holds the commits of pushed-but-unmerged branches it was stacked on.  A branch whose merge into the default branch is a no-op (a squash-merged lane) is skipped as landed.  A bundle over 200 MB is listed, not kept. |
| `git/<repo>-<hash>/stashes/NN/` | Every `stash@{N}` as one patch per text file (`tracked/`, `untracked/`), the raw new content of each binary file (`tracked/binary/`, `untracked/binary/`), and `stash.json` (base sha, subject).  No ref is created in the repository. |
| `git/<repo>-<hash>/group.json` | Common dir, remotes (credentials stripped from URLs), the bound, the bundles and their refs, the count of landed branches skipped. |

| Per worktree | What |
|---|---|
| `worktrees/<name>-<hash>/patches/NNNN-<path>.patch` | One patch per changed **text** file, `git diff --binary HEAD` (staged and unstaged together), so a secret hit drops one file and not the repo. |
| `worktrees/<name>-<hash>/patches/binary/<path>` | A changed **binary** tracked file, stored whole.  git's own binary patch is deflated base85 that no scanner can read, so it is never staged;  the raw file is, and the gate scans it.  `patches/binary-deleted.txt` lists binary files that were deleted. |
| `worktrees/<name>-<hash>/untracked/<path>` | Files `git ls-files --others --exclude-standard` lists (git-ignored files are never copied). |
| `worktrees/<name>-<hash>/worktree.json` | Path, `HEAD` sha and branch (the base for `git apply`). |
| `worktrees/<name>-<hash>/head.bundle` | Only for a detached `HEAD` that no network remote has;  scanned like a branch. |

A checkout with nothing unpushed, no stash and nothing dirty leaves nothing behind.

**(b) Non-git files** under `~/apps`, outside any checkout:  `AGENT-SYNC.md`, the effort logs,
`MAC-LOCAL-PROCESSES.md`, helper scripts, `mac-collab/findings.db` and its `backups/` folder, runtime
configs.  They are staged under `files/`, which mirrors the path relative to the home directory
(`files/apps/AGENT-SYNC.md`).  The loose files directly in `~/Code` are included too.  SQLite files
are copied with the online backup API (a live WAL-mode database is never torn;  `-wal` and `-shm`
sidecars are skipped).  Symlinks are stored as symlinks, never followed.  A folder whose `.git` points
nowhere (a pruned worktree) is not a checkout, so its files are kept here.

**Skipped on purpose:**  `node_modules`, `.venv`, `venv`, `__pycache__`, caches, `dist`, `build`,
`.next`, `Pods`, `DerivedData`, Playwright browsers (`.pw-browsers`), Xcode index stores and
`build-local*`, `logs/` and `*.log`, `*.pyc`, `.DS_Store`, `*.sock`, `*.pid`, the `~/apps/lanes` folder
itself, this job's install and staging folders, and any file over 200 MB (also a single patch or bundle
over 200 MB;  listed in the dry run and the detail log instead).  Runtime session stores are skipped by
default because tool output in a transcript carries credentials in shapes no scanner knows, and the tool
rebuilds them:  `grok-acp-runtime/acp-home/sessions`, `acp-home/memtrace`, `fleet-rag/mined/chat` (the
detail log notes each as `skipped-by-default`;  `FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS=1` turns the
list off).  `FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS=name1,name2` adds directory names without a code change.

## What is never backed up

Secrets.  Infisical is the source of truth, and a restic repository is a second place for a secret to
leak from.  Two layers:

1. **Name filter, before anything is copied.**  Strict names are dropped everywhere, tracked-file
   patches included:  `.env`, `.env.*`, `*.env`, `*.pem`, `*.p8`, `*.p12`, `*.pfx`, `*.key`,
   `*.keystore`, `*.jks`, `*.keychain*`, `id_rsa*` / `id_ed25519*` and the other `id_*` keys, `*zuliprc*`,
   `credentials.json`, `auth.json`, `*creds*.json`, `.dev.vars*`, `.envrc`, `*.tfstate*`, `*.tfvars`,
   `.netrc`, `.npmrc`, `global-api-keys*`, `infisical-machine-identity*`, `service-account*.json`,
   `gh/hosts.yml` (a bare `hosts.yml` is an Ansible inventory and is kept), and anything under
   `.secrets`, `.ssh`, `.gnupg`, `.aws`, `.infisical`, `Keychains`.  Broad names (`*secret*`, `*token*`,
   `*credential*`, `*passwd*`, and a directory named like `*secret*`) are dropped for untracked and
   non-git files, as the owner asked.  For the patch of a file git already tracks the broad names are
   waived for **source and prose only** (`.py .ts .swift .sh .md` and the like):  `tokenizer.py` is source,
   and losing its uncommitted edits would defeat the backup, while a modified `client_secret.json`,
   `secrets.yaml` or `service_token.txt` is dropped.  `.env.example`, `.env.sample` and `.env.template`
   pass the name filter and still get scanned.
2. **Content scan, over the staged set.**  The regex scan always runs (private key blocks, AWS, GitHub,
   Slack, Stripe, Google, Anthropic, OpenAI, JWT, bearer headers, and `*_KEY` / `*_SECRET` /
   `*_TOKEN` / `*_PASSWORD` assignments with a high-entropy value);  `gitleaks dir` runs as well when it
   is installed.  A match drops the file (or, for a tracked file's diff, that file's patch alone) and the
   path goes to the detail log with a reason.  The values are never logged.

   **The gate fails closed.**  With gitleaks installed, a text file goes into a snapshot only after
   gitleaks has cleared it.  If gitleaks fails on a batch it is bisected, so one unreadable file is left
   out alone;  if the 25 minute budget runs out, or a finding maps to no staged file, the files not yet
   cleared are left out of **this** snapshot (`deferred` in the run line and the detail log) and picked up by the
   next run, because the scan cache remembers what already passed.  The size limit given to gitleaks equals
   the staging cap, so nothing staged is skipped for being large.  A run with gitleaks missing says so:
   `gate=regex-only warn=gitleaks-missing`.

   **Binary content** (a NUL byte in the first 8000 bytes, SQLite) is the one place gitleaks does not
   help, because it does not read binary files.  The regex scan reads them whole, and the run line counts
   them (`regex_only=N`).  A changed binary file in a worktree or a stash is staged as the raw new content,
   never as git's binary patch, for exactly that reason.

   Unpushed commits are scanned with the same regex over `git log -p --cc` (first 128 MB, the
   exact range the bundle holds) before they are bundled.  A hit is then isolated ref by ref (10 minute
   budget), so one branch holding a key-shaped string costs only that branch:  the clean refs are still
   bundled and each dropped ref is named.  Not covered:  gitleaks does not run over history, and a secret
   in a pushed commit that a bundle carries along is already on GitHub.

A key-shaped string that is plainly an example (`sk_live_xxxxxxxx`, `AKIA...EXAMPLE`, a PEM header with
no key body after it) does not count.  The scan is cached by (path, size, mtime or content hash) in
`~/Library/Application Support/fleet-work-backup/scan-cache.json`, so a steady-state run scans only what
changed;  gitleaks runs in 256 MB batches, throttled (two threads, low priority).

**Reviewing what the gate dropped.**  The detail log lists every drop with a reason (`secret-name`,
`content:<pattern>`, `gitleaks:<rule>`) and, under each content hit, an `allow-line` to paste.  The first
dry run over the real machine dropped files that are only documentation (a rule file quoting an example
Stripe key, Cloudflare skill references with bearer-header examples) and some unpushed branches.  When you
have looked and the match is harmless, copy the `allow-line` (without the `allow-line ` prefix) into
`~/Library/Application Support/fleet-work-backup/allow.txt`.  A line is `<path> @ <digest>`:  the digest is
the sha1 of the file (or the tip sha of the branch) **you reviewed**, so a real key pasted into that file
later, or a new commit on that branch, no longer matches and the file is dropped again (the detail log says
`allow-stale`).  A line without `@ digest` is ignored (`allow-unpinned`), and a **secret name is never
allowed**.  An allowed file still goes through gitleaks.

If the gate drops `mac-collab/findings.db` or its backups, that shows in the detail log too;  the
board's database not being backed up is not something to leave silent.

## Where it goes

Restic repository `s3:https://s3.eu-central-003.backblazeb2.com/<B2_FLEET_BUCKET_NAME>/fleet-work-backup`
(the endpoint the fleet's litestream config already uses).  Tag `fleet-work`, host `jay-mac`, plus tag
`partial` on a snapshot that was missing something (an unmounted volume, a denied path, files deferred by
the gate).  Retention on every run, per host and tag set, so partial snapshots have their own slots and
never push complete ones out:  `--keep-hourly 24 --keep-daily 14 --keep-weekly 8 --keep-monthly 6`.
`forget` runs each time;  `--prune` at most once a day and `restic check` at most once a week, because
list and delete calls are B2 transactions (board PD #388 is a B2 request spike).

The bucket is shared (`litestream.yml` replicates the board database into it), so the application key
**must** be limited to the file-name prefix `fleet-work-backup/`.  Two B2 facts that need a bucket setting,
not code:  over the S3 API a delete only hides a file version, so `restic prune` frees no space until the
bucket has a **lifecycle rule** on the prefix `fleet-work-backup/` with `daysFromHidingToDeleting` of 7 to
30 (which also leaves a soft-delete window);  and a key that can delete lets any process on the Mac that can
read it erase the history, so keep the key in the handoff file only (mode 600) and consider Object Lock.

The staging folder is fixed at `~/Library/Caches/fleet-work-backup/stage` (restic picks the parent
snapshot by path, so a timestamped folder would break deduplication and retention).  Files are
cloned with `clonefile(2)` where the volume allows (free, and a point-in-time copy:  what the gate
scanned is what restic reads), copied otherwise, and the folder is removed after the run.  restic
runs with `--ignore-inode --ignore-ctime`, because a rebuilt folder has new inodes, at low CPU priority
(`nice 10`, two threads) and gets SIGINT, then a minute, before any kill, so its repository lock is released.

## Credentials

Read by the job from `~/.secrets/global-api-keys` (or the environment) into the restic
subprocess environment only.  Names, never values:

| Name | Use |
|---|---|
| `B2_FLEET_BUCKET_NAME` | bucket (already in the handoff file) |
| `B2_FLEET_WORK_KEY_ID`, `B2_FLEET_WORK_APPLICATION_KEY` | a **bucket-scoped** B2 application key limited to the file-name prefix `fleet-work-backup/`.  Capabilities:  `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` (prune needs it), and `listBuckets`. |
| `RESTIC_FLEET_WORK_PASSWORD` | the repository password |

The master key (`BACKBLAZE_MASTER_KEY_ID` / `BACKBLAZE_MASTER_APPLICATION_KEY`) is used only when
`FLEET_WORK_BACKUP_ALLOW_MASTER=1`, and only for names the scoped key leaves empty.

All three secrets live in Infisical (project `AI Fleet Coordinator`, `prod`) and reach the handoff
file through the one-way `com.jay.infisical-secrets-sync` job:  `scripts/infisical-secrets-sync.sh`
has a `MAP` row for each, and the sync reads exactly the rows in that array.  The password must be a
urlsafe token of at least 16 characters, with no whitespace and no backslash (the sync's plausibility
check rejects anything else, and its `awk -v` mangles backslashes), generated without printing it:
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` piped straight into the Infisical write,
never into a terminal log.

**The password is the only key to every snapshot, so it is never rotated in Infisical.**  The sync
overwrites the Mac's copy whenever Infisical changes, and a changed value there makes every run fail with
`reason=wrong-password` while the old one survives only in Infisical's version history.  A key changes only
with `restic key add` and `restic key remove` against the repository.  At `init`, add a second
**recovery key** (`python3 .../fleet_work_backup.py restic -- key add`) and keep its password outside
Infisical (Apple Passwords, or on paper):  without it, losing Infisical while the Mac is gone loses every
backup.  The run line reports a wrong password as its own reason.

## Schedule and safety

LaunchAgent `com.jay.fleet-work-backup`, every 3 hours (`StartInterval` 10800), `RunAtLoad` false,
`ProcessType Standard` (the background class starves jobs on this Mac under load) with `Nice 5` and
`LowPriorityIO` (the mild knobs).

- **Lock:**  `flock` on `~/Library/Application Support/fleet-work-backup/lock`;  an overlapping run
  logs `skip reason=lock-held`.
- **Pressure gate:**  skips when load1 is over 60, or memory pressure is critical
  (`kern.memorystatus_vm_pressure_level` 4), or swap use is 95% or more **while pressure is at least
  warning**.  Swap alone is not used where the sysctl exists:  macOS grows swap on demand, and this Mac sat
  at 95% swap with pressure normal, so the literal rule would skip every run on an idle machine.  Where the
  sysctl is missing the swap rule stands alone.  This Mac has seen load 300 to 460, so after 8 consecutive
  skips (about 24 hours) the line carries `warn=no-backup-for-about-24h`;  `--ignore-pressure` overrides
  for a manual run.
- **Not ok in a row:**  every run that makes no complete snapshot (error, partial, skip) is counted;  after 8
  in a row the line carries `warn=no-complete-backup-for-about-24h` and every such line shows `last_ok=`.
- **Disk:**  skips when the internal disk has under 5 GB free.
- **Runtime cap:**  2 hours.
- **Read-only git:**  `GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false`, `safe.directory=*`, no fetch, no
  gc, no ref writes.  The one side effect is git's own:  the landed-branch check runs `git merge-tree`
  with objects written to a scratch directory, and git refreshes the mtime of objects it finds again.
  A failing `for-each-ref` or `rev-list` is a note in the detail log, never a silent skip.

### Removable Volumes (TCC)

launchd-spawned processes on this Mac cannot read `/Volumes/External` (removable-volume privacy
denied `CoreSimulatorService` and `npx` the same way), and the lanes are symlinks onto it.  The job
therefore probes first.  If it cannot read a lane it backs up everything it can reach, logs
`status=partial warn=removable-volumes-denied grant=<executable>`, tags the snapshot `partial`, and writes
the instruction to the detail log.  A lane link onto a volume that is not mounted is the same:
`warn=volume-missing`.

**Grant the right thing.**  macOS checks the *running image*, not the launcher you named.  Homebrew's
`python3.14` is a 34 KB stub that execs `.../Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python`;
Apple's `/usr/bin/python3` runs `.../Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python`
(inside Xcode.app).  `python3 ~/apps/fleet-work-backup/fleet_work_backup.py image` prints it, and
`install.py` prints it for the plist's interpreter.  In System Settings > Privacy & Security >
Removable Volumes (or Full Disk Access) press `+` and choose that `Python.app` (Command-Shift-G in the file
picker takes a path).  Apple's interpreter is the default because it is Apple-signed:  its grant survives
updates.  Homebrew's is ad-hoc signed, so the grant is tied to that exact build and is lost on every
`brew upgrade` (`python@3.14` upgraded Oct 9);  `install.py --interpreter /opt/homebrew/bin/python3` selects it.

**Verify under launchd, not Terminal.**  A command typed in Terminal inherits Terminal's grants, so
`check-access` there says `ok` whatever the schedule can do (it now says so itself).  This asks the schedule's
own context:

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py check-access --launchd

It writes a one-shot job (label `com.jay.fleet-work-backup.check`, RunAtLoad, the same interpreter and script as
the schedule, `check-access` only, never a backup), reads its result, and boots it out again.  Its stubbed
plumbing is tested;  whether macOS grants the volume to a launchd job is only known on the Mac, which is why this
is an install step.  `git` children inherit the interpreter's grant;  if one does not, set
`FLEET_WORK_BACKUP_GIT` in the plist to a git binary that has it.  The staging folder is on the internal
disk, so restic itself never reads `/Volumes/External`.

## Logs

- `~/Library/Logs/fleet-work-backup.log`:  one line per run, key=value, 12-hour local time.
  `status` is `ok`, `partial` (the snapshot is missing something:  a denied path, an unmounted volume,
  files deferred by the gate, files restic could not read), `skip` (`reason=` load, memory pressure, swap,
  lock-held, low-disk) or `error` (`reason=` restic-missing, missing-credentials with the NAMES,
  wrong-password, restic-backup-failed, timeout, exception).  Fields:  `checkouts groups work unpushed
  stashes dirty files staged dropped deferred regex_only gate large blocked snapshot added forget prune
  check elapsed warn grant last_ok`.  `gate` is `gitleaks`, `gitleaks-error` or `regex-only`;  `warn` is a
  comma list (`gitleaks-missing`, `gitleaks-deferred`, `gitleaks-error`, `volume-missing`,
  `removable-volumes-denied`, `bundle-scan-partial`, `allow-entry-stale`, `restic-unreadable-files`, ...).
- `~/Library/Logs/fleet-work-backup.detail.log`:  per run, the paths the gate dropped (with reason and the
  `allow-line` to paste), the files it deferred, large files skipped, blocked paths, notes.  Paths only.
- `.out.log` / `.err.log`:  launchd stdout and stderr.

## Commands

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py run --dry-run --json /tmp/x.json
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py init            # once, explicit
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py image           # the executable to grant
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py check-access --launchd
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py snapshots
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py restic -- ls latest --tag fleet-work

`--dry-run` builds the staged tree in `stage-dry`, prints counts and sizes, uploads nothing, and takes
neither the lock nor the pressure gate.  A scheduled run never runs `restic init`.  `--skip-gitleaks` is
for a dry run only (a faster report);  a real run refuses it.

## Install

Order matters;  nothing here runs by itself:

1. `brew install restic` (or `install.py --install-restic`).
2. Create the bucket-scoped B2 application key **limited to the prefix `fleet-work-backup/`**
   (capabilities `listFiles readFiles writeFiles deleteFiles listBuckets`), and add the lifecycle rule on
   that prefix (days from hiding to deleting, 7 to 30).
3. Write `RESTIC_FLEET_WORK_PASSWORD`, `B2_FLEET_WORK_KEY_ID` and `B2_FLEET_WORK_APPLICATION_KEY` to
   Infisical (project `AI Fleet Coordinator`, `prod`), generating the password without printing it.
4. `install -m 755 scripts/infisical-secrets-sync.sh ~/bin/` and wait one tick;  `~/.secrets/infisical-sync.log`
   shows `updated <KEY> (new_len=N)` for each.
5. `python3 scripts/fleet-work-backup/install.py --skip-launchd`, then
   `python3 ~/apps/fleet-work-backup/fleet_work_backup.py init`, then add the recovery key
   (`... restic -- key add`) and keep its password outside Infisical.
6. `python3 scripts/fleet-work-backup/install.py` (loads the LaunchAgent;  refuses while a prerequisite is
   missing;  prints the `Python.app` to grant).
7. Grant Removable Volumes to that `Python.app`, then `python3 ~/apps/fleet-work-backup/fleet_work_backup.py
   check-access --launchd` until it says `ok under launchd`.
8. Optionally `run --ignore-pressure` once by hand;  review the detail log and fill `allow.txt`.
9. Update the live `~/apps/MAC-LOCAL-PROCESSES.md` and its Apple Note, and add `com.jay.fleet-work-backup`
   to `mac-process-watch.sh`.

## Tests

    python3 scripts/fleet-work-backup/test_fleet_work_backup.py

Fake repositories in a temp home:  an unpushed commit, a landed branch, a repo with no remote, a repo whose
only remote is a local path, a detached `HEAD`, two stashes, a dirty linked worktree (also one outside the
scanned folders), tracked and untracked files, tracked and stashed binary files, an ignored file, and
secret-looking names and contents, plus a live WAL-mode SQLite file.  The remotes look like GitHub URLs
(served from local bare repos through `insteadOf`) and restores clone through `file://`, so a bundle that
needed a commit GitHub no longer serves would fail here.  restic, gitleaks and launchctl are stubs;  where
`restic` is installed (CI installs it) a real init, backup, check and restore round trip runs against a local
repository.  The RESTORE.md git steps are exercised, and a test checks that the `.git` directories are
unchanged by a run.  Runs in CI (ubuntu) and under both Python 3.9 and 3.14.
