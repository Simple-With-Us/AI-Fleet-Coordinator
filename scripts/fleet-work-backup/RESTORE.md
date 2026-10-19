# Restoring from fleet-work-backup

Everything here reads the restic repository with the job's own credentials.  `fleet_work_backup.py
restic -- <args>` runs restic with that repository, password and key without putting any of them in
your shell:

    FWB="python3 $HOME/apps/fleet-work-backup/fleet_work_backup.py"
    $FWB snapshots                       # list this job's snapshots (tag fleet-work)

If the Mac itself is gone, the password is in Infisical (project `AI Fleet Coordinator`, `prod`,
`RESTIC_FLEET_WORK_PASSWORD`), the bucket name in `B2_FLEET_WORK_BUCKET_NAME` and the bucket key in
`B2_FLEET_WORK_KEY_ID` / `B2_FLEET_WORK_APPLICATION_KEY`;  put them in the environment as `RESTIC_PASSWORD`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION=eu-central-003` and
`RESTIC_REPOSITORY=s3:https://s3.eu-central-003.backblazeb2.com/<B2_FLEET_WORK_BUCKET_NAME>/fleet-work-backup`,
then call `restic` directly.  If Infisical is out of reach too, use the recovery key the owner keeps
outside Infisical (`restic key add` at init, README section Credentials):  its password opens the same
repository.

## 1. Pull a snapshot into a scratch folder

Never restore over live folders.  Restore to a scratch target and copy out what you need.

    $FWB snapshots                       # note the id;  a snapshot tagged "partial" was missing something (see the run log)
    $FWB restic -- restore latest --tag fleet-work --target $HOME/restore-fwb
    export STAGE=$HOME/restore-fwb$HOME/Library/Caches/fleet-work-backup/stage
    ls $STAGE            # MANIFEST.json  README-RESTORE.txt  files/  git/

A smaller pull:  `... restore latest --tag fleet-work --target ... --include '*/stage/files/apps/AGENT-SYNC.md'`,
or an older snapshot id from `snapshots` in place of `latest`.  `$FWB restic -- ls latest --tag
fleet-work` lists what a snapshot holds.  `latest` can be a partial snapshot;  when the work you want
is older, name the snapshot id.

## 2. Non-git files

`files/` mirrors paths relative to the home directory.

    cp -p $STAGE/files/apps/AGENT-SYNC.md $HOME/apps/

`mac-collab` was retired on 2026-10-19 (AFC-363).  Only a snapshot made before that day holds
`files/apps/mac-collab` (the old board database, `findings.db`, a consistent SQLite online backup);
the live copy is `~/apps/_retired/mac-collab-20261019/findings.db`.  There is no board server to stop
or restart any more.  If you restore an old database, delete any stale `findings.db-wal` and
`findings.db-shm` next to the target.

## 3. Git work

Find the repository.  Each `git/<repo>-<hash>/group.json` names the original common dir, the remotes,
the bound the bundles were cut against (the default branch of the network remote) and the bundles:

    python3 - <<'EOF'
    import glob, json, os
    for g in sorted(glob.glob(os.environ["STAGE"] + "/git/*/group.json")):
        d = json.load(open(g))
        print(os.path.basename(os.path.dirname(g)), d["common_dir"], "bound", d["bound"]["ref"] or "-",
              len(d["bundles"]), "bundles", len(d["unpushed"]), "refs", len(d["stashes"]), "stashes")
    EOF

