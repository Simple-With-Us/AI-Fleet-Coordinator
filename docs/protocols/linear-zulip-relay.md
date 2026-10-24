# Zulip to Linear relay

The relay mirrors a Zulip conversation onto the Linear issue it is about.  It is one way only:
nothing in the relay ever posts to Zulip, and Linear stays the record.

Implementation: `scripts/agent_sync/linear_relay.py`, tests: `scripts/agent_sync/tests/test_linear_relay.py`.
Owner decisions are Jay's, from Oct 21 and Oct 23, 2026.

## What it does

Any message in a public Zulip stream whose topic title or body names a Linear issue id (AFC-123 and
the other team keys, the same list as `LINEAR_TEAMS` in `scripts/zulip_linkifiers.py`) is mirrored
onto that issue as a comment.

There is exactly one comment per (issue, topic).  The first hit creates it; every later hit in the
same topic edits it in place, adding a bullet per message with a permalink back to Zulip.  Edits
are batched: at most one edit per comment every ten minutes, and a tick flushes what is pending.
A message is mirrored once, so a replay is idempotent.

Human posts and bot posts both count.  Direct messages and private streams are never relayed: the
relay bot is a plain member that joins public streams only, and the worker drops any message whose
stream is not in the public set it loaded, so a private stream the bot was added to by hand is
still filtered out.  A stream that was public and later turns private is caught too: the entry is
dropped from the ledger before it can be written, on every path that writes, ticks or reconciles.

The public-stream set fails closed.  Until it has loaded, the relay relays nothing, writes nothing
to Linear and drops nothing from the ledger: an unloaded set proves nothing about any stream, so
every entry keeps its state until the set is known.  A failed load is logged by name ("could not
load the public-stream set") and retried, and an answer without a well-formed streams list is a
failed load, never an empty set.  At start the event queue is not registered until the set loads.
A reload after a stream event forgets the old set first, so a reload that fails leaves the relay
knowing nothing rather than trusting a set that may be stale; the queue is kept, and the reload is
tried again after every poll.  The relay keeps running through a Zulip outage instead of exiting,
because it does nothing while the set is unknown.  A message that arrives while the set is unknown
is not held for later: like a message sent while the queue is down, it is not mirrored.  Archived
public streams count as public: only `invite_only` makes a stream private.

Nothing is ever posted from Linear to Zulip, and there is no #issues stream.

## Renames and moves

A topic rename or a stream move arrives as Zulip's `update_message` event.  Per the get-events
schema, `stream_id` and `stream_name` describe the stream BEFORE the move and `new_stream_id` the
stream after it; `orig_subject` is the old topic, and `subject` the new one, absent when only the
stream changed; `propagate_mode` is `change_one`, `change_later` or `change_all`, and `message_ids`
lists only the messages that moved.

- Both ends must be public before anything moves or is recorded.  A move out of a private stream
  records nothing, so a private stream's name or topic title never reaches Linear.
- `change_all` moves the whole conversation: the standing comment's entry is re-keyed from the old
  stream and topic to the new ones, and its heading takes the new stream's name from the loaded
  set by id, never from `stream_name`.
- `change_one` and `change_later` move some messages, not the topic: the old topic's comment stays
  standing.
- A move to a private stream closes the old entry (`change_all`), or takes the moved messages out of
  its list (a partial move), so the comment never links to messages in a stream the relay may not
  look at.

## The comment

The first line is a marker, so a human can spot a relay comment and a machine can find it:

    <!-- zulip-relay --> AFC-1|12345

The key is the issue and the id of the first message recorded for the topic, not the topic name:
a topic rename moves the conversation, and the key has to survive that so the relay keeps editing
the same comment instead of starting a second one.  The marker is matched against whole lines of
a comment, never as a substring, so the marker of message 100 cannot match the comment of message
1000.  Then a heading, one bullet per message, and a footer that says the mirror runs one way.

Copied Zulip text is untrusted.  Every field (the topic, the message, the sender's name, the stream
name) is scanned for credential-shaped text before it is stored, logged or relayed: the fleet's own
scanner (`scripts/agent_sync/secretscan.py`, which knows the Linear, Sentry, Infisical, Cloudflare
and other service prefixes, and Zulip-shaped API keys of 32 mixed-case letters and digits) and the
relay's wider rows (long hex and base64 runs, and a keyword such as `password` or `token` followed
by `:` or `=` and a value).  The Zulip-shaped row also withholds an opaque 32-character id, a cost
accepted because a Zulip key is the credential most likely to be pasted into Zulip.  A keyword in
plain prose, such as "token refresh", is not a hit.  A field with a hit is withheld whole: it shows as "(withheld: it
looked like it carried a credential)", and the log names only the message id and the field.  It is
never partly redacted, because a pattern can find where a credential starts but not always where it
ends.  The message itself is still mirrored as a link, which carries no Zulip text.  A clean field is
trimmed and escaped so it cannot open a link, start a code span or close the quoted string.  An @ is
kept but followed by a zero-width space, so a mention never becomes a live mention.  The relay's log
names an entry by its issue and its first message id, never by its topic.  A message that came in
by a topic rename was never read by the relay, so its bullet carries no quote.

