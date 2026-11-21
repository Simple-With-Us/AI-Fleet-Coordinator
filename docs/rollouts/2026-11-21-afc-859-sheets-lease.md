# AFC-859 Operator Sheets Drain Lease

## Summary

The Sheets drain now serializes writers with a kernel `flock` held by the Node process on a persistent owner-only lock file.  It no longer unlinks the lock inode.  Existing numeric PID markers remain protected during mixed-version rollout: a live or unprobeable PID is treated as an active legacy writer, and a stale PID is converted only after the new process acquires the kernel lock.  Only the invocation that creates a new inode with `O_EXCL` may initialize its empty file; an existing empty inode is ambiguous and remains untouched for operator reconciliation.

The installer and test documentation now state that the old PID-only runners must be quiescent before promoting this implementation, because their empty-file initialization window cannot be proven safe by the new protocol.

## Files

- `scripts/simple_tools/operator_sheets_drain.mjs` — persistent, validated lock inode and nonblocking helper-based `flock` acquisition.
- `scripts/simple_tools/thread_registry_export.mjs` — manual writer closes its lease without unlinking the shared lock inode.
- `scripts/test_operator_sheets_drain.mjs` and `scripts/test_thread_registry_export.mjs` — multiprocessing exclusion, initialization race, ambiguous-empty refusal, crash release, inode persistence, symlink/mode/content refusals, legacy PID compatibility and helper failure cases.
- `docs/protocols/operator-sheets-export.md` — lease behavior and rollout precondition.
- `STATUS.md` — source and runtime boundary.

## Verification

Verified with `node --test scripts/test_operator_sheets_drain.mjs scripts/test_thread_registry_export.mjs`: 82/82 passed.  The review follow-up covers ambiguous empty inodes, arbitrary content preservation, and the manual CLI's persistent inode.  No runtime install, unit restart or arming change was performed.

## Follow-Up

After both Sheets source changes are merged, install them together through the approved code-only path.  First confirm old PID-only runners are quiescent.  An existing empty lock inode or a creator crash before marker initialization needs operator reconciliation; the runner does not reclaim it automatically.  Keep runtime installation and delivery arming as separate gates.
