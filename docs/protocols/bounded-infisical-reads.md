# Bounded Named Infisical Reads

`fleet_infisical.bounded_fetch_project` runs one logical named-read operation in a disposable process using the same canonical client.  The child performs the existing grant metadata checks, bounded concurrent named GETs, record provenance validation and decoding.  It can select only `named-fetch`; no mutation, arbitrary callable or source dispatch is available.

The parent starts a monotonic budget before launch, reserves the smaller of 100 ms or one fifth of the remaining budget for cleanup, and caps both private pipe directions independently.  Requests are limited to 128 KiB; the caller selects a bounded result cap.  The parent accepts only a complete version-one result from an exited child before expiry.  Partial, malformed, excessive or late output fails closed and cannot update the caller's cache.

Only the explicit consumer binding and selected provider/prod/path metadata enter private stdin.  The token is absent from argv, inherited environment, files and diagnostics.  The child uses the current absolute Python in isolated mode and the same reviewed module, with a minimal environment and discarded stderr.  Provider text and arbitrary exception strings are excluded from errors.

On timeout or interruption the parent kills only its owned child and performs bounded cleanup.  Cleanup failure has a distinct fixed unavailable diagnostic.  This bounds provider, resolver and executor waits after normal process launch; it does not promise an unconditional return within exactly three seconds under an uninterruptible OS launch or scheduling stall.

The existing `fetch_project` and lower-level HTTP operations retain their socket-budget semantics.  The writer's sent/unsent callbacks, retained ledger, acknowledgments, unknown outcomes and reconciliation are unchanged.  A timed-out read worker never establishes that a mutation was unsent.  The registry caller will adopt this generic operation in the coordinated bootstrap source after review; this library draft alone activates no consumer or credential.
