# Zulip to Linear relay

The relay mirrors a Zulip conversation onto the Linear issue it is about.  It is one way only:
nothing in the relay ever posts to Zulip, and Linear stays the record.

Implementation: `scripts/simple_tools/linear_relay.py`, tests: `scripts/simple_tools/tests/test_linear_relay.py`.
Owner decisions are Jay's, from Oct 21 and Oct 23, 2026.

## What it does

Any message in a public Zulip stream whose topic title or body names a Linear issue id (AFC-123 and
the other team keys, the same list as `LINEAR_TEAMS` in `scripts/zulip_linkifiers.py`) is mirrored
onto that issue as a comment.

There is exactly one comment per (issue, topic).  The first hit creates it; every later hit in the
same topic edits it in place, adding a block per message with a permalink back to Zulip.  Edits
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
is not held for later: like a message sent while the queue is down, it is not mirrored by the
standalone relay (hosted by the Operator, a topic with a standing comment reads it at its next
message; see "What this does NOT promise").  Archived public streams count as public: only
`invite_only` makes a stream private.

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
- An archived record (see "The ledger") obeys the same rules: `change_all` re-keys it, a move to a
  private stream drops it, and a partial move leaves it with the old topic.  It lists no message,
  so a partial move to a private stream cannot take messages out of its comment until the topic's
  next message restores and refills it.  A rename that newly names an issue whose comment is
  archived restores nothing: the topic's next message does.

## The comment

The first line is a marker, so a human can spot a relay comment and a machine can find it:

    <!-- zulip-relay --> AFC-1|12345

The key is the issue and the id of the first message recorded for the topic, not the topic name:
a topic rename moves the conversation, and the key has to survive that so the relay keeps editing
the same comment instead of starting a second one.  The marker is matched against whole lines of
a comment, never as a substring, so the marker of message 100 cannot match the comment of message
1000.  Then a heading, one block per message (its permalink, its sender and the whole post),
and a footer that says the mirror runs one way.

Copied Zulip text is untrusted.  Every field (the topic, the message, the sender's name, the stream
name) is scanned for credential-shaped text before it is stored, logged or relayed: the fleet's own
scanner (`scripts/simple_tools/secretscan.py`, which knows the Linear, Sentry, Infisical, Cloudflare
and other service prefixes, and Zulip-shaped API keys of 32 mixed-case letters and digits) and the
relay's wider rows (long hex and base64 runs, and a keyword such as `password` or `token` followed
by `:` or `=` and a value).  A message's text is scanned as the text the comment shows, that is
after it is converted to Markdown, so a markup attribute (a Zulip `data-user-id`, a long href) can
never trip a row by itself; a link's URL stays in the text, so a credential inside one is still
caught.  The post itself is relayed as readable Markdown:  its link openers stay escaped and
never live, and its code spans and fenced blocks keep the sender's own formatting.  The
Zulip-shaped row also withholds an opaque 32-character id, a cost
accepted because a Zulip key is the credential most likely to be pasted into Zulip.  A bare
git commit id is not a credential:  a run of 40 lowercase hex characters (SHA-1) 
or 64 (SHA-256) has the shape of a commit id, and the shape alone does not prove it is
one:  it is also the shape of credential material.  So the run is masked, the post that
quotes one is carried, and the value itself is neither stored nor relayed.  A run in a
credential assignment (`token=<id>`,  `"token": "<id>"`,  `client_secret=<id>`) or inside a
URL query (`?sig=<id>`) is not masked and is withheld whole, as it always was.  A
hexadecimal run of 32 characters or more is withheld whole unless it is masked this way.  A keyword in
plain prose, such as "token refresh", is not a hit.  A field with a hit is withheld whole: it shows as "(withheld: it
looked like it carried a credential)", and the log names only the message id and the field.  Neither
the comment nor the log says which row matched, so a reader cannot tell from Linear why one id was
withheld and another was not; the rows above are the whole list.  It is never partly
redacted, because a pattern can find where a credential starts but not always where it ends.  The
message itself is still mirrored as a link, which carries no Zulip text.  A clean label (the
topic, the sender, the stream name) is trimmed and escaped so it cannot open a link, start a code
span or close the quoted string.  An @ is
kept but followed by a zero-width space, so a mention never becomes a live mention.  The relay's log
names an entry by its issue and its first message id, never by its topic.  A message that came in
by a topic rename was never read by the relay, so its block carries no post.

