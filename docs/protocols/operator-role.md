# The Operator's role: the fleet's listener, its letter office and its mirror

A discussion, for WISE, ECHO and JET to weigh in on, written from Jay's rulings in
`#Work-In-Progress` on Oct 25, 2026 and in the MiniMax Code pane on Oct 26, 2026, and checked
against the code on `main` at `12521ef35`.  The Oct 29 additions describe the status message built after that check.

This document says what the Operator is, what already exists, what does not, and which parts
are a decision rather than a task.

It records what WISE and ECHO recommended, and it records the one they withdrew:  they had framed
the Operator as a conversational assistant answering seats' questions.  Jay's Oct 25 clarification
was that the Operator is mostly deterministic and does not converse with users, and his Oct 26
ruling supersedes that:  the Operator may speak to anyone and may converse with them, in a direct
message, so that conversations do not get bloated.  What still stands of the withdrawal is where
the Operator converses:  a channel post is the wrong place for it, and the question about a tag to
wake stays bounded rather than opening into a conversation.

## 1: What Jay has ruled

**These are rulings and intended design, not verified current behaviour.**  Jay's words on Oct 25
and Oct 26, 2026, in the MiniMax Code pane, and where a seat relayed them, in `#Work-In-Progress`.
The current runtime is described in section 2 and section 7, and the two are kept apart on
purpose:  a ruling says where the design is going, and it is not a claim that the running code
does it today.

**Where each ruling came from, so that it is not re-derived from a later paraphrase.**

*  The Operator as the listener with the eavesdropper on the server, acting under its own
    identity, never wearing a seat's wake tag, and asking in a post rather than a direct message:
    Jay, in the MiniMax Code pane on Oct 25, 2026, **superseded in its asking half on Oct 26,
    2026**:  the Operator asks by direct message now.
*  The direct message being the fleet's exception, used only by the Operator and only when the
    message cannot be paired with a specific thread to receive it:  Jay, in the MiniMax Code pane
    on Oct 25, 2026, **narrowed on Oct 26, 2026**:  the direct message is where the Operator's
    conversation goes, and not only the fallback for a message that pairs with no thread.
*  The Operator waking a seat on the seat's own surface, MM on Lark and Jet on WhatsApp:  Jay, in
    the MiniMax Code pane on Oct 25, 2026.
*  The no bot-to-bot direct message rule, that a bot may send one only when Jay is an actual
    participant, checked by authenticated user id:  Jet relaying Jay's own answer, `#Work-In-Progress`
    message 633794973, Oct 23, 2026.
*  On Oct 25, 2026, Jay said, relayed by ECHO and WISE in `#Work-In-Progress`, that the Operator is
    mostly deterministic and does not converse with users, and that the Muse spark 1.3 contributor
    or the MiniMax M3.1 fallback is for the headless consultant.  **Superseded in its first half
    on Oct 26, 2026**, by the ruling below:  the Operator may converse, in a direct message.  The
    consultant boundary in this bullet is **refined on Nov 7, 2026** by the wake ruling below:  the
    consultant runs only when deterministic code cannot decide, on the owner's own OpenRouter preset.
*  The Operator being able to speak to anyone and to converse with them, and doing that in a
    direct message rather than a channel post, so that conversations do not get bloated:  Jay, in
    the MiniMax Code pane on Oct 26, 2026, verbatim, "operator can speak to anyone or converse with
    them but should in DMs so it doesn't make conversations get bloated".  This supersedes the
    Oct 25 framing that the Operator does not converse with users, which is kept above as part of
    the record.
*  The Operator's one to one status messages:  Jay, in the Claude Code pane on Thu, Oct 29, 2026,
    about 3:35 AM, verbatim, "DM one to one since from bot to another bot but since deterministic
    bot not something about like the project or details I want to be able to see".  This settles the
    guard for the Operator's deterministic status messages:  they go to the sender alone, with
    Jay left out, because they carry no project detail.  A message with detail, such as the
    candidate list, is unchanged and keeps Jay in the conversation.  The Operator's clarifying
    question is not part of this yet:  its reply leg is not built.
