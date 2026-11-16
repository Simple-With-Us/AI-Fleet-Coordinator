# Session letters in listener status (AFC-597, slice S2)

## Summary

The daemon's status output (listener/status.json) now carries the session letter for each lease whose session the Operator registry has lettered.  Muster's Session Letters pane reads these fields instead of joining leases to the registry itself.  Nothing here touches a secret value: names and booleans only.

## Why

The Operator SQLite registry (scripts/simple_tools/operator_registry.py) is the sole letter allocator, and the daemon already joined catalog rows to letters internally for the wake path.  The published status rows named only a lease id and a platform, so no reader outside the daemon could say which letter a live session holds.  The signed listener read API does not expose leases either, so status.json is the publication surface.

## What changed

Each per-seat lease row in daemon status() gains up to four fields. session_id is the lease's own session id when the lease carries one. letter is the registered pair (for example AA) when the registry holds a live letter for that session. title is the registered session title, scrubbed the same way other status text is. verified_host is the host the registry identity matched on exactly.  A lease with no registration carries none of the registry fields rather than an invented value.

The join is the wake path's own. session_labels() is now the letter-only view of a new session_letter_details() that returns the letter, its reference form, the title, and the verified host from one lookup_many() pass, and lookup_many() now keeps the title its underlying lookup already read.  A lease names its platform in the attach vocabulary (claude-code) where the registry keys the platform namespace (claude, the catalog's own alias), and it carries no profile scope, so the lookup uses the scope every in-fleet registration path uses (default).  A session registered under any other scope simply misses and its row carries no letter.

Reads never move any cursor.  The lookup never creates the registry file, and a missing or broken registry degrades to today's rows instead of breaking status, matching the wake path's discipline.

listener_records.leases_view is unchanged on purpose.  Status rows are built in daemon.status(), not there, and that view has no daemon instance to key the registry's verified host on.  Enriching it would duplicate the join in a second place.

## Files

- scripts/simple_tools/daemon.py (session_letter_details, the session_labels view of it, and the status() lease rows).
- scripts/simple_tools/operator_registry.py (lookup_many keeps the title).
- scripts/simple_tools/tests/test_daemon.py (two status tests: a registered lease publishes its letter, title, and verified host while an unregistered one omits them; a missing registry file degrades and is never created by the read).

## Verification

- New tests failed first with KeyError: 'session_id' on the untouched status rows, then passed.
- cd scripts && python3 -m unittest simple_tools.tests.test_daemon.SecurityTests.test_status_publishes_the_registered_letter_for_a_lease simple_tools.tests.test_daemon.SecurityTests.test_status_without_a_registry_file_degrades_to_todays_rows simple_tools.tests.test_daemon.SecurityTests.test_status_output — 3 tests, OK.
- cd scripts && python3 -m unittest simple_tools.tests.test_wake_session_letters — 77 tests, OK.
- cd scripts && python3 -m unittest test_operator_registry test_mm_session_letter — 242 tests, OK.
- cd scripts && python3 -m unittest simple_tools.tests.test_daemon simple_tools.tests.test_listener_records simple_tools.tests.test_loop_status — 195 tests, 1 failure (ThreadedRunTests.test_the_entry_script_runs_the_daemon_and_sigterm_stops_it, the daemon subprocess never registers in this environment; the identical failure reproduces on the untouched base tree, so it pre-dates this change).
- cd scripts && python3 -m unittest discover -s simple_tools/tests -t . — 3967 tests, 11 failures, 11 errors, 2 skipped.  The failures this environment produces are the subprocess-registration class above; none is in the status or letter-join paths, which the focused runs above cover green.

## Follow-ups left open on purpose

- A session registered under a non-default profile scope does not publish a letter through its lease, because a lease carries no scope to join on.  Publishing one would need the lease (or the catalog row for the session) to name the scope.
- Muster's Session Letters pane is the consumer slice and lands separately.  This slice only publishes the fields.

## Coordination

MA (Rob, the Operator owner) accepted this interim read shape on 2026-11-03, provided lookup_many stays the Operator registry module's own read path rather than a second alias-resolution implementation.  This join calls operator_registry.lookup_many exactly as the wake path does.  When the registry API (/registry/v1/) is exposed, Muster's letters surface converges on it and this status join retires.
