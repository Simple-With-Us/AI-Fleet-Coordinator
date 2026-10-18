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
`local-only-remotes`), unless the remote is another checkout on this Mac that itself has a GitHub remote (a
flat lane cloned from `~/Code/X`):  then that checkout's GitHub default branch is the bound and the pushed line (for a clone that has
not fetched lately, the newest commit it shares with that branch;  `local-remote-proxied`, `bound.via_url` in
`group.json`).

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
detail log notes each as `skipped-by-default`;  `FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS=1` turns the whole
list off, all three together, there is no per-store switch).  **This install turns it off** (the plist sets
it, owner ruling, Jay, Oct 10, 2026), so the Grok ACP session history (`acp-home/sessions`) and the mined
fleet-rag chat logs (`fleet-rag/mined/chat`) are in the backup.  The third store, `acp-home/memtrace`, is put
back by name:  the plist also sets `FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS=memtrace`, and the only directory called
`memtrace` under `~/apps` and `~/Code` is that one (a new directory of that name anywhere would be skipped too).
The two stores' files still pass the name filter and the content and gitleaks secret gate, a file over 200 MB is
still skipped, and `*.log` files and directories named `logs` stay skipped.  `FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS=name1,name2` adds directory names without a code change.

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
   Slack, Stripe, Google, Anthropic, OpenAI, JWT, bearer headers, a JSON credential field such as
   `"refresh_token": "..."` with a high-entropy value, and `*_KEY` / `*_SECRET` /
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

   Unpushed commits are scanned with the same regex over one `git log -p --cc` pass (first 512 MB per
   scan) before they are bundled, commit by commit:  a hit is attributed to the commit that carries it,
   and only the branches that can reach that commit are dropped, so one branch holding a key-shaped
   string costs that branch and not the repository (each dropped ref is named in the detail log).  The
   scan covers the commits no network remote has;  a bundle also carries pushed-but-unmerged commits it is
   stacked on, and those are on GitHub already.  A longer history than the window is bundled anyway and the
   run line says `warn=bundle-scan-partial`.  Not covered:  gitleaks does not run over history.

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

Restic repository `s3:https://s3.eu-central-003.backblazeb2.com/<B2_FLEET_WORK_BUCKET_NAME>/fleet-work-backup`
(the Backblaze region the fleet's litestream config uses, so create the bucket there).  Tag `fleet-work`, host `jay-mac`, plus tag
`partial` on a snapshot that was missing something (an unmounted volume, a denied path, files deferred by
the gate).  Retention runs after every complete run, per host and tag set, so partial snapshots have their
own slots and never push complete ones out:  `--keep-hourly 24 --keep-daily 14 --keep-weekly 8
--keep-monthly 6`.  A partial run never runs `forget` or `prune` (`forget=skipped-partial` in its line), so a
week of runs without the lanes cannot age out the last good state.  `--prune` runs at most once a day and
`restic check` at most once a week, because list and delete calls are B2 transactions (board PD #388 is a
B2 request spike).

The bucket is **dedicated** to this job and private (owner decision, Oct 10, 2026):  it is not the shared fleet
bucket that `litestream.yml` replicates the board database into, and that bucket and its lifecycle rule are not
touched.  Its name comes from `B2_FLEET_WORK_BUCKET_NAME` only;  there is no fallback to the shared
`B2_FLEET_BUCKET_NAME`, because a fallback could point the backup at the wrong bucket.  The repository path stays
`<bucket>/fleet-work-backup`, and the application key **must** still be limited to that one bucket and the
file-name prefix `fleet-work-backup/`, so a leaked key reaches nothing else.  `init` and `install.py`
check that with read-only B2 calls (`b2_authorize_account` and `b2_list_buckets`, API v4) and refuse a key
that is not limited to them, lacks a capability restic needs, or holds key or bucket admin capabilities
(`--force` does not override that), and a lifecycle rule that hides files under the prefix by age
(`daysFromUploadingToHiding` would hide live restic packs).  A scheduled run checks the key too, because the sync
can bring a new one after the install:  a passing verdict is kept for a day per key, so a steady run costs two
read-only B2 calls a day, a hard problem refuses the run (`status=error reason=unsafe-b2-key`, the problems in the
detail log), and a missing hide-to-delete rule is `warn=b2-lifecycle-missing`.  The v4 response shape is tested against an offline
fake built from Backblaze's documentation;  a real response the check misreads fails closed (init and install
refuse) rather than letting an unscoped key through.  Two B2 facts that need a bucket setting,
not code:  over the S3 API a delete only hides a file version, so `restic prune` frees no space until the
bucket has a **lifecycle rule** with `daysFromHidingToDeleting` of 7 to 30 (which also leaves a soft-delete
window).  The dedicated bucket's rule is the whole-bucket one,
`{fileNamePrefix: "", daysFromHidingToDeleting: 14, daysFromUploadingToHiding: null}`, and the check accepts it
(a rule on `fleet-work-backup/` alone is accepted too).  A key that can delete lets any process on the Mac that
can read it erase the history, so keep the key in Infisical only (read through `fleet-secrets`) and consider Object Lock.