Use `G=$STAGE/git/<repo>-<hash>` below.  This helper applies a folder of patches one at a time, so one
patch that no longer fits does not stop the others:

    apply_all() {   # apply_all <folder>;  3-way merge first, then leave .rej files for a patch that still fails
      for p in "$1"/*.patch; do
        [ -e "$p" ] || continue
        git apply -3 "$p" || git apply --reject "$p" || echo "NEEDS HAND: $p"
      done
      if [ -d "$1/binary" ]; then cp -Rp "$1/binary/." .; fi           # a binary file is stored whole, not as a patch
      if [ -f "$1/binary-deleted.txt" ]; then
        while IFS= read -r f; do rm -f -- "$f"; done < "$1/binary-deleted.txt"
      fi
    }

And this one fetches a repository's bundles into the clone you are in, branch bundles first, then
`anchors.bundle` (whose prerequisites can be commits the branch bundles carry):

    fetch_bundles() {   # fetch_bundles <G>
      for b in "$1"/bundles/*.bundle; do
        [ -e "$b" ] || continue
        if git bundle verify -q "$b"; then
          git fetch -q "$b" 'refs/heads/*:refs/restored/heads/*' 'refs/tags/*:refs/restored/tags/*'
        else
          echo "CANNOT VERIFY: $b"
        fi
      done
      if [ -f "$1/anchors.bundle" ]; then
        if git bundle verify -q "$1/anchors.bundle"; then
          git fetch -q "$1/anchors.bundle" 'refs/fwb/*:refs/fwb/*'
        else
          echo "CANNOT VERIFY: $1/anchors.bundle"
        fi
      fi
    }

### Unpushed commits (`bundles/*.bundle`) and the commits patches sit on (`anchors.bundle`)

Each bundle holds one group of branches that share commits, and every commit that group has which the
network remote's default branch does not (so also the commits of a pushed branch that was later deleted
on GitHub).  Its prerequisites are commits on the default branch, which GitHub keeps.

`anchors.bundle` holds the commits that stash and worktree patches apply to when nothing else holds them:
the base of a stash made on a branch that was later rebased or deleted, and the `HEAD` of a dirty lane on a
branch that landed by squash merge (GitHub deletes those).  Its refs are `refs/fwb/stash/NN` and
`refs/fwb/worktree/<name>-<hash>`;  `stash.json` and `worktree.json` name theirs under `anchor`, and
`"state": "covered"` there means the commit is on the default branch or in a branch bundle.  Only the base
commit is anchored, never the stash commit itself, so nothing the gate dropped from a stash rides along.

Work in a fresh clone of the remote:

    git clone <remote url from group.json> $HOME/restore/<repo> && cd $HOME/restore/<repo>
    git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'    # belt and braces:  GitHub keeps pull request heads
    fetch_bundles $G
    git for-each-ref refs/restored/ refs/fwb/                        # every branch that was only on this Mac, and the anchors
    git branch restored-main refs/restored/heads/main                 # one per branch you want back

`bundle verify` names the missing commit when a prerequisite is not in the clone.  A flat lane cloned from
another checkout on this Mac (`"network_remotes": []` but `bound.via_url` set in `group.json`, and a
`local-remote-proxied` note in the detail log) is bounded by a commit on that checkout's GitHub default branch (`bound.sha`):  clone
`bound.via_url` (the GitHub URL) instead of the remote the lane lists.  A repository whose only remotes are
local paths or bundle files with no GitHub behind them (`"network_remotes": []`, no `via_url`, and a
`local-only-remotes` note) has its whole history in the bundle:
`git clone $G/bundles/<file>.bundle $HOME/restore/<repo>`.  A repository with no remote at all
(`"no_remote": true`) is the same.

### A stash (`stashes/NN/`, NN is the `stash@{N}` index)

`stash.json` holds the base sha the stash was made on.  Tracked text changes are in `tracked/*.patch`,
tracked binary files whole in `tracked/binary/`, and files the stash had untracked (`git stash -u`) in
`untracked/`.

    cd $HOME/restore/<repo>                                            # after fetch_bundles $G
    git checkout <base from stash.json> || git checkout origin/main    # the base is on GitHub or in refs/fwb;  else 3-way does the work
    D=$G/stashes/01
    apply_all $D/tracked
    apply_all $D/untracked
    git stash push -u -m "restored stash 01"                           # optional:  put it back on the stash list

### Uncommitted work in a worktree (`worktrees/<name>-<hash>/`)

`worktree.json` records `path`, `head` and `branch`.  Text changes are `patches/*.patch`, a changed binary
file is stored whole in `patches/binary/` (a deleted one is listed in `patches/binary-deleted.txt`), and
untracked files are in `untracked/`.

    cd $HOME/restore/<repo>                                            # after fetch_bundles $G
    git worktree add $HOME/restore/<name> <head from worktree.json> || git worktree add $HOME/restore/<name> origin/main
    cd $HOME/restore/<name>
    apply_all $G/worktrees/<name>-<hash>/patches
    cp -Rp $G/worktrees/<name>-<hash>/untracked/. .

The recorded `head` can be a commit GitHub no longer serves (a branch deleted after a squash merge);
`anchors.bundle` carries it then (`worktree.json` says `"state": "bundled"` under `anchor`).  If an anchor was
dropped by the gate (`"state": "dropped"`, named in the detail log), the pull request refs fetched above usually
still have it, and otherwise `git apply -3` rebuilds the change on top of `origin/main`.

A detached `HEAD` that no network remote had is in `head.bundle` beside `worktree.json`:  fetch it the
same way (`git fetch head.bundle HEAD:refs/restored/detached-head`).

## 4. What is not in the backup

Secrets, by design:  anything with a strict secret name (`.env`, keys, `zuliprc`, `auth.json`, `.dev.vars`,
`*.tfstate`), any file the content scan matched, and any secrets file.  Re-create those from Infisical.
Git-ignored files (`node_modules`, build output) are rebuilt from the checkout.  Runtime session stores
(`acp-home/sessions`, `acp-home/memtrace`, `fleet-rag/mined/chat`) are skipped by default, but the repo plist
turns that off and skips `memtrace` by name, so on this install `acp-home/sessions` and `fleet-rag/mined/chat`
are in the backup and `acp-home/memtrace` is not;  a file over 200 MB is listed instead of copied.  The detail log (`~/Library/Logs/fleet-work-backup.detail.log`) lists,
per run, every path the gate dropped, every file it left out for a later run (`deferred`), and the large
files.

## 5. Verify

    $FWB restic -- check                           # repository structure
    $FWB restic -- check --read-data-subset=5%     # and a slice of the data
