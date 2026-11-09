"""open_loops:  the fleet's daily open-loops digest, minus the judgment.

  gather   run the F6 gatherers (GitHub, Linear, and on a Mac the lanes, C1, C6 and C3 parts), or read the
           signed Mac facts, and build the facts document (schema 1).  Read-only.
  render   the facts as the full Markdown list.
  send     BF-Deployer only:  lint the DM it wrote, skip a duplicate, send it to the owner, post a heartbeat.

Modules:
  central      Central time, 12-hour, DST-correct (the owner's clock)
  urls         the one place a URL is made (Linear and PR links are validated, Zulip links built from ids)
  macfacts     the signed Mac facts file:  read, verified with `fleet-facts verify`, or treated as absent
  views        tolerant readers for the C1 and C6 documents
  sections     the six sections of the facts (decisions, stuck PRs, not on GitHub, unpushed, claims, asks)
  claims       claim bands from the one activity definition in fleet_facts.activity
  zulip_topics the claimants' posts in their `<ID> ...` topics, read through the installed simple-chat
  facts        gather and assemble
  render       the Markdown list
  lint         the DM checks
  send         the sender
  cli          `open-loops ...`

Python 3.11 or newer, standard library only.  See README.md.
Tests:  cd scripts && python3 -m unittest discover -s open_loops/tests -t . -v
"""

__version__ = "1.0.0"