The staging folder is fixed at `~/Library/Caches/fleet-work-backup/stage` (restic picks the parent
snapshot by path, so a timestamped folder would break deduplication and retention).  Files are
cloned with `clonefile(2)` where the volume allows (free, and a point-in-time copy:  what the gate
scanned is what restic reads), copied otherwise, and the folder is removed after the run.  restic
runs with `--ignore-inode --ignore-ctime`, because a rebuilt folder has new inodes, at low CPU priority
(`nice 10`, two threads) and gets SIGINT, then a minute, before any kill, so its repository lock is released.

## Credentials

Read by the job from `~/.secrets/global-api-keys` (or the environment) today, moving to the environment or `fleet-secrets` under OPS-818, into the restic
subprocess environment only.  Names, never values:

| Name | Use |
|---|---|
| `B2_FLEET_WORK_BUCKET_NAME` | the dedicated bucket (16 or more characters, because the sync's plausibility check rejects anything shorter) |
| `B2_FLEET_WORK_KEY_ID`, `B2_FLEET_WORK_APPLICATION_KEY` | a **bucket-scoped** B2 application key limited to the file-name prefix `fleet-work-backup/`.  Capabilities:  `listFiles`, `readFiles`, `writeFiles`, `deleteFiles` (prune needs it), and `listBuckets`. |
| `RESTIC_FLEET_WORK_PASSWORD` | the repository password |

The master key (`BACKBLAZE_MASTER_KEY_ID` / `BACKBLAZE_MASTER_APPLICATION_KEY`) is never used to back up,
init or install:  it has no prefix.  With `FLEET_WORK_BACKUP_ALLOW_MASTER=1` the restore commands (`restic`,
`snapshots`) may fall back to it for names the scoped key leaves empty.

All four names live in Infisical (project `AI Fleet Coordinator`, `prod`) and reach the handoff
file through the one-way `com.jay.infisical-secrets-sync` job:  `scripts/infisical-secrets-sync.sh`
has a `MAP` row for each (the password's is write-once, the other three follow Infisical), and the sync reads
exactly the rows in that array.  The password must be a
urlsafe token of at least 16 characters, with no whitespace and no backslash (the sync's plausibility
check rejects anything else, and its `awk -v` mangles backslashes).  Generate it with

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py new-password

which writes a 43-character urlsafe token to Infisical through the logged-in CLI and never prints it:  the
value reaches the CLI as a file path (`RESTIC_FLEET_WORK_PASSWORD=@<0600 file>`, removed afterwards), never in
argv, the CLI's output is discarded, and the value is read back and compared in memory (it reports
`matched` and the length).  It refuses when the handoff file, the environment or Infisical already has a
password, and a read it cannot make is a refusal, never "absent".

**The password is the only key to every snapshot, so it is never rotated in Infisical.**  The sync
overwrites the Mac's copy whenever Infisical changes, and a changed value there makes every run fail with
`reason=wrong-password` while the old one survives only in Infisical's version history.  A key changes only
with `restic key add` and `restic key remove` against the repository.  At `init`, add a second
**recovery key** and keep its password outside Infisical:  without it, losing Infisical while the Mac is gone
loses every backup.  The owner does this in Terminal, because the password is typed, not generated here:

1. In Apple Passwords, create an entry `fleet-work-backup restic recovery key` with a generated password.
2. `python3 ~/apps/fleet-work-backup/fleet_work_backup.py restic -- key add`:  restic asks for the new
   password twice without echoing it;  paste it from Apple Passwords.
3. Check that it opens the repository on its own (the file flag wins over the job's password):
   `pbpaste | python3 ~/apps/fleet-work-backup/fleet_work_backup.py restic -- --password-file /dev/stdin cat config >/dev/null && echo recovery key ok`,
   then clear the clipboard.
4. `install.py --check-repo` now counts two keys;  with one it reports the recovery key as missing.

The run line reports a wrong password as its own reason.

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
- **Not ok in a row:**  every run that makes no complete snapshot (error, partial, a pressure or low-disk skip;
  not `lock-held`, which means another run is working) is counted;  after 8 in a row the line carries
  `warn=no-complete-backup-for-about-24h` and every such line shows `last_ok=`.
- **Alert:**  at that point the job tells two places, each tried even when the other fails.  (1) One post through
  `agent-sync post --as CLAUDE` in the topic `AFC 29c02bcb fleet-work-backup` (`#agent-sync`), with the count, the
  last complete run, the run line (names and counts only) and where the logs are
  (`~/Library/Logs/fleet-work-backup.log`, `.detail.log`, `.out.log`, `.err.log`).  The post @-mentions each name in
  `FLEET_WORK_BACKUP_ALERT_TO` (a comma list of seats or people;  empty wakes nobody) and ends with
  `FLEET_WORK_BACKUP_ALERT_NOTE`, a standing instruction.  (2) When `FLEET_WORK_BACKUP_ALERT_DM_OWNER` is `1`,
  `true` or `yes`, a Zulip DM to the owner through `agent-sync dm --as CLAUDE --owner`, with the same text and
  without the note.  This install's plist sets all three (owner ruling, Jay, Oct 10, 2026:  DM him, and tell the
  Plumbers to investigate and fix it without asking him):  `ALERT_TO=BF-PLUMBER,GB-PLUMBER`, `ALERT_DM_OWNER=1`,
  and a note that says the first Plumber to reply "taking it" in the topic owns the fix and the other reviews it.
  The job posts as the CLAUDE bot, which is a different seat from both Plumbers, so its mention can wake them.
  BF-Plumber is woken by BotFleet's own Zulip listener and GB-Plumber through the Grok Bot routine webhook on the
  agent-sync server (live and connected as of Oct 10, 2026 10:34pm by `agent-sync status` on the server).  Neither
  has posted yet, so the wake path is unproven end to end.  BotFleet caps peer wakes (6 per bot per hour, 2 per
  topic per hour, and after 4 peer wakes in one topic with no message from Jay the topic stops waking that bot
  until Jay speaks there), so a failure that lasts for days can stop waking BF-Plumber in this topic.  **The owner
  DM is the delivery that is guaranteed.**  The alert goes out once when the count reaches 8 and again at most once
  a day while runs stay not ok (the mention and the note go with each).  When a complete run follows, the
  all-clear goes out to the owner by DM and to the topic as a post with **no** mention and no note, so good news
  spends no one's wake budget.  The CLI is `~/.local/bin/agent-sync` (launchd's `PATH` lacks it;
  `FLEET_WORK_BACKUP_AGENT_SYNC` overrides).  `FLEET_WORK_BACKUP_ALERT_TOPIC` and `FLEET_WORK_BACKUP_ALERT_SEAT`
  change the topic and the seat.  When neither send is delivered the detail log says `alert-not-posted` (naming
  `post`, `dm` or both);  when only one is, it says `alert-partly-posted` and names the other, and the alert
  counts as sent for that day, so a DM that failed beside a post that landed is not retried until tomorrow.
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
  lock-held, low-disk) or `error` (`reason=` restic-missing, missing-credentials with the NAMES, unsafe-b2-key,
  wrong-password, restic-backup-failed, timeout, exception).  Fields:  `checkouts groups work unpushed
  stashes dirty files staged dropped deferred regex_only gate large blocked snapshot added forget prune
  check elapsed warn grant last_ok`.  `gate` is `gitleaks`, `gitleaks-error` or `regex-only`;  `warn` is a
  comma list (`gitleaks-missing`, `gitleaks-deferred`, `gitleaks-error`, `volume-missing`,
  `removable-volumes-denied`, `bundle-scan-partial`, `allow-entry-stale`, `restic-unreadable-files`,
  `dangling-worktree` (a folder whose `.git` points at a gitdir that is gone, a lane pruned while its disk was
  unmounted:  its files are kept as plain files under `files/`, same filters), ...).