The link on each block names the message id and nothing else: `https://<realm>/#narrow/near/<id>`.
The topic never goes into a URL, because a percent-encoded topic is still the topic, and a reader of
the Linear comment could decode it.  Zulip's `near` narrow operator resolves a message id on its own,
so the link lands on the message and the topic it lives in.  The link is built when the comment is
rendered, from the message id, so no text that came out of Zulip can reach it.

## The whole post (AFC-847)

A post is shown as the sender wrote it.  Zulip's message content is converted to Markdown
(`scripts/simple_tools/zulip_markdown.py`, standard library only): Zulip's rendered HTML and
Zulip's Markdown source both become Markdown, with the paragraphs, the lists, the quotes, the code
and the links kept.  The relay reads the message source (`apply_markdown=false`), so a post
arrives as Markdown already; the same reader handles the rendered HTML a host that reads the
messages API with its default gets.  The shape is read from the start of a post, so a post
that shows markup in its own text or inside a code block keeps that text, and only a post
that opens with a tag is read as HTML.  The sender's own attribute values (an
image's text, an emoji's name, an embedded link's URL) are escaped like the rest of the
text, so they cannot carry markup into the comment.

The comment is bounded, and every cut is said in the comment itself:

- A message is shown up to 10,000 characters (`MESSAGE_CHARS`).  Zulip itself refuses a
  message over 10,000 characters, so the bound cuts no post Zulip sends; a longer one is
  cut and says `_N characters of this message are not shown._`
- The whole comment is at most 50,000 characters (`COMMENT_CHARS`), well inside what Linear
  takes.  The messages that do not fit are the oldest ones, and the comment says
  `_N characters of this topic are not shown._`  A topic whose posts are longer than that
  leaves the oldest of them out of the comment, and the count of the messages that are not
  listed says so.  A second comment for the overflow would break the one-comment-per-topic
  contract, which is the owner's ruling, so it is not done here.
- A message's text is never collapsed into one line: the line structure the sender wrote is the
  line structure the reader gets.

A link in a post is shown, with its text and its URL, and it is not a live link: the opener is
escaped, so a reader cannot click a link out of a post they did not read.  A link whose own text
holds a bracket is shown the same way.  A mention in the prose of a post is never a live mention; inside code the at 
sign is left as the sender wrote it.

