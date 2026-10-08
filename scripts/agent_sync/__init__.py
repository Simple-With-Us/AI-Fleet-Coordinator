"""agent_sync: the fleet's Zulip coordination CLI.

Modules:
  zulip   credential loading, a urllib-only HTTP client with retries, and the event-queue helper
  state   per-session cursors and posted-id ledger, plus the seat-wide inbox cursor
  cli     the argparse front end (`main(argv) -> int`); `scripts/agent-sync` is the entry point

The listener (docs/protocols/agent-sync-listener.md):
  live          leases, live inboxes, claiming, untrusted wrapping, light config (hook fast path)
  attach        `agent-sync attach` and `detach`: the Claude Code hooks, rewake, drain and wait
  router        pure routing and the wake prefilter
  wakes         the wake ledger, budgets, coalescing, the loop guard, the validator and the prompt
  adapters      the Claude headless wake, notify-owner and board filing
  daemon        the always-on daemon (`agent-sync daemon run`)
  config        listener.toml with defaults and validation
  launchd       `daemon install` and `uninstall` (writes files, never runs launchctl)
  listener_cli  `daemon ...`, `status`, `wakes` and `inbox --local`
  secretscan    the secret scanner for anything about to be posted

Python 3.11 or newer, standard library only.  See README.md for usage, the credential order,
the state layout, the exit codes and the wire rules.

Tests (unittest, a local fake Zulip server, never the live realm):

    cd scripts && python3 -m unittest discover -s agent_sync/tests -t . -v
"""

__version__ = "1.0.0"
