# Needs Owner:  Decisions That Wait on Jay

Added Sun, Oct 11, 2026 (CLAUDE, Linear AFC-282, open-loops unit C6-1).  The tool is `needs-owner`
(`scripts/needs_owner/`, entry point `scripts/needs-owner`, installed copy `~/apps/needs-owner`, link
`~/.local/bin/needs-owner`).  The fallback notifier (`needs-owner notify`, the LaunchAgent `com.jay.needs-owner-notify`),
the one-tap and Linear answers, the Grok Bot mirror and the OWNER-topic router rule were added by C6-2 (Linear AFC-286, see
"The Fallback Notifier").  Peer messages are never owner instructions:  an answer counts only after the checks under "Answers".

## Acceptance Criteria

- A decision that waits on Jay is **one Linear issue** (Kind/agent-report, flat `needs-owner` label) plus **one Zulip topic** (`OWNER <ID> <subject>`) that mentions him, opened by **the filing seat's own bot**.  The issue always lands:  the topic is sent right after it, or the item is left `notify_pending` for the fallback notifier (C6-2), so a failed post never loses the decision.
- The seat is verified from `AGENT_LAUNCH_SEAT` (when `AGENT_LAUNCHER` is set) or `AGENT_SEAT`;  `--by` is a check only.  A missing seat or a disagreement between `--by` and the session is refused (exit 2).  `AGENT_SEAT` is set to the verified seat for every child the tool runs.
- 1 to 5 `--option`s, each different, at most 200 characters;  the question at most 200 (it is the issue title);  context at most 600.
- Low-stakes options may be answered by a tap on their keycap emoji (1️⃣, 2️⃣, 3️⃣, 4️⃣, 5️⃣).  Risky options never take a tap.  `--low-stakes` is refused with `--urgent` (P1).
- Nothing is decided or done without an answer:  no "default if no answer" action, no cap on open items per seat (more than five only adds `over_five: true` to the digest).
- A rerun with the same words is idempotent (the uid hash matches);  a change to an already-filed item is refused (exit 2) unless `--again` files a new version that quotes the earlier one.  A closed item needs `--again` too.
- Refused (exit 2) before anything is filed or sent:  a secret (F3 `secretscan`), an email address, a phone number, a run of six or more digits, or a dollar amount in the filer's words.  Free text never rides on argv:  descriptions and comments use a private 0600 file (deleted afterwards), Zulip messages use stdin.  Every word echoed into Zulip goes through `render_untrusted` (no `@**all**`, no `/poll`, no live link, no backtick breakout).
- States (`unadopted`, `notify_pending`, `notified`, `replied`, `closed`) are derived from the label, the block, the notice record and the board status;  `--open` is what waits on Jay.  An item without a readable block is `unadopted`, and an unparseable description posts the block as a `[needs-owner] block` comment so the decision still lands.
- An answer is verified before it is acted on:  a tap counted only when the reactor's user id equals the pinned `owner_user_id`;  a Zulip reply through `simple-chat verify-owner-msg` (owner id, human client, right topic, newer than the notice);  a Linear comment whose author equals `OWNER_LINEAR_USER_ID`.  The records say a reply exists, nothing more.
- Exit codes:  0 done (a filed item with its notice pending is still done);  1 something failed (Linear, the network, a missing tool);  2 refused;  3 `status` or `adopt` named something that is not a needs-owner item;  4 to 7 the `simple-board` CLI's own code, passed through when a write failed (7 means Linear writes are off).
- **C6-2.**  `needs-owner notify` (CLAUDE only) sends the notice of an item whose own seat could not (after 2 minutes, urgent first, under the caps), records the first answer to each open item as one `owner replied:` Linear comment, wakes the filer with one directed note (or leaves an entry in `pending-CLAUDE.json` for CLAUDE's own items), asks again once for two different taps and once for a tap on a risky option, and mirrors Grok Bot `needs-owner-request` blocks into Linear.  It never closes, edits or restates an item, posts nothing while paused, holds one lock, and logs ids and reason words only.  Records count only from who may write them.  OWNER topics never peer-wake, except its note.
- Stable copy at `~/apps/needs-owner` runs under `python3 -I` with only the package, `fleet_knobs/` (the notifier's knob reader) and the three F3 files it loads by path (`agent_sync/identity.py`, `secretscan.py`, `textsafe.py`).  The install proof is `--help` and `--version` of that copy under `python3 -I`:  both load the package and the three F3 files before they parse anything, so a copy that lacks or has damaged one fails them (exit 1).  Tests use fakes that parse argv with the real `simple-board` and `simple-chat` parsers and print the real `read --json` keys.
- A post that may have landed is found again by its `Ref:` line (`notify.find_notice`):  only a message from a bot (`is_bot` true) of the filing seat or of CLAUDE, only when the line stands alone and is not quoted, and the oldest such message wins.  A person's quote-and-reply, or a peer who copies the public uid, is never taken for the notice.

## Why This Exists

Jay, Sun, Oct 11, 2026, on where decisions should live:

> needs-owner items should be "easy for me to find in Linear too ... just as an extra way to see them quickly.  also, they should message me on Zulip about it."

and on how low-stakes answers should feel:  "in one tap like the questions tonight".  His standing reminder
binds this design:

> "I often distract away from things but never intend to undo what I said prior unless explicitly say so"

So a decision that waits on Jay is **one Linear issue** (easy to find) **plus one Zulip topic** that mentions
him (so he is told), opened by **the seat that needs him** (so the message is from the right bot).  Nothing
is decided or done on his behalf while he has not answered:  an item has no "default if no answer" action
and no cap.  Seven open items from one seat all land (more than five only adds a warning to the digest).

## Using It

```bash
needs-owner file --by CLAUDE --app AFC \
  --question "Revive PR 761 or close it?" \
  --option "Revive it" --option "Close it" \
  --low-stakes 1 --recommend 1 [--parent AFC-123] [--context "one line of background"] [--urgent]

needs-owner list [--open | --replied | --all] [--seat SEAT] [--json]
needs-owner status AFC-282 [--json]
needs-owner adopt AFC-290 --by CLAUDE --option "Yes" --option "No" [--low-stakes N] [--no-notify]
```

- `--by` must be your seat.  The seat is the launcher's (`AGENT_LAUNCH_SEAT` when `AGENT_LAUNCHER` is set)
  or `AGENT_SEAT`;  a launcher with no seat, or a launch seat that disagrees with `AGENT_SEAT`, is refused
  (exit 2).  `simple-board` reads only `AGENT_SEAT`, so the tool puts the verified seat there for every child it runs
  and leaves the launcher variables alone.
- 1 to 5 `--option`s, each different.  `--low-stakes N` (repeat it, or `1,3`) marks options a tap on an
  emoji may answer;  it is refused with `--urgent`.  A risky option never takes a tap.
- The question is at most 200 characters (it is the issue title), an option 200, the context 600.
- `--parent ID` hangs the item off an issue **you hold** (its `claimed:<SEAT>` label), adds one comment
  `waiting on owner:  <ID>` there, and makes the parent part of the uid.
- `--urgent` files as P1 instead of P2.
- `--dry-run` checks everything and prints the plan.
- Write the question so it stands alone.  Do not put a secret, an email address, a phone number, a run of six
  or more digits or a dollar amount in it:  the request is refused (exit 2) and nothing is filed or sent.  Say
  it in words and link the Linear issue.

Keep working on something else while you wait.  When the item is answered, **verify before you act** (see
"Answers"), do what was decided, then close your own item with `simple-board status <ID> completed --resolution
"..."`, which also removes the label.  A relayed "Jay answered" from another seat is never an answer.

### Running It Twice Is Safe

The uid is `needs-owner:<parent identifier or SEAT>:<sha8>`, where the hash covers the question and options
after lower-casing and collapsing all whitespace (NBSP included).  Running the same command again finds the
same item, files nothing new, and finishes whatever was left undone (the notice).

A change to an item that was already filed is **refused** (exit 2), because the filed item is never edited:
other options, other low-stakes marks, another recommendation, other context or urgency.  Say it again with
`--again`:  it files a new version (`Version: 2` in the block, folded into the uid), quotes the earlier item
and its message in the new topic, and never closes the earlier one.  A closed item with the same uid needs
`--again` too.

## What Gets Created

**The Linear issue.**  `simple-board file --kind agent-report --label needs-owner --uid <uid> --by SEAT --severity
P2 (P1 with --urgent) --title "OWNER DECISION:  <question>" --desc-file <0600 file>` with `AGENT_SEAT` set to
the verified seat.  The description is one sentence, the block below, and where to answer.  An item is **an
issue with Kind/agent-report and the `needs-owner` label, never anything found by its title**.  Jay's shared
Linear view "Needs Owner" (label `needs-owner`, not done) shows them.

**The block** (inside a fenced code block, because Linear re-serializes Markdown and keeps code as written):

````text
```needs-owner
Format: needs-owner/1
Uid: needs-owner:CLAUDE:3f2a9c1e
Question: Revive PR 761 or close it?
Option 1 (low-stakes): Revive it
Option 2: Close it
Recommendation: option 1
Parent: none
Filed by: CLAUDE
Context: none
```
````

The fence is found by its content (a fenced block whose first line is `Format: needs-owner/1`), not by its
info string, because Linear's editor may normalize a code language it does not know to a bare fence.  If a
description does not read back as a block at all, `file` posts the block as a `[needs-owner] block` comment
(the way `adopt` does) and says so on stderr, so the decision still lands.

`Version`, `Again of` and `Mirror of` lines appear only when they apply.  Every value is one clean line
(control and invisible characters dropped, whitespace collapsed, a backtick turned into an apostrophe), so a
value can neither start another field nor close the fence.  The reader is strict:  an unknown or repeated
field, a gap in the option numbers, a bad uid or a seat that is not a token makes the item **unadopted**.

**The Zulip topic**, from the filing seat's own bot, immediately after filing:

```
simple-chat post --as SEAT --session needsown --json --topic "OWNER <ID> <subject, 40 characters at most>" --to-owner -- -
```

with the message on standard input.  The mention of Jay comes from the owner id pinned in
`~/.agent-sync/listener.toml` (`--to-owner`), never from a typed name.  The message holds the question and
options (every word the filer wrote goes through `render_untrusted`), a keycap emoji on each low-stakes
option **by its own position** (a low-stakes option 3 is 3️⃣) with "Tap to answer", the recommendation, the
Linear link, "Answer here or as a comment on the Linear issue.", and a reference line with the uid.

**The records**, ordinary Linear comments signed by whoever wrote them:

| Comment | Written by | Means |
|---|---|---|
| `[needs-owner] notified msg <id>` then `topic: <topic>` | the filer | the filer's bot posted the OWNER topic;  `<id>` is the message |
| `[needs-owner] notify_pending` then `reason: <word>` | the filer | the post failed or the seat has no bot credential;  the fallback notifier (C6-2) sends it |
| `[needs-owner] block` then the fenced block | `adopt` | an issue filed earlier became an item |
| `owner replied:  msg <id>` / `reaction <n> on msg <id>` / `comment <id>` | the notifier (C6-2) | Jay replied;  **never holds the answer** |

Readers accept any whitespace where the writers put one.  Reasons are `no_credential`, `send_failed`, `no_agent_sync`, `unsent` and `held`
(`adopt --no-notify` marks the item `held`, so the fallback notifier leaves it alone until it is announced for real).

**Who may write a record.**  Anyone who can comment can type a record, so a record counts only when the comment is signed by a seat that may
write it (the tag line the board writes for the seat that wrote the comment):  a notice (`notified`) or a `notify_pending` mark from the
filing seat or from CLAUDE, a reply (`owner replied:`) only from CLAUDE.  A copied record from a peer is just a comment:  it cannot hide an
item from Jay's list or point the taps at another message.

**A post that may or may not have landed** (simple-chat exit 5 or 6, or a timeout) is never retried blind:  the
topic is read for the reference line first, and only a missing line becomes `notify_pending`.  A rerun looks
in the topic before it posts, so a crash between the post and its record cannot post twice.  The lookup is
`notify.find_notice(runner, topic, uid)`, and it is strict about who and what it takes for the notice (see the
next paragraph).

**For the fallback notifier (C6-2).**  An item shows `notify_pending` when a post landed but both attempts to
record it failed.  Before it posts, the notifier calls `notify.find_notice(runner, topic, uid, filer=<the filing seat>)`
(it imports the function and does not copy the logic), or it would post a second time:  it runs as CLAUDE, so the filer's own
bot has to be allowed too.  The function is sender-filtered, unquoted and oldest-first:

- **Sender.**  It keeps a message only when `simple-chat read --json` says `is_bot` is true and `seat` is the
  filing seat's or `CLAUDE` (the fallback notifier posts as CLAUDE for any seat).  A person never counts, whatever
  their quote says, and neither does a bot of another seat that copied the uid, which is public in the topic.
  A sender whose kind `simple-chat` could not look up has `is_bot` null and is left out:  the worst outcome is a
  repeated post, never a quote taken for the notice.
- **Unquoted.**  The reference must be a line of its own, `` Ref:  `<uid>` ``, that is not a `>` line and not inside
  a closed code fence (Zulip's quote-and-reply wraps the original in a fence of three backticks and the word
  quote, with no `>` on its lines).  A mention inside a sentence does not count.
- **Oldest.**  Of the messages that pass, the lowest id is the notice.

The notifier records that id as `[needs-owner] notified msg <id>`, so `verify-owner-msg --after <notice>` and a
tap reaction key on the bot's own post and Jay's answer counts as newer than it.  The record formats and the
states live in `records.py` and `items.py`:  import them rather than copying the patterns.

**A Grok Bot item** (`file --mirror-of <topic link> --context "for GB-<ROLE>"`, used by the C6-2 notifier as
CLAUDE):  files as above, posts **no** new topic, records the persona's own message as the notice
(`notified msg <the persona's message id>` with that topic), and replies in that topic with the Linear link.
The link must be on this realm, in #Work-In-Progress, in a topic that starts `OWNER `, and name its message
(`/near/<id>`);  the message id is folded into the uid, so two personas asking one question are two items and
a repeat of the same request is not.  The caller has already checked who opened the topic.

**Adopting** (`adopt ID --by SEAT --option ...`):  for an issue the seat filed earlier (the day-0 items,
live step L0b).  The board has no verb that edits a description, so adopt adds the label when it is missing,
posts the block as a comment, and then announces it like `file`.  It takes the same content flags (the
question defaults to the title);  it refuses another seat's issue, a closed one and a Kind other than
agent-report;  and it never re-files.

## States and the JSON

`list --json` prints an array;  `status ID --json` one object.  Neither holds an answer's text.

| State | Means |
|---|---|
| `unadopted` | the label is there but there is no valid block (filed by hand, or the block was damaged) |
| `notify_pending` | filed, not announced yet |
| `notified` | the OWNER topic is open |
| `replied` | an `owner replied:` record exists;  the filer has not closed it |
| `closed` | the board status is completed, deployed, wontfix, duplicate or canceled |

`--open` (the default) is unadopted, notify_pending and notified:  **what waits on Jay**.  Closing with
`simple-board status` removes the label, so only an item closed in Linear's own UI stays listed (as `closed`, with
`--all`).

| Field | Notes |
|---|---|
| `id`, `identifier`, `url`, `app`, `created_at`, `age_hours` | from the board |
| `seat`, `seat_verified`, `for_seat` | `seat` is the block's "Filed by";  `seat_verified` is true when the text that carries the block was signed by that seat (the issue's own tag line, or the block comment's author);  `for_seat` is `GB-<ROLE>` on a mirrored item |
| `question`, `options`, `low_stakes`, `recommend`, `context`, `parent` | from the block (an unadopted item shows its title and no options) |
| `severity`, `urgent` | P2 or P1 |
| `topic`, `notified_msg_id` | from the notified record, else the computed topic |
| `replied` | null, or `{kind: msg|reaction|comment, id, option, by}` |
| `state`, `status` | the state above, and the board's status word |
| `over_five` | true on every row of a seat with more than five items waiting (a warning for the digest, never a refusal) |
| `uid`, `version`, `block_ok`, `edited`, `problem` | `edited` is true when the question or options no longer fit the uid hash (somebody changed the block) |

## Answers

Three ways, and a consumer **verifies before it acts** (the records say a reply exists, nothing more):

1. A tap on a low-stakes option's emoji in the OWNER topic:  counted only when the reactor's user id is the
   pinned `owner_user_id`.  Risky options never take a tap.  A **filer** that wants to check a tap itself reads the notice with
   `simple-chat read --json --topic "<topic>" --since <notice id - 1> --include-self` (each message lists its `reactions` as
   `user_id`, `reaction_type`, `emoji_code`, `emoji_name`) and compares `user_id` with `owner_user_id` from `simple-chat whoami --json`.
2. A typed reply in the OWNER topic:  `simple-chat verify-owner-msg <id> --topic "<topic>" --json` (the owner's
   user id, a human client, the right topic, newer than the notice).  The client check is a heuristic, not
   authentication.
3. A Linear comment whose author is `OWNER_LINEAR_USER_ID` (a fleet knob):  `simple-board show <ID> --json` lists each comment with
   `author_id` and `author_app` (Linear's own account fields, which a typed tag line cannot change), and an app's comment never counts.

The notifier does these checks and writes the record (see "The Fallback Notifier");  a filer that acts on an answer repeats the check for the kind of
answer the record names, because a record says only that a reply exists.

## The Fallback Notifier

`needs-owner notify [--dry-run] [--json]` is the one verb only CLAUDE may run (any other seat is refused, exit 2).  The LaunchAgent
`scripts/launchd/com.jay.needs-owner-notify.plist` runs it every 5 minutes as `/opt/homebrew/bin/python3 -I ~/apps/needs-owner/needs-owner notify`
with `AGENT_SEAT=CLAUDE` and no launcher.  It reads Linear, Zulip and the K1 knobs, and writes only Linear comments, Zulip posts, the files below and (for
a Grok Bot request) one Linear item.  It reads no secret.  A failed read of Linear sends nothing and exits 0.

**What one run does, in this order.**  Each step is cut off from the others:  one failing never stops the rest, and none prints a traceback.

1. **Re-validate** every open item:  the block parses strictly, `seat_verified` is true, no secret or personal data is in the question, options or
   context, and the question and options still hash to the uid (an item edited after filing is skipped and its reason logged).
2. **Replies.**  For each `notified` item it reads the topic after the notice (`simple-chat read --json --since <notice - 1>`) and records the first
   answer, looked for in this order:
   - a **typed message** that `simple-chat verify-owner-msg` passes (the pinned owner's user id, a human client, the right topic, newer than the notice).
     A message from a bot or another person is never asked about, a rejected one is never asked about twice, and a message that is only `pause` or `resume`
     controls the notifier and is never an answer (to answer an option that is called Pause, write a sentence);
   - a **tap**:  a reaction on the notice whose `user_id` is the pinned `owner_user_id` (from `simple-chat whoami --json`, never a name), whose
     `reaction_type` is `unicode_emoji` and whose `emoji_code` is a keycap from 1️⃣ to 5️⃣ (`0031-20e3` and so on) naming an option the item has, and that option is
     **low-stakes**.  A custom emoji, anyone else's reaction and any other emoji are not taps.  **Two different options** from Jay mean "ask again"
     (neither counts), and a tap on an option that is **not** low-stakes is not an answer ("this one needs a typed reply");
   - a **Linear comment** whose `author_id` is `OWNER_LINEAR_USER_ID` and which is not an app's (`simple-board show --json` gives `author_id` and `author_app`, Linear's
     own account fields;  a typed tag line cannot change them).  With the knob empty, no Linear reply is accepted.

   The record is one comment from CLAUDE, `owner replied:  msg <id>`, `owner replied:  reaction <n> on msg <id>` or `owner replied:  comment <id>`.  It is written
   once per item.  The first answer stands:  a reaction or message that comes later does not undo it (Jay says so to the filer if he means to, and the filer decides).
3. **Notices.**  An item still `notify_pending` two minutes after it was filed, adopted or marked (urgent items first) is posted from CLAUDE with the header
   `For <SEAT> (filed by <SEAT>, not checked by CLAUDE).`, after `find_notice` has looked for a post the filer's bot already landed.  A failed post backs off
   (5 minutes, then doubling up to an hour).
4. **Notes.**  The record is followed by one directed note in the item's topic that wakes the filer:  `simple-chat --as CLAUDE --session needsown post --topic "OWNER <ID> ..." --to <SEAT>`,
   saying "Jay replied on <ID>:  <Linear link>" and where the reply is (the message, the message a tap is on, or the Linear comment), "verify it before you act", and that
   the item stays open.  It never repeats the answer.  For a Grok Bot item the note goes to the persona (`for GB-<ROLE>`).  For **CLAUDE's own items** a post that mentions
   yourself wakes nobody, so the entry goes into `~/apps/needs-owner/state/pending-CLAUDE.json` (identifier, link, kind, message or comment id, option, topic and time;
   no words) for the CLAUDE session-start skill to read, and is removed once the item is closed.  A note that cannot go out (the seat has no bot, the caps are full, the pause)
   stays due and is retried, 12 times at the most.
5. **Nudges**, once each:  "I saw two different reactions ... I counted neither" and "I saw a reaction on option N, which needs a typed reply".
6. **The Grok Bot mirror** (below).

**The caps and the pause.**  Every Zulip post the notifier makes counts:  `NEEDS_OWNER_CAPS` (default 3 urgent an hour, 6 an hour, 30 a day) caps notices, notes, nudges and
mirror link-backs together, urgent notices first.  The counts are kept in `notify-state.json` and carry over between runs.  Nothing is dropped for a full cap:  it waits for the next
hour or day.  **Paused** (the Infisical knob `NEEDS_OWNER_NOTIFY_PAUSED` true, or Jay's verified `pause` as the whole message in **any** OWNER topic, for
24 hours or until his `resume`;  each topic is read for what was said since the last run, and the first run reads no history) stops every post and every Linear item the notifier would make.  Replies are still recorded in Linear (a comment is silent) and their notes wait,
and the file for CLAUDE's items is still written.  To stop it at once:  `launchctl bootout gui/$(id -u)/com.jay.needs-owner-notify`.

**Its files**, in `~/apps/needs-owner/state/` (directory 0700, files 0600, ids and numbers only, never a question, option, answer or message):  `notify-state.json`, `notify.lock`
(one run at a time;  a lock older than 30 minutes, or whose process is gone, is taken over) and `pending-CLAUDE.json`.  The log is `~/Library/Logs/needs-owner-notify.log`:  a line per
event with an identifier and reason words (`notified`, `replied`, `note-sent`, `skip why=edited_after_filing`, `deferred cap=per_hour`, ...), nothing else.

**The Grok Bot mirror.**  A Grok Bot persona cannot run the Mac CLI and has no Linear write, so it opens its own OWNER topic by REST and puts a fenced block in the message:

````text
```needs-owner-request
{"question": "Rotate the Plumber's key?", "options": ["Approve", "Decline"], "seat": "GB-PLUMBER", "low_stakes": [1], "recommend": 1, "context": "one line", "urgent": false}
```
````

Each run lists the recent OWNER topics (`simple-chat topics --json`), skips the topics of existing items and the ones already read at their last message, and for the others requires
that the topic's first message is from a **Grok Bot persona** (a bot whose seat is `GB-<ROLE>`, whose `seat_app` is `grok-bot` and whose email is exactly `<role>-grok-bot@<this realm>`),
and that the block is in a message from that same persona, once, with only the keys above (`app` and `format` are accepted and ignored).  The question is 1 to 200 characters, there are
1 to 5 different options, `seat` equals the sender's seat, `low_stakes` and `recommend` name real options, `urgent` never comes with `low_stakes`, and the same secret and personal-data
refusals as `file` apply.  An accepted request becomes `needs-owner file --by CLAUDE --app AFC --mirror-of <topic link> --context "for GB-<ROLE>: ..."` (in process, with `AGENT_SEAT=CLAUDE`),
which posts no topic of its own, records the persona's message as the notice and replies in the topic with the Linear link.  Three requests a run at the most, once per message (the uid folds
in the message id, and the message ids already mirrored are kept in the state), so within about 5 minutes of the post.  A block that is wrong gets **one** reply with a reason word
(`seat_is_not_the_sender`, `not_json`, `secret_or_personal_data`, ...) and no Linear item.  A topic opened by anyone else is ignored, and so is a persona's block in a topic someone else opened.

**The router rule** (`scripts/agent_sync/router.py`, described in `docs/protocols/agent-sync-listener.md`):  nothing a peer posts in a topic that starts `OWNER ` wakes anyone, except this
notifier's note (CLAUDE's bot, tag `[CLAUDE·needsown→`).  The owner's own message there wakes only the seats it mentions.

