# The Operator's role: the fleet's listener, its letter office and its mirror

What the Operator is, what already exists, and which parts are still a decision rather than a task.  The wake path is [zulip-wake-routing.md](zulip-wake-routing.md).

## 1: What The Owner Has Ruled

These are the owner's rulings.  Where the code does not do one yet, [the listener design, section 9](simple-tools-listener.md#9-not-yet-in-the-code) says so.

1.  **The Operator is the thing that listens.**  It holds the eavesdropper, it runs on the server,
    and it is the service identity the fleet talks to.

2.  **It acts under its own identity.**  It does not reply to a Zulip post directly, it does not
    impersonate the seat it is speaking to, and it never says a seat's wake tag.

3.  **It is deterministic, and it never DMs anyone.**  Its work is defined event inputs, routing and
    matching rules, and defined outputs.  It asks nobody a clarifying question, it never answers on
    behalf of a platform, and anything that would be a direct message goes in Side-Chats.

4.  **It issues session letters.**  A letter is given in exchange for the session id and a set of
    facts about the conversation, a brief list of what the conversation is about, where it lives,
    and the other details a reader needs to make sense of it.

5.  **It keeps the session and thread registry, and that registry lives on the Fleet Reference
    Google Sheet.**

6.  **It mirrors a Zulip conversation onto its Linear issue.**  One standing post per issue and
    topic, holding the conversation and a link back to the Zulip topic, and a separate post for an
    individual message that names an issue its topic does not.

7.  **It wakes a seat on the seat's own surface.**  MM on Lark, Jet on WhatsApp, and every other
    seat on Zulip.  The registry already records where a session lives, so the surface is a
    property of the session rather than a choice the Operator makes per message.

8.  **A seat wake needs no agent.**  A wake is deterministic capture, a pick and a delivery
    ([zulip-wake-routing.md](zulip-wake-routing.md)).  The only model is an optional decider, off by
    default, after the router's deterministic steps, and it only picks a chat:  it never writes a
    reply.

## 2: What already exists