The comments that were written before this change hold posts that were cut at 280 characters, and
the cut text is gone: only Zulip has the whole post.  Those comments are repaired, not left as
they are.  Every message carries the format its own quote was rendered in, and an entry is
due for repair while any of its messages still holds a cut quote.  An entry without one is read
from Zulip again (the topic's history), its quotes are rendered again, and the comment is edited
with the whole posts and says that it was re-rendered.  A topic's next message repairs its
comment, and a topic that stays silent is repaired by the cycle, one comment per cycle.  The
standalone relay has no history reader, so its comments keep what they have until the topic speaks
again.

A repair that covered only part of the entry is not a whole repair:  the entry stays due, so a
later cycle tries again, and the comment says how many messages were not read back.  A reader
answers the oldest messages of a topic first, up to its cap, so the repair read goes on, page by
page, until it reaches the entry's own messages.  Every page of that walk shares one deadline,
so the walk is bounded in time and not only in pages.  A message the read passed and did not
hold is no longer in Zulip:  no read can bring it back, so it stops waiting, and the comment says
that it is gone and keeps its earlier text.  A message the entry holds that is newer than the end
of the topic the read reached is gone as well.

A repair that fails is logged with the type of the failure and never with its text, so no
message or topic content reaches the log.  The entry then waits REFRESH_RETRY seconds before it
is tried again, like an entry whose topic could not be read, so a topic that keeps failing does
not take the cycle's one repair every cycle and the other stale comments are repaired too.  A
repair a topic's next message triggers fails the same way:  it is logged with the type of the
failure, the entry waits REFRESH_RETRY seconds, and the message that triggered it is recorded
either way.

## Environment

| Name | Meaning |
| --- | --- |
| `ZULIP_RELAY_EMAIL` | the relay bot's Zulip address |
| `ZULIP_RELAY_API_KEY` | its API key |
| `ZULIP_SITE` | the site of the bot's credentials; it must name the same host as the realm |
| `SIMPLE_TOOLS_REALM` | the realm the relay talks to and links to (default: the fleet realm) |
| `LINEAR_RELAY_TOKEN` | the OAuth app token of the dedicated Linear relay app |
| `LINEAR_RELAY_STATE` | ledger path (default `~/.simple-tools/linear-relay.json`) |
| `LINEAR_RELAY_TEAMS` | override for the team keys |
| `LINEAR_RELAY_DRY_RUN` | `1` logs what it would write, writes nothing to Linear, and keeps its own `.dry-run.json` ledger |
| `LINEAR_RELAY_CHECK_ISSUE` | an issue id for `check` to look up |

A `ZULIP_SITE` whose host is not the realm's stops the relay at start with a message that names
both variables.

Commands:

    python3 -m simple_tools.linear_relay run        # the long-running loop
    python3 -m simple_tools.linear_relay check      # credentials, public streams, Linear auth; no writes

`check` proves the Zulip bot is a member and not an admin, that the public-stream set loads, and
that the Linear token works.  A set that cannot load fails `check`, because `run` relays nothing
until it loads.  A missing issue, or an id Linear refuses as malformed, is reported as an issue
problem, not as an auth failure: those are different problems and the operator needs to know which
one it is.

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
A refused token names only the variable to check, for example
"linear refused LINEAR_RELAY_TOKEN (http 401)".  No key value, prefix, suffix or masked
preview is included in the error, log or stop message.

Failures are sorted by what waiting can change:

- A refused token (HTTP 401, or an authentication error) stops the relay with a message, because
  no retry can fix it.  The container's restart policy brings it back, and it stops again until
  the token is fixed.  A 403 refuses that one request, not the token, so it does not stop the relay.
- A missing issue, a forbidden entity, invalid input or a 403 is final for that write: the entry is
  not retried until a new message in the topic gives it new content.  An issue id Linear does not
  have is looked up again after an hour, so an id named before its issue exists is not lost.  The
  log says which it was, a missing issue or a refused lookup, so a scope problem never looks like a
  quiet relay.  A refused lookup waits the same hour before it is asked again, because waiting
  cannot fix a refusal; the topic's messages are still recorded meanwhile, so the comment made once
  a later lookup succeeds lists them.
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
recent messages, and the timing.  The text one entry holds is bounded as well (60,000
characters): a comment shows at most 50,000, so an entry that held more would hold text no
comment can show, and the ledger would grow with it.  It keys each topic on a digest of the title, and stores the title
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

The ledger is bounded: 500 entries and 16 MB, and the oldest entries are pruned first.  The comment
map outlives that bound (AFC-821).  A pruned entry that has a comment, or a create whose outcome is
unknown, leaves a record in the ledger's `archive` table under the same key: the conversation's
first message id, the comment id, the `pending` flag and when the entry was last touched, and no
topic text, stream name, link or quote.  The topic's next message, whether or not it names the
issue, restores the entry from that record under its original marker and edits the old comment; a
record whose create was never confirmed is looked up by that marker before anything is created.
Hosted by the Operator, the restored entry is refilled from the topic's history first, and when
that read fails the record waits in the archive and the comment is left as it is until a later
message; standalone, the restored entry restarts from the live message.

The archive is optional, so a ledger without one opens as it always did, and the version stays 2.
When it is there it is checked on load like the entries: a table, every key exactly as the relay
writes it, and every record with all four fields of the right type and a finite time.  A malformed
record stops the relay, and the message names its position only.  The archive is bounded at 20,000
records and by the file's 16 MB: its oldest records are dropped first, and the log says how many
("the relay ledger dropped N archived record(s)").  A full archive takes about 3.7 MB of the 16 MB.
A build from before AFC-821 reads a ledger that has an archive, writes the table back as it found
it, and neither adds to it nor restores from it, so a rollback loses only the restores.

## What this does NOT promise

The relay is not exactly-once, and it does not say it is.  Four things can each produce a second
comment for one topic:

1. a lost ledger, or an archived record dropped at the archive's bound;
2. a second relay process on another host, which the file lock cannot prevent across hosts;
3. an uncertain write outcome, where a create landed and its answer was lost;
4. a topic rename or a stream move the relay never saw (one made while it was not running, or while
   its public-stream set was unloaded): the topic's entry, or its archived record, stays under the
   old name, and a mention under the new name starts afresh.

One more leaves an old comment behind without adding one: when a topic is renamed onto a topic that
already has its own comment, the two conversations merge into the surviving comment, and the other
comment is left as it was and is no longer updated, because the app cannot delete comments.  The
destination's comment survives, except that a live entry survives over an archived record, which
holds no message to merge.  Retiring the other comment with an edit that points at the surviving
one waits for the sandbox run to show that an edit works under `read` and `comments:create`.

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

Two more limits, both from the handoff.  For the standalone relay, a queue expiry or a downtime
loses the messages sent in the gap, because it has no history reader.  Hosted by the Operator
(`docs/operator-linear-comments.md`), a topic with a standing comment reads that gap when its next
message arrives, after a restart, after an event queue registers again, or after a message was
dropped while the stream set was unloaded; a topic that stays silent keeps its gap until then, and
the module docstring of `linear_relay.py` lists what else that catch-up does not promise.  And the
permission to edit a comment under the Linear scopes `read` and `comments:create` is UNVERIFIED.
The relay is written for it, and the sandbox test that proves it comes later, after Jay approves
the app.

## Gates that stay with Jay

The relay writes nothing to Linear and holds no credential until Jay approves each of these:

1. **Linear**: a new OAuth application named "Zulip Relay", actor application, scopes `read` and
   `comments:create` only.  The id, secret and token stay out of chat, code and logs.
2. **Zulip**: a new bot, role Member, not admin and not owner.
3. **Infisical**: `ZULIP_RELAY_EMAIL` and `ZULIP_RELAY_API_KEY` in prod `/zulip`, where the
   fleet keeps every Zulip bot's credentials, and `LINEAR_RELAY_TOKEN` in a path of Jay's choice.
4. **Coolify**: after the PR merges, a second service built with Coolify's Docker Compose build
   pack from the same Dockerfile (not a Dockerfile application; the healthcheck paragraph below
   says why), with the environment above, a persistent volume for the ledger with
   `LINEAR_RELAY_STATE` pointing at a file on it, and restart always.  Without that variable the
   ledger sits in the container's home directory, which a redeploy empties, and the relay would
   then start a second comment for every topic.  First run with `LINEAR_RELAY_DRY_RUN=1`, watch
   the log, then unset it.  A dry run keeps its own ledger next to the configured one
   (`linear-relay.dry-run.json` beside `linear-relay.json`), so it never marks a topic written in
   the file the live run will use.

   The image is built for the daemon, so the relay service changes three things.  The image's
   Infisical loader (`scripts/simple_tools/server/infisical_env.py`) copies only the names on its
   allow-list, and the relay's three are not on it, so the service gets them as its own runtime
   environment; adding them to the shared loader would hand the relay's keys to the daemon as well.
   The image's entrypoint always runs `simple-chat` and its command is `daemon run --wait-lock`, so
   the service replaces both: the entrypoint becomes `python3` and the command
   `-m simple_tools.linear_relay run`, with `PYTHONPATH` set to the image's `scripts` folder.  And the
   image's healthcheck watches the daemon's status file, which the relay never writes, so the relay
   service must have that healthcheck disabled, which the compose file described below does;
   restart always still brings the relay back if it exits.

   Coolify's health-check setting cannot turn it off for a Dockerfile application, which is how the
   daemon is deployed.  In Coolify 4.5, the version on the server, such an application always ends
   up with a check: with Coolify's own check off, Coolify reads the Dockerfile's `HEALTHCHECK` and
   waits for it during a rolling deploy, so a relay deploy would be marked unhealthy and rolled
   back; with it on, Coolify runs its own probe instead, an HTTP request by default that the relay
   cannot answer (a command that always passes works only until one deploy runs with the check off,
   after which Coolify keeps the image's check); and its custom Docker options drop
   `--no-healthcheck`.  So the relay service uses Coolify's Docker Compose build pack: a compose
   file that builds the same Dockerfile from the repo root and gives the relay service
   `healthcheck: { disable: true }` beside its entrypoint, command, volume and `restart: always`.  A
   Compose deploy does not wait for a health status.  Coolify reads that file from the repo, so it
   is committed with the service when Jay approves this gate.

The relay runs on the Coolify server instance, the same image as the simple-chat daemon.  No new
Cloudflare Worker is involved.

## Why there is no RELAY row in the seat partition

The handoff suggested a `RELAY = "server"` row in `docs/protocols/simple-tools-partition.toml`.  That
row is deliberately absent.  `config.py`'s `fleet_tags` treats every seat named in the partition as
an eligible sender, so a RELAY row would make the relay a wake target and a tag it could wake, and
the relay holds no agent.  The relay is a service, not a seat, so it takes no row.  Echo's reviewer
confirmed this twice from the source, and `scripts/simple_tools/config.py` was read again for this
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