*  The wake design:  Jay, to session AA on Nov 7, 2026, verbatim, "Wake agent should use operator
    for headless agent when needed only if unable to determine with deterministic code, and then
    should use a preset I made on openrouter", "Only the operator does headless runs sometimes",
    "Seat wake doesn't need an agent" and "Consultant is an agent needed minority of the time
    hopefully".  The preset is his own, built on the Muse spark 1.3 contributor ("Not just normal
    one").  ECHO reports it as `@preset/operator-inbox-router` version 3 (served by
    `meta/muse-spark-1.3-contributor`, backup `minimax/minimax-m3.1-flash`);  it was held when this
    was written, and the Mac has declared it since Nov 7, 2026, 10:14 PM, now as the decider's
    `[decider.consult_backup]` (section 3.2).  Jay also objected to the Operator ever being
    called "MM-wake":  the wake tag is `[OPERATOR·wake]`, and a wake is not tied to MM or to any
    seat.  This is a ruling, not current behaviour:  per-seat model wakes still run until Phase 2,
    and PR #1506 only records how often each start was avoidable (section 3.2).

1.  **The Operator is the thing that listens.**  It holds the eavesdropper, it runs on the server,
    and it is the service identity the fleet talks to.

2.  **It acts under its own identity.**  It does not reply to a Zulip post directly, it does not
    impersonate the seat it is speaking to, and it never says a seat's wake tag.