**The knobs** (Infisical, project "AI Fleet Coordinator", prod only;  read through `fleet_knobs`, one fetch a run, the last-known-good copy when Infisical is unreachable):

| Knob | Meaning |
|---|---|
| `NEEDS_OWNER_NOTIFY_PAUSED` | `true` pauses the notifier.  An outage longer than the 48 hour copy reads `false` |
| `NEEDS_OWNER_CAPS` | `urgent_per_hour=3,per_hour=6,per_day=30` |
| `OWNER_LINEAR_USER_ID` | Jay's Linear user id (a UUID).  Unset or empty:  no Linear comment is accepted as his |

A refused environment (a non-prod `INFISICAL_ENV`) or an unreadable knob stops the run before anything is read or sent (exit 1).

**Loading it** (live step L16, after `install.sh --apply`):  copy the plist to `~/Library/LaunchAgents/`, run `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jay.needs-owner-notify.plist`,
restart the simple-chat listener so the router rule applies, and add the rows to the live process list (`docs/MAC-LOCAL-PROCESSES.md` is the mirror).  `mac-process-watch.sh` keeps it loaded.  Try it first
with `needs-owner notify --dry-run --json`.

## Safety Rules

- The filer's words (question, options, context) are cleaned, then scanned for secrets (F3 `secretscan`)
  and for personal data, and a hit refuses the whole request naming the field, never the text.
