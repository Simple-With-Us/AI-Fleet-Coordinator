# needs_owner

`needs-owner` files a decision that waits on Jay as a Linear issue and has the filing seat's own bot open the
Zulip topic `OWNER <ID> <subject>` that mentions him.  The contract, the formats and the safety rules are in
`docs/protocols/needs-owner.md`;  this file is the developer view.  Python 3.11+, standard library only.

```
needs-owner file --by SEAT --app APP --question TEXT --option TEXT ... [--low-stakes N] [--recommend N]
                 [--parent ID] [--context TEXT] [--urgent] [--again] [--mirror-of LINK] [--dry-run] [--json]
needs-owner adopt ID --by SEAT --option TEXT ... [--question TEXT] [--no-notify]
needs-owner list [--open | --replied | --all] [--seat SEAT] [--json]
needs-owner status ID [--json]
needs-owner notify [--dry-run] [--json]        the fallback notifier (CLAUDE only;  the LaunchAgent runs it every 5 minutes)
```

| Module | Job |
|---|---|
| `cli.py` | argparse front end;  `scripts/needs-owner` is the entry point |
| `verbs.py` | `file`, `adopt`, `list`, `status` |
| `block.py` | the fenced block in the issue description:  render and strict parse |
| `records.py` | the comment formats (`notified`, `notify_pending`, `block`, `owner replied:`) |
| `items.py` | reading items back (`board list` and `board show`), derived state, the JSON |
| `notify.py` | the post, `find_notice` (the maybe-sent check by reference line:  bot of the filing seat or CLAUDE only, unquoted, oldest;  the notifier imports it), the Grok Bot mirror reply |
| `notifier.py` | `notify`:  the fallback notifier's run (replies, notices, notes, nudges, the Grok Bot mirror), the K1 knobs, the event log |
| `replies.py` | pure rules:  which reactions are taps, the control words, a Grok Bot `needs-owner-request` block, the notifier's own words |
| `ledger.py` | the notifier's state (caps, once-only marks, notes due, the pause), its lock and the file for CLAUDE's own items |
| `text.py`, `links.py`, `safety.py`, `seat.py`, `proc.py` | cleaning and the uid, topic links, secret and personal-data refusals, the verified seat, running the children |
| `f3.py` | loads `agent_sync/textsafe.py`, `secretscan.py` and `identity.py` by file path |

`needs_owner` never runs `import agent_sync`:  that would pull in the whole Zulip client, and the stable copy
(`~/apps/needs-owner`, installed by `install.sh`) holds only those three files, plus `fleet_knobs/` for `notify`.  To change a record format or
a state, change `records.py` or `items.py` once:  the notifier imports them.  The LaunchAgent is `scripts/launchd/com.jay.needs-owner-notify.plist`;
the contract (what counts as an answer, the caps, the pause, the Grok Bot block) is `docs/protocols/needs-owner.md` § The Fallback Notifier.

## Tests

```
cd scripts && python3 -m unittest discover -s needs_owner/tests -t . -v
```

`tests/fake_board.py` and `tests/fake_agent_sync.py` stand in for `board` and `agent-sync` on PATH (the fake `agent-sync` also answers `read`, `whoami`,
`verify-owner-msg`, `topics` and `channels`, with the real reaction shape and the real `verify-owner-msg` rule;  it logs each call's exit code).  They parse
argv with the real parsers (`scripts/linear-board/board`, `agent_sync.cli`), so a wrong flag, or a flag in
the wrong place, fails in the tests as it would for real.  The fake `read --json` prints the
keys of the real `message_json` (`is_bot`, `seat`, `sender_id`, ...;  a test compares the two key sets), and
`World.add_message` puts a person's or another bot's message in a topic.  The fake board stores descriptions as Linear hands
them back.  `tests/fixtures/list_golden.json` pins the `list --json` shape;  regenerate it with
`UPDATE_GOLDEN=1` and read the diff.
