# Restoring from fleet-work-backup

Everything here reads the restic repository with the job's own credentials.  `fleet_work_backup.py
restic -- <args>` runs restic with that repository, password and key without putting any of them in
your shell:

    FWB="python3 /Users/jay/apps/fleet-work-backup/fleet_work_backup.py"
    $FWB snapshots                       # list this job's snapshots (tag fleet-work)

If the Mac itself is gone, the password is in Infisical (project `AI Fleet Coordinator`, `prod`,
`RESTIC_FLEET_WORK_PASSWORD`) and the bucket key in `B2_FLEET_WORK_KEY_ID` /
`B2_FLEET_WORK_APPLICATION_KEY`;  put them in the environment as `RESTIC_PASSWORD`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION=eu-central-003` and
`RESTIC_REPOSITORY=s3:https://s3.eu-central-003.backblazeb2.com/<B2_FLEET_BUCKET_NAME>/fleet-work-backup`,
then call `restic` directly.

## 1. Pull a snapshot into a scratch folder

Never restore over live folders.  Restore to a scratch target and copy out what you need.

    $FWB restic -- restore latest --tag fleet-work --target /Users/jay/restore-fwb
    export STAGE=/Users/jay/restore-fwb/Users/jay/Library/Caches/fleet-work-backup/stage
    ls $STAGE            # MANIFEST.json  README-RESTORE.txt  files/  git/

A smaller pull:  `... restore latest --tag fleet-work --target ... --include '*/stage/files/apps/mac-collab'`,
or an older snapshot id from `snapshots` in place of `latest`.  `$FWB restic -- ls latest --tag
fleet-work` lists what a snapshot holds.

## 2. Non-git files

`files/` mirrors paths relative to the home directory.

    cp -p $STAGE/files/apps/AGENT-SYNC.md /Users/jay/apps/
    cp -Rp $STAGE/files/apps/mac-collab /Users/jay/apps/mac-collab.restored

`mac-collab/findings.db` is a consistent snapshot (SQLite online backup);  stop the board server
(`mac-collab-server.py`) before moving it into place, and delete any stale `findings.db-wal` and
`findings.db-shm` next to the target.

## 3. Git work

Find the repository.  Each `git/<repo>-<hash>/group.json` names the original common dir:

    python3 - <<'EOF'
    import glob, json, os
    for g in sorted(glob.glob(os.environ["STAGE"] + "/git/*/group.json")):
        d = json.load(open(g)); print(os.path.basename(os.path.dirname(g)), d["common_dir"], len(d["unpushed"]), "refs", len(d["stashes"]), "stashes")
    EOF

Use `G=$STAGE/git/<repo>-<hash>` below.

### Unpushed commits (`unpushed.bundle`)

Work in a clone that has the remote's history, because the bundle lists remote commits as
prerequisites.

    git clone <remote url from group.json> /Users/jay/restore/<repo> && cd /Users/jay/restore/<repo>
    git bundle verify $G/unpushed.bundle
    git fetch $G/unpushed.bundle 'refs/heads/*:refs/restored/heads/*' 'refs/tags/*:refs/restored/tags/*'
    git branch restored-main refs/restored/heads/main      # one per branch you want back

A repository with no remote (`"no_remote": true` in `group.json`) has the whole history in the
bundle:  `git clone $G/unpushed.bundle /Users/jay/restore/<repo>`.

### A stash (`stashes/NN/`, NN is the `stash@{N}` index)

`stash.json` holds the base sha the stash was made on.  Tracked changes are in `tracked/`, files the
stash had untracked (`git stash -u`) in `untracked/`.

    cd /Users/jay/restore/<repo>
    git checkout <base from stash.json>              # the commit must be in the clone or the bundle
    git apply $G/stashes/01/tracked/*.patch
    git apply $G/stashes/01/untracked/*.patch 2>/dev/null || true
    git stash push -u -m "restored stash 01"          # optional:  put it back on the stash list

### Uncommitted work in a worktree (`worktrees/<name>-<hash>/`)

`worktree.json` records `path`, `head` and `branch`.

    cd /Users/jay/restore/<repo>
    git worktree add /Users/jay/restore/<name> <head from worktree.json>
    cd /Users/jay/restore/<name>
    git apply $G/worktrees/<name>-<hash>/patches/*.patch
    cp -Rp $G/worktrees/<name>-<hash>/untracked/. .

A detached `HEAD` that no remote had is in `head.bundle` beside `worktree.json`:  fetch it the same
way as the main bundle (`git fetch head.bundle HEAD:refs/restored/detached-head`).

## 4. What is not in the backup

Secrets, by design:  anything with a strict secret name (`.env`, keys, `zuliprc`), any file the
content scan matched, and the handoff file.  Re-create those from Infisical.  Git-ignored files
(`node_modules`, build output) are rebuilt from the checkout.  The detail log
(`~/Library/Logs/fleet-work-backup.detail.log`) lists, per run, every path the gate dropped.

## 5. Verify

    $FWB restic -- check                           # repository structure
    $FWB restic -- check --read-data-subset=5%     # and a slice of the data