3.  **It is mostly deterministic, and where it converses, it converses by direct message.**  Its
    work is defined event inputs, routing and matching rules, defined outputs, and one bounded
    conversational exception:  a question about a tag to wake, which the Operator may put to
    anyone.  A direct message is the fleet's exception, used only by the Operator, and under Jay's
    Oct 26 ruling it is where the Operator's conversation goes, to anyone, and not only when the
    Operator cannot pair the message with a specific thread to receive it.  Jay's Oct 26
    ruling adds that the Operator may converse at all, and that it does so in a direct message
    rather than in a channel post, so that conversations do not get bloated.  Jay's consultant
    boundary is separate:  the headless consultant (since Nov 7, 2026, run only when deterministic
    code cannot decide, on the owner's own OpenRouter preset) is not the Operator's own voice,
    and separating incoming content from
    authority, and consultation from execution, are the barriers that make that safe.  Jay's own
    note is that a barrier may also help with prompt-injection risk, which is a rationale and not
    a proof of immunity.

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

8.  **A seat wake needs no agent, and only the Operator starts a model, sometimes.**  Jay's Nov 7,
    2026 ruling, quoted above:  a seat wake is deterministic delivery, and the Operator starts its
    headless consultant only when deterministic code cannot decide, on the owner's own OpenRouter
    preset.  A wake belongs to no seat's tag:  it is `[OPERATOR·wake]`, never "MM-wake".  The running
    code does not do this yet:  a seat configured with a `claude`, `muse` or `openrouter` wake still
    starts a model for itself, and converging those onto the Operator's one consultant run is Phase 2,
    which is pending (section 3.2).

## 2: What already exists

| Requirement | Where it lives today | State |
| --- | --- | --- |
| The Operator as a service seat | `scripts/simple_tools/config.py`: the `OPERATOR` seat requires `bot = "Operator"`, `creds = "infisical"` and `wake = "inbox"` | built |
| The Operator's identity, read from the environment | `scripts/simple_tools/service_identity.py`, `scripts/simple_tools/dm_policy.py`: `operator_user_id(env, pinned)` | built |
| The Operator's direct message rule | `scripts/simple_tools/dm_policy.py`: `check_new_dm()` lets the Operator open a direct message for its one clarifying question and, with `status=True`, for its status message; `scripts/simple_tools/zulip.py`: `check_dm_guard()` refuses a direct message that does not carry the owner, and `ZulipClient.operator_status_dm` is the one exemption, for the status message alone | built for the status message, narrower than the Oct 26 ruling |
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

**The wake tag is composed for every seat, including the Operator.**  `scripts/simple_tools/wakes.py`
builds `"[%s·wake] re=%d\n%s"`, and `scripts/simple_tools/adapters.py` sets the same shape as the
adapter's `reply_prefix`.  There is no path today that lets a seat speak in its own right with
its own tag instead of the wake-reply tag.

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

**Zulip is the general case.**  The wake adapter is per platform and the registry already carries
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
deterministically, and anything it says about it to a seat is a direct message.

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

**Withdrawn, and not carried forward.**  WISE and ECHO first recommended that the Operator answer
a seat in a new post in the same topic, and then withdrew it:  they had framed the Operator as a
conversational assistant answering seats' questions, and Jay's clarification at the time was that
the Operator is mostly deterministic and should not converse with users.  Jay has since moved past
that framing himself:  on Oct 26, 2026 he ruled that the Operator may speak to anyone and may
converse with them, in a direct message, so that conversations do not get bloated.  So the
withdrawal stands for where the Operator converses, a channel post being the wrong place for it,
and it no longer stands for the reason it was given.  The discussion here is about defined event
inputs, routing and matching rules, outputs and bounded exception behaviour, and the Operator's
question about a tag to wake stays a bounded question rather than an open-ended conversation.  The
rest of their recommendations stand only where they are consistent with that role.

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

The Operator is mostly deterministic.  Its behaviour is defined event inputs, routing and matching
rules, defined outputs, and one bounded exception.  It may converse, and it may speak to anyone,
and where it converses it converses by direct message rather than in a channel post, so that
conversations do not get bloated.  The question it asks is still the bounded one:  it asks about a
tag to wake, and it is not an opening for an open-ended conversation.

Three concrete changes carry that:

1.  **A message it sends is its own, not a reply.**  The Operator's own message is composed and
    sent as a new message.  A reply carries the thread's context and reads as the conversation's
    own voice, which is the impersonation Jay is ruling out.

2.  **The Operator does not wear a seat's wake tag.**  The wake tag is composed in two places,
    `wakes.py` and the adapter's `reply_prefix`, so this is two places:  the `OPERATOR` seat
    composes its own tag, and the wake-reply tag stays for a seat answering on the owner's behalf.

    **Oct 30, 2026 addition.**  The Operator now also RELAYS a headless wake's reply (owner
    ruling, Oct 30, 2026: automation that composes text for a seat never posts as the seat's bot,
    and the owner said yes to the reply going out as the Operator in the trigger's topic).  That
    relay's first line is `[OPERATOR·wake] re=<id>` (`wakes.compose_reply`, `OPERATOR_TAG`), not
    a seat's tag:  the owner ruled the same day that the tag is the Operator's, and on Nov 7, 2026
    he objected to the Operator ever being called "MM-wake", because a wake is tied to no seat.
    The tag is the label the loop guard and the router read (a `·wake` post never wakes anyone),
    and the seat the wake was for is in the wake ledger row.  A seat's own `[SEAT·wake]` tag
    survives only in an `http` routine's `reply_prefix`, which that routine posts as itself.  The
    Operator's own messages (its questions, its
    status messages, the `@all` reminder, tagged `[OPERATOR·note]`) keep their own tags.  See
    `simple-tools-listener.md`, "The Operator's voice".

    **Nov 10, 2026 supersession.**  The relay above is gone.  The owner ruled that "The operator should never be answering on behalf of a platform", so the Operator posts no wake reply, no owner ack, no route page and no quota notice, in a channel or in a direct message, for any seat and any wake type.  It sends its own alerts and notes and asks no question (owner ruling, Nov 10, 2026:  the Operator does not ask clarifying questions).  `Daemon.post` is a refusal that records `operator_never_answers` on the wake's ledger row.

3.  **The pairing test decides whether the Operator speaks, and the direct message carries what
    it says.**  A message that pairs with a thread gets the Operator's own message, and that
    message travels by direct message to the seat that asked, with a link to the thread, because a
    conversation belongs in a direct message and not in a channel.  A message that pairs with no
    thread gets the Operator's question, and that goes by direct message too.  The direct message
    is the fleet's exception, used only by the Operator:  that question and the exchange it opens,
    and, as a separate direct message, the candidate list that tells the sender of a tagged address
    which sessions the letter could mean (`scripts/simple_tools/operator_candidates.py`).  That list
    carries the fleet's internal session inventory, so it goes only to the pinned owner or to a user
    the owner has allowed in `[operator] dm_allow`, and the gate that decides it is
    `operator_dm_allowed()` in `scripts/simple_tools/daemon.py`.  Jay's Oct 26 ruling is what moved
    it:  the Operator may speak to anyone and converse with them, and it converses by direct
    message rather than in a channel post, so that conversations do not get bloated.  The question
    itself is bounded:  it asks about a tag to wake, and it is not an opening for an open-ended
    conversation.

