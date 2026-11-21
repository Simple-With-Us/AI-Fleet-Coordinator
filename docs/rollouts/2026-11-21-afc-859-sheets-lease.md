# AFC-859 Operator Sheets Drain Lease

## Summary

The Sheets drain now serializes writers with a kernel `flock` held by the Node process on a persistent owner-only lock file.  It no longer unlinks the lock inode.  Existing numeric PID markers remain protected during mixed-version rollout: a live or unprobeable PID is treated as an active legacy writer, and a stale PID is converted only after the new process acquires the kernel lock.

The installer and test documentation now state that the old PID-only runners must be quiescent before promoting this implementation, because their empty-file initialization window cannot be proven safe by the new protocol.

## Files

- `scripts/simple_tools/operator_sheets_drain.mjs` — persistent, validated lock inode and nonblocking helper-based `flock` acquisition.
- `scripts/test_operator_sheets_drain.mjs` — multiprocessing exclusion, opener initialization race, crash release, inode persistence, symlink/mode refusals, legacy PID compatibility and helper failure cases.
- `docs/protocols/operator-sheets-export.md` — lease behavior and rollout precondition.
- `STATUS.md` — source and runtime boundary.

## Verification

The owner reports `node --test scripts/test_operator_sheets_drain.mjs` passed 58/58, including multiprocess and crash cases.  No runtime install, unit restart or arming change was performed.

## Follow-Up

After both Sheets source changes are merged, install them together through the approved code-only path.  First confirm old PID-only runners are quiescent.  Keep runtime installation and delivery arming as separate gates.