- Free text never rides on argv, except that the question is the issue title and its first 40 characters are
  the topic's subject:  descriptions and comments go through a private file (mode 0600, removed afterwards)
  and Zulip messages through standard input.  Nothing a filer wrote shows up in `ps` for longer than that.
- Everything echoed into Zulip goes through `render_untrusted`:  no `@**all**`, no `/poll`, no live link, no
  backtick breakout.
- This tool writes the `block`, `notified` and `notify_pending` records.  The `owner replied:` records are the
  fallback notifier's (C6-2) to write, as CLAUDE, and only after the check for the kind of answer.  Nothing here closes, edits or restates an item.
- No credential is read, printed or logged.  A `notify_pending` reason is a word, not an error text.

## Exit Codes

0 done (an item filed with its notice pending is done:  the decision landed);  1 something failed (the
board, the network, a missing tool);  2 refused;  3 `status` or `adopt` named something that is not an item;
4 to 7 the `simple-board` CLI's own code when a write failed (7 means Linear writes are off).

## Installing

`scripts/needs_owner/install.sh` prints a plan;  `--apply` copies `needs-owner`, the `needs_owner` package
(without its tests) and the three F3 files it loads by path (`agent_sync/identity.py`, `secretscan.py`,
`textsafe.py`) and `fleet_knobs/` (the K1 knob reader the notifier uses, without its tests) to `~/apps/needs-owner`, links `~/.local/bin/needs-owner`, and proves the copy by running
`python3 -I ~/apps/needs-owner/needs-owner --help` and `--version`, which load the package and the three F3 files
(a missing or damaged file fails them).  The proof does not run a verb;  the tests do that through a copy (`notify` also runs from it, and says so when
`fleet_knobs/` is missing).  It leaves `~/apps/needs-owner/state/` alone.  `needs-owner` is an on-demand helper (docs/MAC-LOCAL-PROCESSES.md);  nothing it does stays running.  The
scheduled fallback notifier is the LaunchAgent above, which the owner session loads.  BotFleet routine allowlists
name `/Users/jay/.local/bin/needs-owner file` with fixed flags and the question and options as quoted
arguments.

## Tests

`cd scripts && python3 -m unittest discover -s needs_owner/tests -t .` (CI runs it;  about four minutes).  The router rule is tested in
`agent_sync/tests/test_router.py`, the `reactions` and `owner_user_id` fields in `agent_sync/tests/test_safety_commands.py`, and the comment account fields
in `linear-board/test_board.py` (`CommentAuthorTests`).  The notifier tests run `notify` in process with a fixed clock and fake K1 knobs (and one class against
the real `fleet_knobs` and a fake `infisical`), and a LaunchAgent test parses the plist.  The fakes parse
argv with the real `simple-board` and `simple-chat` parsers, so a wrong flag fails here too, and the fake board hands
descriptions back as Linear does (escaped `[ ] ~`, `*` bullets).