**The audience of that one question, stated so it cannot drift into user conversation.**  The
question may be addressed to anyone, and it goes by direct message, because Jay's Oct 26 ruling
lets the Operator speak to anyone and converse with them, in a direct message rather than in a
channel post.  When it is a seat, the question is a seat-to-seat message about a tag, and it
belongs to the fleet's internal exception.  When it is a human, the Operator opens a direct
message with that person and does not start a thread with them, because a channel post is where
the conversation gets bloated.  The question stays bounded either way:  it asks about a tag to
wake, and it is not an opening for an open-ended conversation.

**What that costs, and how Jay has ruled.**  The owner-in-the-conversation guard, merged on Oct 25,
2026, requires Jay in a direct message, so a question that goes by direct message has to carry him
as a participant under the guard as it stands.  That was in tension with "the Operator does not
converse with users", and the two ways out were:  the question carries Jay and is addressed to the
seat, or the question does not go by direct message at all.  Jay's Oct 26 ruling settles it the
other way:  the Operator converses by direct message, it does not carry Jay, and the guard gains
an Operator exemption.  That exemption is written for the Operator's deterministic status
message alone (`ZulipClient.operator_status_dm`, `operator_send.py`, Oct 29, 2026), so any other
Operator direct message still has to carry Jay.

**The headless consultant is a different thing.**  Jay's boundary is that the Muse spark 1.3
contributor or the MiniMax M3.1 fallback (Oct 25, 2026;  refined on Nov 7, 2026 to the owner's
own OpenRouter preset, built on that contributor model) is for the headless consultant, and it is
not the
Operator's own voice.  So the Operator's deterministic outputs and the consultant's reasoning are
separate, and the consultant is not what the Operator listens with.  Jay's own note is that
separating incoming content from authority, and consultation from execution, are the useful
barriers here, and that a barrier may also help with prompt-injection risk, which is a rationale
and not a proof of immunity.

