# The Operator's role: the fleet's listener, its letter office and its mirror

A discussion, for WISE, ECHO and JET to weigh in on, written from Jay's rulings in
`#Work-In-Progress` on Oct 25, 2026 and in the MiniMax Code pane on Oct 26, 2026, and checked
against the code on `main` at `12521ef35`.

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
    consultant boundary in this bullet is unchanged.
*  The Operator being able to speak to anyone and to converse with them, and doing that in a
    direct message rather than a channel post, so that conversations do not get bloated:  Jay, in
    the MiniMax Code pane on Oct 26, 2026, verbatim, "operator can speak to anyone or converse with
    them but should in DMs so it doesn't make conversations get bloated".  This supersedes the
    Oct 25 framing that the Operator does not converse with users, which is kept above as part of
    the record.

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
    boundary is separate:  the Muse spark 1.3 contributor or the MiniMax M3.1 fallback is for the
    headless consultant, not for the Operator's own voice, and separating incoming content from
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

## 2: What already exists

| Requirement | Where it lives today | State |
| --- | --- | --- |
| The Operator as a service seat | `scripts/agent_sync/config.py`: the `OPERATOR` seat requires `bot = "Operator"`, `creds = "infisical"` and `wake = "inbox"` | built |
| The Operator's identity, read from the environment | `scripts/agent_sync/service_identity.py`, `scripts/agent_sync/dm_policy.py`: `operator_user_id(env, pinned)` | built |
| The Operator's direct message rule | `scripts/agent_sync/dm_policy.py`: `check_new_dm()` lets the Operator open a direct message only for its one clarifying question, and `scripts/agent_sync/zulip.py`: `check_dm_guard()` refuses a direct message that does not carry the owner, with no Operator exemption | built, narrower than the Oct 26 ruling |
| Session letters | `scripts/agent_sync/operator_registry.py`: the `sessions` and `alias_assignments` tables, one live holder per `(platform, letter)`, generation on recycle | built (AFC-412) |
| The registry on the Fleet Reference sheet | `scripts/agent_sync/operator_sheets_export.mjs`: the exporter writes the Sessions (`2033000102`) and Events (`2033000103`) tabs and never touches Reference (`2033000101`) | built (AFC-412 PR2) |
| The Zulip to Linear mirror | `scripts/agent_sync/linear_relay.py`: one comment per `(issue, topic)`, edited in place, ten minute batching, direct messages and private streams excluded | built (AFC-502, PR #899) |
| The listener's read API the eavesdropper reads | `scripts/agent_sync/listener_api.py`: five signed GET endpoints under `/listener/v1/<SEAT>/<record>` | built (AFC-330 part B) |
| The Operator waking MM on Lark | `docs/protocols/lark-mm-bridge.md`: the Operator runs the bridge with `--signal` and the text, and the bridge, the only holder of the Lark bot identity, sends it, runs the seat turn and puts the answer back in the same chat | built |
| The Fleet Reference sheet adapter | the exporter depends on an adapter surface (`inspect()`, `validateHeaders()`, `readRows()`, `appendSession()`, `updateSessionRow()`, `appendEvent()`) that is deliberately another seat's slice, so the exporter is written against it rather than owning it | built, owned elsewhere |

Two facts about the existing code shape the discussion below.

**The relay already detects an issue named in a message body.**  `linear_relay.py` calls
`find_issue_ids(self.regex, topic, content)`, so a message that names an issue its topic does not
is relayed today.  What is wrong is the keying, not the detection:  `ledger_key(issue, stream_id,
topic)` folds that message into the standing comment for `(issue, topic)`, so a message about an
issue the topic is not about lands in a comment that claims the topic is about it.

**The wake tag is composed for every seat, including the Operator.**  `scripts/agent_sync/wakes.py`
builds `"[%s·wake] re=%d\n%s"`, and `scripts/agent_sync/adapters.py` sets the same shape as the
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

3.  **The pairing test decides whether the Operator speaks, and the direct message carries what
    it says.**  A message that pairs with a thread gets the Operator's own message, and that
    message travels by direct message to the seat that asked, with a link to the thread, because a
    conversation belongs in a direct message and not in a channel.  A message that pairs with no
    thread gets the Operator's question, and that goes by direct message too.  The direct message
    is the fleet's exception, used only by the Operator, and it carries that question, the exchange
    it opens, and the candidate list that tells the sender of a tagged address which sessions the
    letter could mean (`scripts/agent_sync/operator_candidates.py`).  Jay's Oct 26 ruling is what
    moved it:  the Operator may speak to anyone and converse with them, and it converses by direct
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
an Operator exemption.  That exemption is code and is not written yet, so the running code still
refuses an Operator direct message that does not carry Jay.

**The headless consultant is a different thing.**  Jay's boundary is that the Muse spark 1.3
contributor or the MiniMax M3.1 fallback is for the headless consultant, and it is not the
Operator's own voice.  So the Operator's deterministic outputs and the consultant's reasoning are
separate, and the consultant is not what the Operator listens with.  Jay's own note is that
separating incoming content from authority, and consultation from execution, are the useful
barriers here, and that a barrier may also help with prompt-injection risk, which is a rationale
and not a proof of immunity.

**The direct message consequence, and what Jay has now ruled.**  AFC-469, merged on Oct 25, 2026,
says a direct message may go out only when the owner is in the conversation, checked by user id.
The Operator's direct message does not carry Jay, because Jay's Oct 26 ruling is that the Operator
converses by direct message and that it may be addressed to anyone, so the guard has to let the
Operator through.  That is a change in the code and it is not made yet:  the AFC-469 guard gains
an Operator exemption, and until that exemption ships the running guard still refuses an Operator
direct message that does not carry Jay, and `dm_policy.py` still limits the Operator to its one
clarifying question.  The ruling is recorded here as a ruling, not as a claim about the running
code.

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

**The sheet adapter is not in this repository.**  `scripts/agent_sync/operator_sheets_export.mjs`
writes the Sessions and Events tabs and depends on an adapter surface  (`inspect()`,
`validateHeaders()`, `readRows()`, `appendSession()`, `updateSessionRow()`, `appendEvent()`)  that its
own header records as deliberately another seat's slice.  So the new column is a request to that
adapter's owner, not a change made here, and the sheet's own header list and validation have to
accept it before a row can carry it.

**The citations for the pieces this discussion rests on.**

*  The wake tag composer:  `scripts/agent_sync/wakes.py`, `"[%s·wake] re=%d\n%s"`.
*  The owner-in-the-direct-message guard, merged Oct 25, 2026:  `scripts/agent_sync/zulip.py`,
  `direct_recipient_sets()` and `check_dm_guard()`, and the client call site that runs the guard for
  a POST and a PATCH.  Pull request 947, AFC-469.
*  The Lark bridge:  `docs/protocols/lark-mm-bridge.md` and `scripts/lark-mm-bridge.py`, and the
  Operator's signal path through `--signal`.
*  The relay:  `scripts/agent_sync/linear_relay.py`, `find_issue_ids()` and `ledger_key()`.
*  The registry and the letters:  `scripts/agent_sync/operator_registry.py`, the `sessions` and
  `alias_assignments` tables.
*  The exporter:  `scripts/agent_sync/operator_sheets_export.mjs`.

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
    (AFC-469, pull request 947) gains an Operator exemption.  That exemption is code and is not
    written yet.
*  **A message that names an issue its topic does not gets its own Linear post.**  That is ruled.
    What is open is the granularity WISE and ECHO raised:  one comment per message, or those
    batched by issue and kept distinct from the topic's standing comment.

**Tasks, once the decisions are made.**

*  The Operator's own tag, and the post-versus-reply rule, in the two places the wake tag is
    composed.
*  The pairing test that decides whether the Operator speaks, and the direct message that carries
    what it says.
*  The AFC-469 owner-in-the-conversation guard gains an Operator exemption, so that the
    Operator's direct message does not have to carry Jay.
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
*  **The Lark path.**  Built and running.  The Operator's signal reaches MM's Lark chat today.
*  **The WhatsApp receive half.**  Owned by CODEX as the approved executor, with Jet owning the
    subscription, the binding ceremony and the acceptance receipts.  Blocked on the deployed
    Worker being behind the merged read-routing, on the poller being stopped with the binding
    absent, and on the rollout approval.
*  **The WhatsApp outbound half.**  AFC-545, the missing code.  Blocked on the approval to send
    at all, which the current approval excludes in its own words.
*  **The sheet columns.**  A request to the adapter's owner, because the exporter is written
    against an adapter surface that is deliberately another seat's slice and is not in this
    repository.