The link on each bullet names the message id and nothing else: `https://<realm>/#narrow/near/<id>`.
The topic never goes into a URL, because a percent-encoded topic is still the topic, and a reader of
the Linear comment could decode it.  Zulip's `near` narrow operator resolves a message id on its own,
so the link lands on the message and the topic it lives in.  The link is built when the comment is
rendered, from the message id, so no text that came out of Zulip can reach it.

## Environment

| Name | Meaning |
| --- | --- |
| `ZULIP_RELAY_EMAIL` | the relay bot's Zulip address |
| `ZULIP_RELAY_API_KEY` | its API key |
| `ZULIP_SITE` | the site of the bot's credentials; it must name the same host as the realm |
| `AGENT_SYNC_REALM` | the realm the relay talks to and links to (default: the fleet realm) |
| `LINEAR_RELAY_TOKEN` | the OAuth app token of the dedicated Linear relay app |
| `LINEAR_RELAY_STATE` | ledger path (default `~/.agent-sync/linear-relay.json`) |
| `LINEAR_RELAY_TEAMS` | override for the team keys |
| `LINEAR_RELAY_DRY_RUN` | `1` logs what it would write and writes nothing to Linear |
| `LINEAR_RELAY_CHECK_ISSUE` | an issue id for `check` to look up |

A `ZULIP_SITE` whose host is not the realm's stops the relay at start with a message that names
both variables.

Commands:

    python3 -m agent_sync.linear_relay run        # the long-running loop
    python3 -m agent_sync.linear_relay check      # credentials and Linear auth, no writes

`check` proves the Zulip bot is a member and not an admin, and that the Linear token works.  A
missing issue, or an id Linear refuses as malformed, is reported as an issue problem, not as an
auth failure: those are different problems and the operator needs to know which one it is.

## Talking to Linear

The client never follows a redirect, so the bearer token is never re-sent to another host, and it
sends its own User-Agent.  Every call has one deadline that its retries share, and the marker scan
has one deadline for all of its pages.  Each poll cycle has a two-minute write budget, which the
creates made while its messages are handled and the tick after them share (the reconcile at
start gets a cycle of its own, and a pending entry it does not reach is checked before its next
write): a write starts only
inside the budget, and what it does not reach stays dirty for the next cycle.  A write already
under way finishes within its own deadline, so one cycle's writes end within a few minutes and the
Zulip poll loop keeps polling.

Errors carry only allow-listed fields: an HTTP status, Linear's error codes, and a fixed
description.  The response body never reaches an exception or the log, and neither does the token.
A refused token is reported as "linear refused LINEAR_RELAY_TOKEN (http 401); key abcdefgh…wxyz":
the variable to check and the fleet's masked preview of the key (`zulip.masked_credential`, the
same one the agent-sync CLI uses: the first eight and last four characters of a key of 24 or more,
one character of a shorter one), so the operator can tell which key was refused.

Failures are sorted by what waiting can change:

- A refused token (HTTP 401, or an authentication error) stops the relay with a message, because
  no retry can fix it.  The container's restart policy brings it back, and it stops again until
  the token is fixed.  A 403 refuses that one request, not the token, so it does not stop the relay.
- A missing issue, a forbidden entity, invalid input or a 403 is final for that write: the entry is
  not retried until a new message in the topic gives it new content.  An issue id Linear does not
  have is looked up again after an hour, so an id named before its issue exists is not lost.  The
  log says which it was, a missing issue or a refused lookup, so a scope problem never looks like a
  quiet relay.
- A rate limit is a definite "not done": reads, edits and creates are all retried later.
- A 5xx, a timeout or a network error is transient for a read or an edit, which retry with backoff
  inside the call's deadline before the worker backs the entry off.  For a create it is an unknown
  outcome, and a create is never retried as it is: see "What this does NOT promise" below.
- A refused edit is not read as "the comment is gone".  The relay scans the issue for its marker: a
  comment that is still there is kept, and only a comment that is really gone is created again.  A
  definite refusal of the edit is not retried until new content arrives; an edit whose stored id
  missed goes to the id the scan found.
- A ledger write that fails (a full or broken volume) is logged and tried again next cycle.  A create
  still needs its own write-ahead save, so nothing is created while the volume fails.

## The ledger

The ledger maps (issue, stream, topic) to the comment id, the message ids already seen, the
recent messages, and the timing.  It keys each topic on a digest of the title, and stores the title
only in its withheld-or-clean display form, so a credential in a topic never reaches the file.  It
is written atomically under a lock, so a restart never double-comments and two relays never share
one file: the temporary file is owner-only from the moment it is created, it is synced before the
rename, and the directory is synced after it.  A new state directory is created owner-only, and a
symlinked one is refused.

Every entry is checked on load (each field, its type, the list elements and the bounds), and a
ledger with a malformed entry stops the relay rather than being half read.  The file is written
once per cycle, not once per message; a create still writes its intent ahead of the call.

