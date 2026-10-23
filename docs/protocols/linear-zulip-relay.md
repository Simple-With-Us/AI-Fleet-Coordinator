# Zulip to Linear relay

The relay mirrors a Zulip conversation onto the Linear issue it is about.  It is one way only:
nothing in the relay ever posts to Zulip, and Linear stays the record.

Owner decisions, Jay, Oct 21 and Oct 23, 2026.  Implementation: `scripts/agent_sync/linear_relay.py`,
tests: `scripts/agent_sync/tests/test_linear_relay.py`.

## What it does

Any message in a public Zulip stream whose topic title or body names a Linear issue id (AFC-123 and
the other team keys, the same list as `LINEAR_TEAMS` in `scripts/zulip_linkifiers.py`) is mirrored
onto that issue as a comment.

There is exactly one comment per (issue, topic).  The first hit creates it; every later hit in the
same topic edits it in place, adding a bullet per message with a permalink back to Zulip.  Edits
are batched: at most one edit per comment every ten minutes, and a tick flushes what is pending.
A message is mirrored once, so a replay is idempotent.

Human posts and bot posts both count.  Direct messages and private streams are never relayed: the
relay bot is a plain member that joins public streams only, and the worker drops any message whose
stream is not in the public set it loaded, so a private stream the bot was added to by hand is
still filtered out.

Nothing is ever posted from Linear to Zulip, and there is no #issues stream.

## The comment

The first line is a marker, so a human can spot a relay comment and a machine can find it:

    <!-- zulip-relay --> AFC-1|12345

The key is the issue and the id of the first message recorded for the topic, not the topic name:
a topic rename moves the conversation, and the key has to survive that so the relay keeps editing
the same comment instead of starting a second one.  Then a heading, one bullet per message, and a
footer that says the mirror runs one way.

Copied Zulip text is untrusted.  It is trimmed, stripped of credential-shaped strings, and escaped
so it cannot open a link, start a code span, close the quoted string, or carry a token into Linear.
An @ is kept but followed by a zero-width space, so a mention never becomes a live mention.

## Environment

| Name | Meaning |
| --- | --- |
| `ZULIP_RELAY_EMAIL` | the relay bot's Zulip address |
| `ZULIP_RELAY_API_KEY` | its API key |
| `ZULIP_SITE` | the realm (already set on the server instance) |
| `LINEAR_RELAY_TOKEN` | the OAuth app token of the dedicated Linear relay app |
| `LINEAR_RELAY_STATE` | ledger path (default `~/.agent-sync/linear-relay.json`) |
| `LINEAR_RELAY_TEAMS` | override for the team keys |
| `LINEAR_RELAY_DRY_RUN` | `1` logs what it would write and writes nothing to Linear |
| `LINEAR_RELAY_CHECK_ISSUE` | an issue id for `check` to look up |

Commands:

    python3 -m agent_sync.linear_relay run        # the long-running loop
    python3 -m agent_sync.linear_relay check      # credentials and Linear auth, no writes

`check` proves the Zulip bot is a member and not an admin, and that the Linear token works.  A
missing issue is reported as a missing issue, not as an auth failure: those are different
problems and the operator needs to know which one it is.

## The ledger

The ledger maps (issue, stream, topic) to the comment id, the recent permalinks, the message ids
already seen, and the timing.  It is written atomically under a lock, so a restart never
double-comments and two relays never share one file.

It MUST sit on a persistent volume.  A lost ledger means the relay no longer knows which comment
belongs to a topic, so it starts a new one, and the old comment stays behind.

The ledger is bounded: 500 entries and 8 MB.  The oldest entries are pruned first.  A pruned entry
means a later message in that old topic starts a second comment.

## What this does NOT promise

The relay is not exactly-once, and it does not say it is.  Three things can each produce a second
comment for one topic:

1. a lost ledger, or a ledger pruned past the topic;
2. a second relay process on another host, which the file lock cannot prevent across hosts;
3. an uncertain write outcome, where a create landed and its answer was lost.

The relay narrows the third one: it writes the intent to disk before the call, and before it
creates anything it looks for its own marker on the issue and adopts the comment it finds.  That
reconciliation is bounded by the pages it reads and is not verified against live Linear pagination,
consistency or markup persistence, so it narrows the risk rather than removing it.

Two more limits, both from the handoff: a queue expiry or a downtime loses the messages sent in the
gap, because there is no backfill; and the permission to edit a comment under the Linear scopes
`read` and `comments:create` is UNVERIFIED.  The relay is written for it, and the sandbox test that
proves it comes later, after Jay approves the app.

## Gates that stay with Jay

The relay writes nothing to Linear and holds no credential until Jay approves each of these:

1. **Linear**: a new OAuth application named "Zulip Relay", actor application, scopes `read` and
   `comments:create` only.  The id, secret and token stay out of chat, code and logs.
2. **Zulip**: a new bot, role Member, not admin and not owner.
3. **Infisical**: `ZULIP_RELAY_EMAIL`, `ZULIP_RELAY_API_KEY` and `LINEAR_RELAY_TOKEN` for the
   server instance.  The path is Jay's choice.
4. **Coolify**: after the PR merges, a second process or service from the same image, with the
   environment above, a persistent volume for the ledger, and restart always.  First run with
   `LINEAR_RELAY_DRY_RUN=1`, watch the log, then unset it.

The relay runs on the Coolify server instance, the same image as the simple-chat daemon.  No new
Cloudflare Worker is involved.

## Why there is no RELAY row in the seat partition

The handoff suggested a `RELAY = "server"` row in `docs/protocols/agent-sync-partition.toml`.  That
row is deliberately absent.  `config.py`'s `fleet_tags` treats every seat named in the partition as
an eligible sender, so a RELAY row would make the relay a wake target and a tag it could wake, and
the relay holds no agent.  The relay is a service, not a seat, so it takes no row.  Echo's reviewer
confirmed this twice from the source, and `scripts/agent_sync/config.py` was read again for this
build.

## Live verification

Run `check` first.  Then, in a sandbox public topic, post a human message naming a known test
issue: expect exactly one comment from the Zulip Relay app with a working link.  Post as a bot:
after ten minutes the same comment is updated, not stacked.  An unknown issue id, a direct message
to the relay bot and a private-stream post produce nothing.  Restart the container: no replay and
no duplicates.  Report the results with the Linear comment URL.
