"""needs_owner:  decisions that wait on Jay, filed in Linear and announced by the filing seat's own bot.

A needs-owner item is a Linear issue (Kind/agent-report plus the flat `needs-owner` label) whose
description carries one fixed block (question, options, recommendation, parent, filer, context).  The
seat that needs Jay files it with `needs-owner file`, and the same command makes that seat's bot open
the Zulip topic `OWNER <ID> <subject>` that mentions Jay.  `list` and `status` read the items back.

Modules:
  f3        the F3 helpers (textsafe, secretscan, identity) loaded by file path, never by package import
  text      cleaning and normalizing caller text, the uid hash, the topic name, the emoji
  block     the fixed description block:  render and strict parse
  records   the comment formats that record a notice, a pending notice and an owner reply
  safety    the secret and personal-data refusals that run before anything is sent
  seat      the verified seat (launcher first) and the child environment
  proc      running `simple-board` and `simple-chat` as argv lists with private temp files
  items     reading items back from the board and deriving their state
  notify    the post (or the mirror reply), the maybe-sent check and the notice record
  notifier  `notify`:  the fallback notifier (replies, notices, notes, nudges, the Grok Bot mirror)
  replies   pure rules for taps, control words and Grok Bot requests
  ledger    the notifier's state, caps, lock and the file for CLAUDE's own items
  links     Zulip topic links, read and made
  verbs     file, adopt, list, status and notify
  cli       the argparse front end

Python 3.11 or newer, standard library only.  docs/protocols/needs-owner.md is the contract.
"""

__version__ = "1.0.0"
