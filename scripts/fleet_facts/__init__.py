"""fleet_facts:  read-only gatherers for the fleet's open-loops digest, and the one activity definition.

Modules:
  activity    pure functions, no state and no I/O:  activity_at and idle_band (C2-1 and C4-1 import them;
              there is no second definition anywhere)
  github_prs  one GraphQL census of the open pull requests in the org
  lanes       the Mac lane doctor report (`lane ls --json`), reused when under 15 minutes old
  board       Linear through the installed `board` CLI, read verbs only:  claims and needs-owner items
  backup      the fleet-work-backup health report, passed through
  interfaces  tolerant readers for C1, C3, C4, C6 and C7 (a missing one is "absent", not an error)
  safety      the F3 helpers (render_untrusted, redact_fields), loaded by path
  runner      the one place a subprocess starts:  argv lists only, a timeout, never a shell
  gather      runs the requested parts and builds the facts document
  cli         `fleet-facts gather ...`

Python 3.11 or newer, standard library only.  See README.md.
Tests:  cd scripts && python3 -m unittest discover -s fleet_facts/tests -t . -v
"""

__version__ = "1.0.0"
