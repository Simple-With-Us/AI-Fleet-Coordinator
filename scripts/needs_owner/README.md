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
```

| Module | Job |
|---|---|
| `cli.py` | argparse front end;  `scripts/needs-owner` is the entry point |
| `verbs.py` | `file`, `adopt`, `list`, `status` |
| `block.py` | the fenced block in the issue description:  render and strict parse |
| `records.py` | the comment formats (`notified`, `notify_pending`, `block`, `owner replied:`) |
| `items.py` | reading items back (`board list` and `board show`), derived state, the JSON |
| `notify.py` | the post, `find_notice` (the maybe-sent check by reference line:  bot of the filing seat or CLAUDE only, unquoted, oldest;  C6-2 imports it), the Grok Bot mirror reply |
| `text.py`, `links.py`, `safety.py`, `seat.py`, `proc.py` | cleaning and the uid, topic links, secret and personal-data refusals, the verified seat, running the children |
| `f3.py` | loads `agent_sync/textsafe.py`, `secretscan.py` and `identity.py` by file path |

`needs_owner` never runs `import agent_sync`:  that would pull in the whole Zulip client, and the stable copy
(`~/apps/needs-owner`, installed by `install.sh`) holds only those three files.  To change a record format or
a state, change `records.py` or `items.py` once:  the C6-2 notifier imports them.

## Tests

```
cd scripts && python3 -m unittest discover -s needs_owner/tests -t . -v
```

`tests/fake_board.py` and `tests/fake_agent_sync.py` stand in for `board` and `agent-sync` on PATH.  They parse
argv with the real parsers (`scripts/linear-board/board`, `agent_sync.cli`), so a wrong flag, or a flag in
the wrong place, fails in the tests as it would for real.  The fake `read --json` prints the
keys of the real `message_json` (`is_bot`, `seat`, `sender_id`, ...;  a test compares the two key sets), and
`World.add_message` puts a person's or another bot's message in a topic.  The fake board stores descriptions as Linear hands
them back.  `tests/fixtures/list_golden.json` pins the `list --json` shape;  regenerate it with
`UPDATE_GOLDEN=1` and read the diff.
