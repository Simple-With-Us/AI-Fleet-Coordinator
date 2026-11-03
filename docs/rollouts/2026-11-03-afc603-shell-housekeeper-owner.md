# Shell Housekeeper Owner Publication (AFC-603)

Standalone `mac-auto-cleanup.sh` now publishes the shared five-key owner record under the existing sibling guard before starting cleanup.  The record names the long-lived shell PID, retains the local C-locale process start token required by the reader-first rollout, and is atomically replaced at mode `0600` with an mtime strictly newer than the lock directory.

The guarded acquisition fingerprints the previous owner path before changing the lock and refuses publication when that identity changes.  Publication failures abort cleanup, preserve unknown or foreign state, and leave the lock for conservative recovery rather than starting an unrecorded run.

Release checks both the acquired directory inode and the exact published record before removing the empty lock and then its owner file.  A changed or unreadable record is preserved with the lock, and a failed directory removal keeps the owner record for dead-holder recovery.

An invocation inheriting `HOUSEKEEPER_LOCK_OWNER=1` neither publishes nor removes its parent's owner record or lock.  Controlled termination waits for foreground cleanup work and tracked shell jobs before releasing the mutex.

Verification uses the extracted lock block under temporary homes, never the full cleanup script.  Cases cover exact shape, shell identity, local start token, mode and timestamp bounds, matching release, inherited ownership, foreign or malformed release metadata, failed publication, foreign publication during acquisition, and termination while a foreground child is running.

The existing shell and watcher lock suites retain their takeover and reader-agreement coverage.  This change does not switch writers to UTC or alter scheduler cadence.

Deployment and process-inventory updates are tracked by the parent AFC-603 lane; this source change does not itself modify a live script, lock, LaunchAgent, or runtime setting.