- `~/Library/Logs/fleet-work-backup.detail.log`:  per run, the paths the gate dropped (with reason and the
  `allow-line` to paste), the files it deferred, large files skipped, blocked paths, notes.  Paths only.
- `.out.log` / `.err.log`:  launchd stdout and stderr.

## Health and repair

Two verbs for the Plumbers (BF-Plumber runs them on this Mac, and the Mac-facts publisher reads `health --json`).
Neither changes the backup run path.

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py health --json
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py repair --reload

**`health --json`** is read-only.  It reads `state.json`, the newest line of the run log, the installed files and
`launchctl print gui/<uid>/com.jay.fleet-work-backup` (the one launchctl verb it uses), and it writes nothing, not
even a directory.  It never reads the credential names or values, the handoff file, restic or B2.  It always exits 0:
the verdict is in the output.  Without `--json` it prints the same facts as a few lines.

| Field | What it holds |
|---|---|
| `installed` | the stable copy `~/apps/fleet-work-backup/fleet_work_backup.py` and the plist `~/Library/LaunchAgents/com.jay.fleet-work-backup.plist` both exist (`script_present`, `plist_present` say which) |
| `loaded` | `launchctl print` exited 0 (`launchctl_rc` is the exit status;  127 means launchctl itself did not run) |
| `last_complete_at`, `last_complete_text`, `last_complete_age_hours` | `last_success` and `last_success_text` in the state file:  the last run that made a snapshot, which can be a `partial` one |
| `last_ok_text` | `last_ok_text`:  the last run with `status=ok` |
| `not_ok_in_a_row`, `skips_in_a_row` | `consecutive_not_ok`, `consecutive_skips` |
| `last_check_at`, `last_check_age_days` | `last_check`:  the last `restic check` that passed (weekly) |
| `alert_last_sent` | `last_alert` |
| `last_line`, `warn` | the newest run-log line (names and counts only, cut to 600 characters, and withheld whole if the backup's own secret scan matches it), and its `warn=` tokens |
| `disk_free_gb` | free space on the home volume |
| `plist_sha_ok` | the installed plist's sha256 equals the one `install.py` recorded (`plist_sha_recorded` says whether one was) |
| `verdict`, `reason` | below |

Times are epoch seconds, as the state file holds them, with the 12-hour text beside them where the run wrote one.

**Verdict.**  `unhealthy` when the job is not installed (the reason names the missing piece, and starts with
`not installed`), or installed but not loaded, or the last complete backup is more than 24 hours old.  `warn` when it is
more than 12 hours old.  Otherwise `ok`.  Before the first complete backup the wait counts from the install
(`plist_installed_at`, or the plist's own time).  Healthy needs no message;  the reason carries the age and, when the
verdict is not `ok`, how many runs in a row were not ok.

**`repair --reload`** is the one repair a Plumber may make without asking the owner (Jay, Oct 10, 2026:  fix backup
failures without asking him).  Only when the job is installed, **not loaded**, and `plist_sha_ok`, it runs
`launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/com.jay.fleet-work-backup.plist` and checks again.  Anything
else it does nothing and prints why:  already loaded (exit 0), not installed, no recorded hash, a plist that has
changed since `install.py` wrote it, a plist whose `Label` is not this job's, or launchctl not running (exit 1).  If
the bootstrap runs and the job is still not loaded, it says so (exit 2).  It never edits the plist, reads credentials,
calls restic or B2, or runs `install.py`.  A refusal is for a person:  a changed plist means someone must look at it
and re-run `install.py`, and anything that needs a secret (the restic key, a B2 key) or a Removable Volumes grant is an
owner step, not a repair.

`install.py` records the plist's sha256 (`plist_sha256`, plus `plist_installed_at`) in `state.json` each time it
installs or confirms the plist;  `--skip-launchd`, `--dry-run` and an install blocked by a missing prerequisite record
nothing.  A state file written before this verb existed has no hash, so `repair --reload` refuses until `install.py`
runs once more.

## Commands

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py run --dry-run --json /tmp/x.json
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py new-password    # once:  the restic password into Infisical, never printed
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py init            # once, explicit;  refuses an unscoped B2 key
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py image           # the executable to grant
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py check-access --launchd
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py health --json    # read-only:  verdict ok, warn or unhealthy, and why
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py repair --reload  # load the installed LaunchAgent if it is not loaded (hash-checked)
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py snapshots
    python3 ~/apps/fleet-work-backup/fleet_work_backup.py restic -- ls latest --tag fleet-work

`--dry-run` builds the staged tree in `stage-dry`, prints counts and sizes, uploads nothing, and takes
neither the lock nor the pressure gate.  A scheduled run never runs `restic init`.  `--skip-gitleaks` is
for a dry run only (a faster report);  a real run refuses it.

## Install

Order matters;  nothing here runs by itself:

1. `brew install restic` (or `install.py --install-restic`).
2. In the B2 console, create the dedicated private bucket (in the same region as the fleet's litestream bucket,
   `eu-central-003`, and with a name of 16 or more characters), give it the lifecycle rule
   `{fileNamePrefix: "", daysFromHidingToDeleting: 14, daysFromUploadingToHiding: null}` (days from hiding to
   deleting of 7 to 30 and **no** days from uploading to hiding), and create an application key with access to
   that one bucket only, type **Read and Write**, and the file-name prefix `fleet-work-backup/` (or with the `b2`
   CLI, an exact list:  `b2 key create --bucket <bucket> --name-prefix fleet-work-backup/ fleet-work-backup
   listBuckets,listFiles,readFiles,writeFiles,deleteFiles`).  It needs `listFiles readFiles writeFiles
   deleteFiles listBuckets`;  the check refuses key or bucket admin (`writeKeys`, `deleteKeys`, `writeBuckets`,
   `deleteBuckets`) and accepts the console preset's other read and write capabilities.
3. Write `B2_FLEET_WORK_BUCKET_NAME`, `B2_FLEET_WORK_KEY_ID` and `B2_FLEET_WORK_APPLICATION_KEY` to Infisical
   (project `AI Fleet Coordinator`, `prod`), and copy `scripts/fleet-work-backup/fleet_work_backup.py` to
   `~/apps/fleet-work-backup/` (`install.py --skip-launchd` does that), then run `fleet_work_backup.py
   new-password` for `RESTIC_FLEET_WORK_PASSWORD`.
4. `install -m 755 scripts/infisical-secrets-sync.sh ~/bin/` and wait one tick;  `~/.secrets/infisical-sync.log`
   shows `updated <KEY> (new_len=N)` for each.
5. `python3 ~/apps/fleet-work-backup/fleet_work_backup.py init` (it checks the key's scope and the lifecycle
   rule first), then add the recovery key (Credentials, above) and keep its password outside Infisical.
6. `python3 scripts/fleet-work-backup/install.py --check-repo` (loads the LaunchAgent and records the installed
   plist's sha256 for `repair --reload`;  refuses while a prerequisite is missing, and refuses an unscoped key even
   with `--force`;  prints the `Python.app` to grant).
7. Grant Removable Volumes to that `Python.app` in System Settings, then `python3
   ~/apps/fleet-work-backup/fleet_work_backup.py check-access --launchd` until it says `ok under launchd`.
8. Optionally `run --ignore-pressure` once by hand;  review the detail log and fill `allow.txt`.  The alert
   settings (`FLEET_WORK_BACKUP_ALERT_TO`, `FLEET_WORK_BACKUP_ALERT_DM_OWNER`, `FLEET_WORK_BACKUP_ALERT_NOTE`) are
   already in the repo plist (see Alert, above);  change them there, because `install.py` copies the repo plist
   over the live one whenever the two differ.
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
unchanged by a run.  `health` and `repair` run against a temp home with a fake `launchctl`:  one state file for
each verdict and threshold, a check that nothing is written and no credential, restic or B2 code is reached, and a
check that `repair --reload` bootstraps only when the job is not loaded and the plist hash matches (and that
`install.py` records that hash).  Runs in CI (ubuntu) and under both Python 3.9 and 3.14.
