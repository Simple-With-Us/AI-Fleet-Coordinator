# Housekeeper owner record: one schema, and a reused pid that cannot pass for a live holder (AFC-554)

## Context and objective

The housekeeper lock is shared state with two readers in this repository: the Python watcher (`scripts/mac-resource-watch.py`) and the embedded reader in `scripts/mac-auto-cleanup.sh`).  Kody reviewed the merged AFC-479 pull request (#856) after the merge and posted three findings, and all three are live in `main`.  The objective of this unit is to fix all three in code, and to make the two readers of one protocol unable to drift apart again.

## Changes made

The owner record gains a fifth key, `start`: the start time of the holder's process, read from the kernel through `ps` and kept as an opaque string that is compared and never parsed.  The C locale is forced when it is read, so the writer and the reader produce the same value for the same process on any machine and in any locale.  The shell reader now enforces the same schema the watcher enforces, value for value, and both readers compare the start time: a pid whose start time does not match the record names a different process, so the holder is gone and the lock is taken over at once instead of waiting out the whole stale window.  The watcher test stubs the hostname with a synthetic value and restores the original lookup after the host-sensitive cases.

The class of the first finding is two implementations of one protocol drifting apart, so the fix is not only the schema check: a new case runs one table of owner records through both readers and fails when they disagree.  The table covers a good record, a missing key, an unknown key, a pid that is a string, a pid that is a bool, an empty start, another host, an owner older than the lock beside it, a dead pid, a live pid and a reused pid.

Files touched:

- `scripts/mac-resource-watch.py`: the owner schema, the parser, the owner check and the writer.
- `scripts/mac-auto-cleanup.sh`: the embedded owner reader.
- `scripts/test-mac-auto-cleanup.sh`: the fixture, a reused-pid case and the cross-implementation agreement table.
- `scripts/test-mac-resource-watch.sh`: the hostname stub, the fixture and a reused-pid case.
- `docs/rollouts/2026-10-26-afc554-housekeeper-owner-record.md`: this note.
- `STATUS.md`: the entry for this unit.

## Decisions and trade-offs

The start time is compared as an opaque string rather than as a parsed timestamp.  A parsed date is locale dependent, and a token that differed only because the writer and the reader ran under different locales would read as a different process, which would take a live holder's lock away from it.  A start time that cannot be read at all is treated as no evidence: the record cannot speak for the lock and the age rule decides.

The owner record is not written at all when the start time cannot be read, because a record without it cannot tell the holder from a reused pid.  A missing owner file is an ordinary state: the shell holder writes none at all and recovers by the age rule, so the failure mode is the one this protocol already had.

The two readers stay separate implementations.  A shared module would have been the tidier shape, but the cleanup script is deployed on its own to `~/apps/mac-auto-cleanup.sh` and the watcher on its own to `~/apps/mac-resource-watch.py`, so a new sibling file would have to be deployed with them and would be one more thing that can be out of step.  The agreement table is the guard instead, and the table reads the start time itself rather than asking either implementation, so it pins the format the protocol publishes instead of following the code.

A partial deployment is safe in both directions.  An old shell reader accepts the new five-key record, because it does not check the key set, and it falls back to the age rule while the pid is alive.  A new watcher rejects an old four-key record as malformed and falls back to the age rule too.  The fast recovery is lost until both copies are updated, and nothing is taken away from a live holder.

## Verification state

Commands run, from the lane:

- `bash scripts/test-mac-auto-cleanup.sh` -> OK, including the new agreement table.
- `bash scripts/test-mac-resource-watch.sh` -> OK.
- `node --test workers/alert-ingress/src/*.test.js` -> 145 pass, 0 fail.
- `shellcheck -S warning` on the three changed shell files -> the same warnings as `origin/main` and no new one.
- The watcher test also passes on `ubuntu:24.04` with GNU ps, which is the image the CI `test` job runs it on, so the `ps` path is proven on the BSD and the GNU implementation.

The new cases fail against the previous code.  The cleanup suite fails on the reused-pid case ("the housekeeper owner check answered 'no' where 'yes' was expected"), and the watcher suite fails because the old parser rejects the five-key record the protocol now publishes.  Both were run by putting the `origin/main` copies of the two scripts back under the new tests.

## Next steps and blockers

Merge this branch, then replace `/Users/jay/apps/mac-auto-cleanup.sh` and `/Users/jay/apps/mac-resource-watch.py` with the merged copies in the same change, because the owner schema is exact and the two readers have to agree on it.  Update the two rows and the changelog in `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` and refresh the pinned Apple Note in the same change.

No owner action is needed for the code itself.

## Zero-code findings

Nothing in this unit is a document-only change: every finding was a defect in the running scripts, and all three are fixed in code.