| Requirement | Where it lives today | State |
| --- | --- | --- |
| The Operator as a service seat | `scripts/simple_tools/config.py`: the `OPERATOR` seat requires `bot = "Operator"`, `creds = "infisical"` and `wake = "inbox"` | built |
| The Operator's identity, read from the environment | `scripts/simple_tools/service_identity.py`, `scripts/simple_tools/dm_policy.py`: `operator_user_id(env, pinned)` | built |
| No Operator direct messages | `scripts/simple_tools/dm_policy.py` still lets the Operator open a direct message (`check_new_dm()`, and `ZulipClient.operator_status_dm` for its status message), and `scripts/simple_tools/daemon.py` still DMs the owner (`dm_owner`, `_run_broadcast_audit`) | not built:  these move to Side-Chats |
| Session letters | `scripts/simple_tools/operator_registry.py`: the `sessions` and `alias_assignments` tables, one live holder per `(platform, letter)`, generation on recycle | built (AFC-412) |
| The registry on the Fleet Reference sheet | `scripts/simple_tools/operator_sheets_export.mjs`: the exporter writes the Sessions (`2033000102`) and Events (`2033000103`) tabs and never touches Reference (`2033000101`) | built (AFC-412 PR2) |
| The Zulip to Linear mirror | `scripts/simple_tools/linear_relay.py`: one comment per `(issue, topic)`, edited in place, ten minute batching, direct messages and private streams excluded | built (AFC-502, PR #899) |
| The listener's read API the eavesdropper reads | `scripts/simple_tools/listener_api.py`: five signed GET endpoints under `/listener/v1/<SEAT>/<record>` | built (AFC-330 part B) |
| The Operator waking MM on Lark | `docs/protocols/lark-mm-bridge.md`: the Operator runs the bridge with `--signal` and the text, and the bridge, the only holder of the Lark bot identity, sends it, runs the seat turn and puts the answer back in the same chat | built |
| The Fleet Reference sheet adapter | `scripts/simple_tools/operator_sheets_google.mjs`: the Google Sheets transport behind the exporter's adapter surface (`inspect()`, `validateHeaders()`, `readRows()`, `appendSession()`, `updateSessionRow()`, `appendEvent()`), with `SESSION_HEADERS` and `EVENT_HEADERS` as the authoritative header contract | built (AFC-428) |

Two facts about the existing code shape the discussion below.

**The relay already detects an issue named in a message body.**  `linear_relay.py` calls
`find_issue_ids(self.regex, topic, content)`, so a message that names an issue its topic does not
is relayed today.  What is wrong is the keying, not the detection:  `ledger_key(issue, stream_id,
topic)` folds that message into the standing comment for `(issue, topic)`, so a message about an
issue the topic is not about lands in a comment that claims the topic is about it.

**The wake tag.**  `scripts/simple_tools/adapters.py` sets `[SEAT·wake] re=<id>` as an `http`
routine's `reply_prefix`, which that routine posts as itself.  The Operator writes no `·wake` tag.

**The Lark path already carries the attribution Jay wants.**  The Lark bridge records that the
sender travels with the turn, so the seat inbox and the Zulip mirror attribute the Operator's
signal to the Operator and it does not wake the seat as if the owner had asked.  That is the
behaviour the Operator needs on every surface, and on Lark it is already the behaviour.

## 2a: The wake surfaces

A seat is not where the Operator is.  MM is on Lark, Jet is on WhatsApp, and the other seats are
on Zulip.

**MM on Lark is done.**  The Operator already has the signal path, and the signal is attributed
to the Operator rather than to the owner, which is the rule.

**Jet on WhatsApp is not.**  The bridge is a single reader and never sends:  its own module
docstring says so.  So the Operator has no way to wake Jet on WhatsApp, because there is nothing
to send with.  That is the same missing work as the reply leg in AFC-545, and it is the reason
that item matters beyond its own description:  without an outbound path there is no Operator wake
on WhatsApp at all.

**Zulip is the general case.**  Each platform's router delivers the wake, and the registry already carries
`platform` and `verified_host` for every session, so the surface a seat is woken on is a property
of the session rather than a decision the Operator makes per message.

## 3: What does not exist, and the shape it should take

### 3.0: What WISE and ECHO recommend

Their input, recorded as theirs, in the topic "Operator role: listener, letters, relay and how it
talks" on Oct 25, 2026.

**Keep each seat's own records, and give the Operator the fleet view.**  A seat keeps what is
already its own, and the Operator holds the fleet-wide view over it.  This answers Jay's "the
Operator should have its eavesdropper on the server" without a seat losing its own ground, and it
does not make the Operator answer in a channel:  the fleet view is a read it serves
deterministically, and anything it says about it to a seat goes in a Side-Chats topic.

**A session summary carries purpose, the owning issue, the source thread, repository and pull
request links, the current status, and a contact.**  A letter read six weeks later should still
say what the conversation was for and who to ask.

**Location is a coarse class plus a stable identifier, supplied by the session registration and
never inferred.**  Server, Mac or cloud, and then the seat or instance that holds it.

**The summary can be corrected later.**  A conversation changes what it is about, so the field
has to be writable after the letter is issued.

**An off-topic issue mention keeps its own attribution and link, and is never folded into the
topic's standing comment.**  Batching those by issue may cut the noise, and that batch is kept
distinct from the standing comment rather than merged into it.

**Withdrawn.**  WISE and ECHO first recommended that the Operator answer a seat in a new post in
the same topic, and then withdrew it.  The Operator answers nobody:  it never answers for a platform,
and it never DMs.

**What they asked for, and what they asked about.**  They asked for the document, for the wake tag
composer, for the merged owner-in-the-direct-message guard, for the Lark bridge implementation, for
the source of the new wake-surface requirements, and for the provenance of the three decisions so
that Jay is not asked the same thing twice.  All of that is in section 3.3 and section 7.

### 3.1: The Operator as the listener with the eavesdropper

The read API exists and the hosted Worker answers `eavesdropper_status`, `eavesdropper_inbox`,
`eavesdropper_owner_queue`, `eavesdropper_wakes` and `eavesdropper_held_back` for a hosted seat.

The open question is one of grant, not of plumbing:  does a seat keep reading its own records
through the eavesdropper, or does the Operator become the single reader of the whole fleet's records?
The second is a much bigger grant, because an Operator that reads every seat's inbox can see
everything the fleet sees.  It is also the only shape that matches "the Operator is the thing that
listens".

The shape that fits:  the Operator holds the eavesdropper for the whole fleet, a seat's own
eavesdropper read is narrowed to what that seat already owns, and the Operator serves the
fleet-wide read deterministically rather than answering questions in a thread.  The decision for
Jay is whether a seat's own eavesdropper read survives at all.

### 3.2: How the Operator acts

The Operator is deterministic.  Its behaviour is defined event inputs, routing and matching rules,
and defined outputs.

1.  **A message it sends is its own, not a reply.**  The Operator's own message is composed and
    sent as a new message.  A reply carries the thread's context and reads as the conversation's
    own voice, which is the impersonation the owner ruled out.

2.  **It posts no wake reply and wears no seat's wake tag.**  The owner ruled that "The operator
    should never be answering on behalf of a platform", so the Operator posts no wake reply, no
    owner ack, no route page and no quota notice, for any seat.  `Daemon.post` is a refusal that
    records `operator_never_answers` on the wake's ledger row.  Its own notes are tagged
    `[OPERATOR·note]`.

3.  **It never DMs and never asks.**  The Operator asks nobody a clarifying question, and anything
    that would be a direct message goes in Side-Chats.  The code still sends a candidate list
    (`scripts/simple_tools/operator_candidates.py`, `ask_operator_for_candidates`) and a registration
    question (`operator_registration_ask.py`), and still DMs the owner;  those are what this rule
    removes.

**No model answers.**  A wake is delivered to the picked chat, and that chat answers.  The router's
optional decider, off by default, only picks a chat.  Its model is declared in
`[decider.consult_backup]` ([zulip-wake-routing.md](zulip-wake-routing.md#the-deciders-declaration)),
and no code calls it yet.

### 3.3: The letter's fact bundle

The registry row already carries most of what Jay asks for.  Reading the `sessions` table:

| Jay asks for | Field today | Note |
| --- | --- | --- |
| The session id | `native_session_id` | present |
| Where it lives:  server, Mac, cloud | `verified_host` | present, and it already holds the coarse class:  the registry's `--location` argument is documented as "the fleet location (mac, server, cloud), never an OS hostname", and the live row reads `mac` |
| What it is about | `title`, `project` | present, but a title is not a brief list of what the conversation covers |
| The rest of the facts | `seat`, `platform`, `profile_scope`, `state`, `wake_capability`, `registered_at`, `last_seen_at`, `closed_at`, `thread_link`, `apple_note_link`, `source` | present |

So the gap is one thing, not two:  a short summary of what the conversation is about, carrying
purpose, the owning issue, the source links, the current status and a contact, and correctable
after the letter is issued.  The location is already recorded, so it needs a column for the
summary and not for itself.

**The one proposed sheet column**, on the Sessions tab beside the ones that already exist:

| Column | Carries | Written by |
| --- | --- | --- |
| About | purpose, owning issue, source thread, repository and pull request links, current status and contact, correctable after the letter is issued | the registry, and whoever owns the conversation |

The Sessions tab already carries a Host column for the location, so the tab needs one new
column and not two.

**The sheet adapter is `scripts/simple_tools/operator_sheets_google.mjs`.**  It is the Google
Sheets transport the exporter writes through (`inspect()`, `validateHeaders()`, `readRows()`,
`appendSession()`, `updateSessionRow()`, `appendEvent()`), and its `SESSION_HEADERS` and
`EVENT_HEADERS` are the authoritative header contract the tabs are validated against.  So the new
column is a change to that header contract first:  the sheet's own header list and validation have
to accept it before a row can carry it.

**Exporter adapter contract.** `inspect()` returns
`{ sessions: { rowCount }, events: { rowCount } }`. Each `rowCount` is the positive
safe-integer physical grid row count, including the header. `readRows(table,
{ startRow, limit })` returns an array, or `{ rows: [...] }`, of
`{ rowNumber, cells }` records with 1-based physical row numbers inside the requested
page. The exporter also accepts `row` and `values` aliases. Empty or short values
pages do not end the physical grid scan. Duplicate matching Record IDs or Event IDs
refuse a write. `findSessionRowByRecordId` and optional fast lookups are unsupported:
they cannot prove destination uniqueness.

The runner MUST hold one exclusive cross-process writer lease across reconciliation,
write and acknowledgement, with no concurrent manual row restructuring. An error
keeps the earliest outbox row pending and stops the batch so later snapshots cannot
overtake it. After a confirmed Sheets append and failed registry acknowledgement,
replay first scans the immutable ID: an existing event is acknowledged without
another append. A truly unknown Sheets transport outcome can commit late. An
exclusive writer alone does not prove that an ID absent from a read will stay
absent, so the runner must not automatically resume such an uncertain drain until
the outstanding request is settled or safely reconciled. The exporter does not
persist a general durable `ok`/`unknown`/`refused` mutation ledger; the required
runner handling of truly unknown Sheets writes remains an integration gap.
A duplicate needs destination repair before draining resumes; skipping it or adding
a failure/quarantine state would require a separate ordering and schema design.
Repeated full-grid scans are an explicit safety/performance tradeoff; a reusable
index needs a separately qualified runner lease and destination-mutation contract.

**The citations for the pieces this discussion rests on.**

*  The wake tag:  `scripts/simple_tools/adapters.py`, an `http` routine's `reply_prefix`.
*  The owner-in-the-direct-message guard, merged Oct 25, 2026:  `scripts/simple_tools/zulip.py`,
  `direct_recipient_sets()` and `check_dm_guard()`, and the client call site that runs the guard for
  a POST and a PATCH.  Pull request 947, AFC-469.
*  The Lark bridge:  `docs/protocols/lark-mm-bridge.md` and `scripts/lark-mm-bridge.py`, and the
  Operator's signal path through `--signal`.
*  The relay:  `scripts/simple_tools/linear_relay.py`, `find_issue_ids()` and `ledger_key()`.
*  The registry and the letters:  `scripts/simple_tools/operator_registry.py`, the `sessions` and
  `alias_assignments` tables.
*  The exporter:  `scripts/simple_tools/operator_sheets_export.mjs`.

### 3.3a: The Linear issues a session works on (AFC-694)

Each session record carries `issues`: the Linear issues the chat has worked on, so the wake
router can link a wake that names an issue to the chat working on it (contract posted in Zulip,
AFC-694, message 639354953; Jay approved it Nov 10, 2026).  The field is additive: a record
from before it has none, and every answer that leaves it out means "no issues known".

**The field.**  A list of at most 8 entries `{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}`,
newest `seen_at` first.  It is kept in the `session_issues` side table (schema version 3), so an
older registry keeps every row and gains the table the next time a writer opens it.

**The id rule.**  An id is a Linear team key, a dash and a number, in capitals.  The keys are
`LINEAR_TEAM_KEYS` in `scripts/simple_tools/operator_registry.py`, a copy of `TEAM_KEYS` in
`scripts/linear-board/board` (the board's routing table), and a test keeps the two equal.

**Writing it.**  The registry's `update` op (the API's `POST /registry/v1/update`, and the CLI's
`update --issues '<JSON list>'`) takes an optional `issues` list that replaces the stored one.  The
registry normalizes it: an invalid id or an entry with no zoned `seen_at` is dropped without a word,
one entry is kept per id (its newest `seen_at`), and the list is cut to 8.  An `update` without
`issues` leaves the list alone, and an empty list clears it.  A closed session's list does not change.

**Reading it.**  The answers that describe a record (`touch`, `update`, `resolve`, and every item of
`history`) carry `issues` when the session holds any.  `history` items also carry `last_seen_at`,
the session's last activity (every touch, title and update stamps it): the router ranks sessions by
it first and falls back to `assigned_at`.

**Who writes it.**  The session-letter hooks, at the start of each turn (`claude_letter` for Claude
Code, `platform_letter` for the other platforms whose hook data names a transcript or a folder). 
They read three sources:

*  the transcript lines added since the hook last looked (the note keeps a byte offset, and a turn
  reads at most the newest 256 KB), taking ids from the chat's own words and tool inputs but not
  from a tool's output, which can name issues the chat never worked on;
*  the git branch of the chat's folder (`git rev-parse --abbrev-ref HEAD`, two seconds at most),
  read in either case, so `claude/afc-694-slug` names AFC-694;
*  the topic of any `simple-chat post --topic "..."` command or `posted id N to #channel › topic`
  line, including one in a tool's output.

The ids are merged into the note's list (seen now, newest first, at most 8), and the hook sends the
whole list through the registry side's `issues` op only when the set of ids changed.  Nothing in
this path can block or break a prompt: a failure is silent, a registry that refuses the call (one
too old to know the op) is asked again after ten minutes, and a transcript outside the platform's
own folder is never read.

### 3.4: The Linear mirror, one message at a time

The relay's key becomes two-keyed:  an issue named by the topic keeps the standing
`(issue, topic)` comment;  an issue named only by the message gets its own comment, keyed by the
message, holding that message and a link to the Zulip topic.  The existing ten minute batching and
the one-standing-comment behaviour for topic-named issues stay exactly as they are.

## 4: What is a decision and what is a task

**The owner's decisions, and nothing else can settle them.**  What is left open of each:

*  **The Operator holds the eavesdropper, on the server.**  That is ruled.  What is open is the
    second half that WISE and ECHO answered:  a seat keeps its own records, or the Operator
    becomes the single reader of everything the fleet sees.
*  **The Operator never DMs.**  That is ruled:  anything that would be a direct message goes in
    Side-Chats.  The code still has the direct messages listed in section 3.2.
*  **A message that names an issue its topic does not gets its own Linear post.**  That is ruled.
    What is open is the granularity WISE and ECHO raised:  one comment per message, or those
    batched by issue and kept distinct from the topic's standing comment.

**Tasks, once the decisions are made.**

*  The Operator's own tag, and the post-versus-reply rule, in the two places the wake tag is
    composed.
*  Move the Operator's direct messages to Side-Chats, and remove the candidate list and the
    registration question.
*  The location column and the short summary column, in the registry and on the sheet.
*  The relay's second key, for the message-named issue.
*  The Operator's own voice on the server, so it authors its own threads instead of a seat
    reporting on them.
*  The outbound WhatsApp leg, which is the same work as AFC-545 and is also what gives the
    Operator a way to wake Jet on WhatsApp.

## 4a: A proposed division of the work

Three slices, one seat each, and each is asked rather than assigned.  A seat that wants a
different slice, or both, says so.

**The Operator's egress and its own voice.**  The tag the Operator speaks under, the rule that a
message it sends is its own and never a reply inline, and the eavesdropper grant question.  This is
the listener and identity layer, where the wake tag is composed and where the seats are told apart.

**The WhatsApp surface.**  What it takes for the Operator to wake Jet on WhatsApp, which is the
outbound leg and the bridge's send path, and the receive half that has to be running first.

**The registry and the letter.**  The location a session lives at, the short summary of what a
conversation is about, and the new column on the Fleet Reference sheet, because the sheet adapter
is deliberately another seat's slice and the column has to be asked for, not assumed.

**The line that does not move.**  The letter rules stand:  a letter is for a tagged address, only
the Operator issues one, and a seat never chooses its own letter.  The no-wake signal stands:  a
leading eyes emoji means never wake anyone, per message, on Zulip.

## 5: What this discussion does not change

The Operator is not a seat and does not get a letter.  A letter is for a tagged address and only
the Operator issues one.  A direct message needs no letter.  The no-wake signal from AFC-468 is
unchanged:  a leading eyes emoji means never wake anyone, and it is a per-message signal on
Zulip only.

## 6: Open questions, and which are answered

1.  Does a seat need the eavesdropper for its own records once the Operator holds it for the
    fleet, or is the Operator's answer enough?  **Answered, in recommendation form, by WISE and
    ECHO:**  keep each seat's own records, and give the Operator the fleet view.  Jay's call is
    still needed on the shape, and this is the recommendation that goes with it.
2.  Where should the Operator's message live?  **Answered:**  the Operator answers nobody, and a
    message it sends is its own, never a reply and never a direct message.
3.  What belongs in the short summary of what a conversation is about, so that a letter read six
    weeks later still says what it was for?  **Answered, in recommendation form, by WISE and
    ECHO:**  purpose, owning issue, source thread, repository and pull request links, current
    status and contact, and correctable later.
4.  Is one Linear comment per message the right granularity when a topic is not about the issue, or
    does that flood the issue?  **Answered, in recommendation form, by WISE and ECHO:**  keep each
    message's own attribution and link, never fold it into the topic's standing comment, and
    batching those by issue is a separate thing that may cut the noise.

**Still open, and only Jay can settle them.**

*  Whether the seat keeps its own eavesdropper read, or the Operator becomes the single reader.
*  Whether the off-topic issue messages are batched by issue or one comment each.
*  Whether sending on WhatsApp is approved at all, which is the same approval AFC-545 waits on.

## 7: The rollout, and who holds each part

*  **The Operator's own voice on the server.**  The Operator seat is already a service seat with
    its credentials from Infisical and its identity read from the environment.  What is missing is
    the Operator publishing its own threads rather than a seat reporting on them.
*  **The Lark path.**  Built, but not running on the Mac from Nov 2 to Nov 7, 2026 (AFC-490).  The installed bridge could not find the managed runtime's `simple_tools` package and crash-looped on `ModuleNotFoundError` (over 10,000 launchd runs, last exit 1).  `scripts/lark-mm-bridge.py` now looks in its own scripts folder and then in the managed runtime, and the Mac's copy is reinstalled from it.  A signal reaching MM's Lark chat has to be checked again after that reinstall, and this line says so until it has been.
*  **The WhatsApp receive half.**  Owned by CODEX as the approved executor, with Jet owning the
    subscription, the binding ceremony and the acceptance receipts.  Blocked on the deployed
    Worker being behind the merged read-routing, on the poller being stopped with the binding
    absent, and on the rollout approval.
*  **The WhatsApp outbound half.**  AFC-545, the missing code.  Blocked on the approval to send
    at all, which the current approval excludes in its own words.
*  **The sheet columns.**  A request to the adapter's owner, because the exporter is written
    against an adapter surface that is deliberately another seat's slice and is not in this
    repository.
