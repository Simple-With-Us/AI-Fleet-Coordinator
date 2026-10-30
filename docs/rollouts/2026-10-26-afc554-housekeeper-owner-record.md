# Housekeeper owner record: one schema, and a reused pid that cannot pass for a live holder (AFC-554)

## Context and objective

The housekeeper lock is shared state with two readers in this repository: the Python watcher (`scripts/mac-resource-watch.py`) and the embedded reader in `scripts/mac-auto-cleanup.sh`).  Kody reviewed the merged AFC-479 pull request (#856) after the merge and posted three findings, and all three are live in `main`.  The objective of this unit is to fix all three in code, and to make the two readers of one protocol unable to drift apart again.

## Changes made

The owner record gains a fifth key, `start`: the holder's process start time, read from the kernel through `/bin/ps`.  Readers now request a UTC-form token with `TZ=UTC0` while preserving `LC_ALL=C`; writers keep the established local-time token during this reader-first rollout.  Exact UTC tokens identify the same process directly.  For records written before the timezone change, readers parse the C-locale civil timestamp and compare its wall-clock-as-UTC epoch with the current UTC process-start epoch, accepting a difference of at most 24 hours only when that process started no later than the record's `acquired_at`.  That acquisition-time bound prevents a later process that reused the pid from being mistaken for the holder.  If either token cannot be read or parsed, the owner record does not prove the holder gone and the existing age rule decides.  The shell reader enforces the same schema as the watcher.

The class of the first finding is two implementations of one protocol drifting apart, so the fix is not only the schema check: a new case runs one table of owner records through both readers and fails when they disagree.  The table covers a good record, a missing key, an unknown key, a pid that is a string, a pid that is a bool, an empty start, an overlong start, another host, an owner older than the lock beside it, a dead pid, a live pid, a reused pid and a pid that cannot be signalled because it belongs to another user.

Files touched:

- `scripts/mac-resource-watch.py`: the owner schema, the parser, the owner check and the writer.
- `scripts/mac-auto-cleanup.sh`: the embedded owner reader.
- `scripts/test-mac-auto-cleanup.sh`: the fixture, a reused-pid case and the cross-implementation agreement table.
- `scripts/test-mac-resource-watch.sh`: the hostname stub, the fixture and a reused-pid case.
- `docs/rollouts/2026-10-26-afc554-housekeeper-owner-record.md`: this note.
- `STATUS.md`: the entry for this unit.

## Decisions and trade-offs

Exact UTC start tokens are compared as strings.  A differing token is parsed only as the fixed English civil form emitted under `LC_ALL=C`, so it can identify a legacy local-time record after the host timezone changes.  The bounded epoch comparison plus `acquired_at` check prevents an uncertain parse or a reused pid from being treated as the same live holder.  A start time that cannot be read or parsed is treated as no evidence: the record cannot speak for the lock and the age rule decides.

This is the reader-compatibility phase.  Both writers still publish local-time tokens; switch either writer to UTC only after every reader that can encounter its owner records accepts both the exact UTC form and the legacy local form.

The owner record is not written at all when the start time cannot be read, because a record without it cannot tell the holder from a reused pid.  A missing owner file is an ordinary state: the shell holder writes none at all and recovers by the age rule, so the failure mode is the one this protocol already had.

The two readers stay separate implementations.  A shared module would have been the tidier shape, but the cleanup script is deployed on its own to `~/apps/mac-auto-cleanup.sh` and the watcher on its own to `~/apps/mac-resource-watch.py`, so a new sibling file would have to be deployed with them and would be one more thing that can be out of step.  The agreement table is the guard instead, and the table reads the start time itself rather than asking either implementation, so it pins the format the protocol publishes instead of following the code.

A partial deployment is safe in both directions.  An old shell reader accepts the new five-key record, because it does not check the key set, and it falls back to the age rule while the pid is alive.  A new watcher rejects an old four-key record as malformed and falls back to the age rule too.  The fast recovery is lost until both copies are updated, and nothing is taken away from a live holder.

## Second round: two more reader divergences, found by the review of the fix

The review of this pull request found two more ways the two readers could reach opposite conclusions about the same owner record, and both were in the shell reader.

The shell reader did not enforce the length bound on the `start` token that the watcher enforces.  A record with an overlong token was refused by the watcher and trusted by the shell, so a dead pid behind such a record was taken over by one implementation and left alone by the other.  The shell reader applies the same bound now, the watcher test pins the bound on the watcher side as well, and the agreement table carries an overlong start case.

The shell reader also answered a permission error as "not gone" and stopped there, while the watcher treated the same error as "the process exists" and went on to compare the start time.  A pid that belongs to another user cannot be signalled, and the shell's answer left a reused pid looking like a live holder for the whole stale window.  The shell reader carries on to the start time now, exactly as the watcher does, so the two agree.

The permission error is exercised with a stub rather than with a real foreign process, so the case runs the same on a developer machine and in CI:  `sitecustomize` raises `PermissionError` for the pid under test, and Python imports it at startup, so the embedded reader reaches the stub without knowing anything about it.

The second round was shown the same way.  With the shell reader from the first commit in place, the agreement table fails and names both divergences:  "an overlong start: the shell said True and the watcher said False" and "a pid that is not ours to signal: the shell said False and the watcher said True".

## Verification state

Commands run, from the lane:

- `bash scripts/test-mac-auto-cleanup.sh` -> OK, including the agreement table and its thirteen cases.
- `bash scripts/test-mac-resource-watch.sh` -> OK, including the new overlong start cases.
- `node --test workers/alert-ingress/src/*.test.js` -> 145 pass, 0 fail.
- `shellcheck -S warning` on the three changed shell files -> the same warnings as `origin/main` and no new one.
- The watcher test also passes on `ubuntu:24.04` with GNU ps, which is the image the CI `test` job runs it on, so the `ps` path is proven on the BSD and the GNU implementation.

The new cases fail against the previous code.  The cleanup suite fails on the reused-pid case ("the housekeeper owner check answered 'no' where 'yes' was expected"), and the watcher suite fails because the old parser rejects the five-key record the protocol now publishes.  Both were run by putting the `origin/main` copies of the two scripts back under the new tests.

## Next steps and blockers

Merge this branch, then replace `/Users/jay/apps/mac-auto-cleanup.sh` and `/Users/jay/apps/mac-resource-watch.py` with the merged copies in the same change, because the owner schema is exact and the two readers have to agree on it.  Update the two rows and the changelog in `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` and refresh the pinned Apple Note in the same change.

No owner action is needed for the code itself.

## Zero-code findings

Nothing in this unit is a document-only change: every finding was a defect in the running scripts, and all three are fixed in code.