It MUST sit on a persistent volume.  A lost ledger means the relay no longer knows which comment
belongs to a topic, so it starts a new one, and the old comment stays behind.

The ledger is bounded: 500 entries and 8 MB.  The oldest entries are pruned first.  A pruned entry
means a later message in that old topic starts a second comment.

## What this does NOT promise

The relay is not exactly-once, and it does not say it is.  Three things can each produce a second
comment for one topic:

1. a lost ledger, or a ledger pruned past the topic;
2. a second relay process on another host, which the file lock cannot prevent across hosts;
3. an uncertain write outcome, where a create landed and its answer was lost.

A fourth leaves an old comment behind without adding one: when a topic is renamed onto a topic that
already has its own comment, the two conversations merge into the surviving comment, and the moved
topic's older comment is left as it was, because the app cannot delete comments.

The relay narrows the third one, and it tracks the outcome of every create explicitly in the
file-backed ledger:

- Before a create, the entry is marked `pending` and the ledger is written.  `pending` is the
  unknown state: the create may or may not land.
- A create is sent once.  The client never re-sends it on a timeout, a network error, a 5xx, an
  unreadable answer or an unmapped GraphQL error; it reports the outcome as unknown, and the entry
  stays `pending`.
- A `pending` entry is never created again blind.  The next attempt, and the reconciliation when the
  queue registers, first reads the issue's comments for the marker and adopts the comment it finds.
  Only when the whole scan finishes without finding it is a create sent again.  A scan that cannot
  finish (a malformed page, a bad cursor, the page cap or its deadline) is a failure, not "not
  found", so it never leads to a create.
- A definite answer releases the entry: a refusal (a forbidden entity, invalid input,
  `success: false`) or a rate limit means nothing landed, so the entry is no longer `pending`.
- The entry's fields carry the three outcomes: `comment_id` set is done, `pending` with no
  `comment_id` is unknown, and neither is refused or not yet tried.

That reconciliation is bounded by the pages it reads and is not verified against live Linear
pagination, consistency or markup persistence (whether Linear keeps the HTML comment of the marker
is one of the sandbox checks below), so it narrows the risk rather than removing it.

Two more limits, both from the handoff: a queue expiry or a downtime loses the messages sent in the
gap, because there is no backfill; and the permission to edit a comment under the Linear scopes
`read` and `comments:create` is UNVERIFIED.  The relay is written for it, and the sandbox test that
proves it comes later, after Jay approves the app.

## Gates that stay with Jay

The relay writes nothing to Linear and holds no credential until Jay approves each of these:

1. **Linear**: a new OAuth application named "Zulip Relay", actor application, scopes `read` and
   `comments:create` only.  The id, secret and token stay out of chat, code and logs.
2. **Zulip**: a new bot, role Member, not admin and not owner.
3. **Infisical**: `ZULIP_RELAY_EMAIL` and `ZULIP_RELAY_API_KEY` in prod `/zulip`, where the
   fleet keeps every Zulip bot's credentials, and `LINEAR_RELAY_TOKEN` in a path of Jay's choice.
4. **Coolify**: after the PR merges, a second process or service from the same image, with the
   environment above, a persistent volume for the ledger, and restart always.  First run with
   `LINEAR_RELAY_DRY_RUN=1`, watch the log, then unset it.  The image's Infisical loader
   (`scripts/agent_sync/server/infisical_env.py`) copies only the names on its allow-list, and the
   relay's three are not on it, so the relay service gets them as its own runtime environment.
   Adding them to the shared loader would hand the relay's keys to the daemon as well.  The image's
   entrypoint always runs `simple-chat`, and the package sits in `/app/scripts`, so the relay
   service overrides the entrypoint with `PYTHONPATH=/app/scripts python3 -m agent_sync.linear_relay
   run`.

The relay runs on the Coolify server instance, the same image as the simple-chat daemon.  No new
Cloudflare Worker is involved.

## Why there is no RELAY row in the seat partition

The handoff suggested a `RELAY = "server"` row in `docs/protocols/agent-sync-partition.toml`.  That
row is deliberately absent.  `config.py`'s `fleet_tags` treats every seat named in the partition as
an eligible sender, so a RELAY row would make the relay a wake target and a tag it could wake, and
the relay holds no agent.  The relay is a service, not a seat, so it takes no row.  Echo's reviewer
confirmed this twice from the source, and `scripts/agent_sync/config.py` was read again for this
build.

## Live verification

Run `check` first.  Then, in a sandbox public topic, post a human message naming a known test
issue: expect exactly one comment from the Zulip Relay app with a working link.  Post as a bot:
after ten minutes the same comment is updated, not stacked.  An unknown issue id, a direct message
to the relay bot and a private-stream post produce nothing.  Restart the container: no replay and
no duplicates.  The sandbox run also settles three things this build cannot check offline: that
Linear keeps the marker line in the comment body, that an edit works under `read` and
`comments:create`, and that Zulip's real `update_message` events for a rename and a stream move
match the shapes the tests use.  Report the results with the Linear comment URL.
