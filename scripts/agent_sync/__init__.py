"""agent_sync: the fleet's Zulip coordination CLI.

Modules:
  zulip   credential loading, a urllib-only HTTP client with retries, and the event-queue helper
  state   per-session cursors and posted-id ledger, plus the seat-wide inbox cursor
  cli     the argparse front end (`main(argv) -> int`); `scripts/agent-sync` is the entry point

Python 3.11 or newer, standard library only.  See README.md for usage, the credential order,
the state layout, the exit codes and the wire rules.

Tests (unittest, a local fake Zulip server, never the live realm):

    cd scripts && python3 -m unittest discover -s agent_sync/tests -t . -v
"""

__version__ = "1.0.0"