**When the consultant runs (owner, Nov 7, 2026).**  "Wake agent should use operator for headless
agent when needed only if unable to determine with deterministic code, and then should use a preset
I made on openrouter", "Only the operator does headless runs sometimes", "Seat wake doesn't need an
agent" and "Consultant is an agent needed minority of the time hopefully".  So the consultant is the
only model run, it is the Operator's, and it starts only when deterministic code cannot decide.  What
runs today is not that yet:  a seat configured with a `claude`, `muse` or `openrouter` wake still
starts a model for itself, with any configured fallback behind it.  (The Mac's Muse inbox bridge,
which also consulted a model, was deleted on Nov 10, 2026, on the owner's instruction.)  PR #1506 adds
a record-only model gate that records, for every model start, why it happened and whether a
deterministic rule could have decided it, and `simple-chat status` counts the avoidable ones;  it
changes no decision (`simple-tools-listener.md`, section 3.4a).  Phase 2 is pending and not built:
PR #1506 lists AA's readings of these lines, which are readings and not rulings, that it would
remove the `claude`, `muse` and `openrouter` seat wakes and their fallbacks in favour of the
Operator's one consultant run and fix the per-seat model run an `@all` starts.  ECHO's candidate
for the consultant's preset, `@preset/operator-inbox-router` version 3, was held when this was
written.  The Mac's `listener.toml` has declared it since Nov 7, 2026, 10:14 PM, with the owner's Nov 6
line as the reason and no `review_by`, first as the deleted Muse inbox bridge's consult backup (the
AFC-649 known-cap mode) and now as the decider's `[decider.consult_backup]`
([zulip-wake-routing.md](zulip-wake-routing.md), The Decider's Declaration).  No code calls it now
that the bridge is gone;  making it the consultant's standing runtime is still pending Phase 2, and no
release by ECHO's coordinator is recorded in the repository.

**The direct message consequence, and what Jay has now ruled.**  AFC-469, merged on Oct 25, 2026,
says a direct message may go out only when the owner is in the conversation, checked by user id.
The Operator's direct message does not carry Jay, because Jay's Oct 26 ruling is that the Operator
converses by direct message and that it may be addressed to anyone, so the guard has to let the
Operator through.  That change is made for the Operator's status message:  the AFC-469 guard
has one exemption, `ZulipClient.operator_status_dm`, enforced in `ZulipClient.request` on the
exact shape of the call:  a client verified as the pinned Operator bot, still authenticating as it
and still holding the owner pin it was checked against, one recipient, and a content that is
exactly a text the fixed vocabulary builds (the tag with anything else after it is refused).
`dm_policy.check_new_dm` accepts `status=True` for the Operator.  The text is built from a fixed
vocabulary in `operator_status_text.py`, with no project, topic or quoted message in it.  Every other
Operator direct message, the candidate list included, still carries Jay, and the Operator's
clarifying question waits for its reply leg.

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

*  The wake tag composer:  `scripts/simple_tools/wakes.py`, `"[%s·wake] re=%d\n%s"`.
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

**Jay's decisions, and nothing else can settle them.**  All three have a main shape from Jay on
Oct 25, 2026, and the second was settled in full by his ruling of Oct 26, 2026, so what is left is
the residual, so that nobody asks him the same thing twice.

*  **The Operator holds the eavesdropper, on the server.**  That is ruled.  What is open is the
    second half that WISE and ECHO answered:  a seat keeps its own records, or the Operator
    becomes the single reader of everything the fleet sees.
*  **The Operator asks by direct message, and the direct message is the fleet's only exception.**
    That is ruled.  Jay's Oct 26 ruling settles the audience as well:  the Operator may address
    anyone, and the direct message does not carry Jay, so the owner-in-the-conversation guard
    (AFC-469, pull request 947) gains an Operator exemption.  That exemption is written for the Operator's status message alone
    (Oct 29, 2026);  the rest of the Operator's conversation is still open.
*  **A message that names an issue its topic does not gets its own Linear post.**  That is ruled.
    What is open is the granularity WISE and ECHO raised:  one comment per message, or those
    batched by issue and kept distinct from the topic's standing comment.

**Tasks, once the decisions are made.**

*  The Operator's own tag, and the post-versus-reply rule, in the two places the wake tag is
    composed.
*  The pairing test that decides whether the Operator speaks, and the direct message that carries
    what it says.
*  The AFC-469 owner-in-the-conversation guard gains an Operator exemption, so that the
    Operator's direct message does not have to carry Jay:  written for the status message on
    Oct 29, 2026, and still to do for the clarifying question and the rest of the conversation.
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
message it sends is its own and never a reply inline, the pairing test that decides whether the
Operator speaks, and the eavesdropper grant question.  This is the listener and identity layer,
where the wake tag is composed and where the seats are told apart.

**The WhatsApp surface.**  What it takes for the Operator to wake Jet on WhatsApp, which is the
outbound leg and the bridge's send path, and the receive half that has to be running first.  This
slice also owns the question of what a question about a tag to wake looks like when the seat lives
on WhatsApp rather than on Zulip.

**The registry and the letter.**  The location a session lives at, the short summary of what a
conversation is about, and the new column on the Fleet Reference sheet, because the sheet adapter
is deliberately another seat's slice and the column has to be asked for, not assumed.

**The line that does not move.**  The letter rules stand:  a letter is for a tagged address, only
the Operator issues one, a direct message needs no letter, and a seat never chooses its own
letter.  Jay's Oct 26 ruling does not move that line:  the Operator may converse, it converses by
direct message, and a conversation needs no letter.  The no-wake signal stands:  a leading eyes
emoji means never wake anyone, per message, on Zulip.

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
2.  Where should the Operator's message live, so that a seat that asked sees the answer in the
    thread it asked in, without the Operator ever replying to the post that asked?  **Withdrawn
    by WISE and ECHO and not carried forward:**  "the Operator answers in the thread" is the wrong
    shape to design for, because Jay's Oct 26 ruling puts the conversation in a direct message so
    that conversations do not get bloated.  The reason it was withdrawn, that the Operator does
    not converse with users, is superseded.  What stands is the narrower rule:  a message it sends
    is its own, never a reply.
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
