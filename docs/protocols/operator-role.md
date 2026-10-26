# The Operator's role: the fleet's listener, its letter office and its mirror

A discussion, for WISE, ECHO and JET to weigh in on, written from Jay's rulings in
`#Work-In-Progress` on Oct 25, 2026 and checked against the code on `main` at `12521ef35`.

This document says what the Operator is, what already exists, what does not, and which parts
are a decision rather than a task.

It records what WISE and ECHO recommended, and it records the one they withdrew:  they had framed
the Operator as a conversational assistant answering seats' questions, and Jay's clarification is
that the Operator is mostly deterministic and does not converse with users.  The withdrawal is
the part that stands, so the Operator's one bounded exception, a question to a message's poster or
sender about a tag to wake, is a bounded exception and not a conversational turn.

## 1. What Jay has ruled

1.  **The Operator is the thing that listens.**  It holds the eavesdropper, it runs on the server,
    and it is the service identity the fleet talks to.

2.  **It acts under its own identity.**  It does not reply to a Zulip post directly, it does not
    impersonate the seat it is speaking to, and it never says a seat's wake tag.

3.  **It is mostly deterministic, and it does not converse with users.**  Its work is defined
    event inputs, routing and matching rules, defined outputs, and one bounded exception:  a
    question to a message's poster or sender about a tag to wake.  A direct message is the
    fleet's exception, used only by the Operator, and only when the Operator cannot pair the
    message with a specific thread to receive it.  Jay's consultant boundary is separate:  the
    Muse spark 1.3 contributor or the MiniMax M3.1 fallback is for the headless consultant, not
    for the Operator's own voice, and separating incoming content from authority, and consultation
    from execution, are the barriers that make that safe.  Jay's own note is that a barrier may
    also help with prompt-injection risk, which is a rationale and not a proof of immunity.

4.  **It issues session letters.**  A letter is given in exchange for the session id and a set of
    facts about the conversation, a brief list of what the conversation is about, where it lives,
    and the other details a reader needs to make sense of it.

5.  **It keeps the session and thread registry, and that registry lives on the Fleet Reference
    Google Sheet.**

6.  **It mirrors a Zulip conversation onto its Linear issue.**  One standing post per issue and
    topic, holding the conversation and a link back to the Zulip topic, and a separate post for an
    individual message that names an issue its topic does not.

7.  **It wakes a seat on the seat's own surface.**  MM on Lark, Jet on WhatsApp, and every other
    seat on Zulip.  The registry already records where a session lives, so the surface is a
    property of the session rather than a choice the Operator makes per message.

## 2. What already exists

| Requirement | Where it lives today | State |
| --- | --- | --- |
| The Operator as a service seat | `scripts/agent_sync/config.py`: the `OPERATOR` seat requires `bot = "Operator"`, `creds = "infisical"` and `wake = "inbox"` | built |
| The Operator's identity, read from the environment | `scripts/agent_sync/service_identity.py`, `scripts/agent_sync/dm_policy.py`: `operator_user_id(env, pinned)` | built |
| Session letters | `scripts/agent_sync/operator_registry.py`: the `sessions` and `alias_assignments` tables, one live holder per `(platform, letter)`, generation on recycle | built (AFC-412) |
| The registry on the Fleet Reference sheet | `scripts/agent_sync/operator_sheets_export.mjs`: the exporter writes the Sessions (`2033000102`) and Events (`2033000103`) tabs and never touches Reference (`2033000101`) | built (AFC-412 PR2) |
| The Zulip to Linear mirror | `scripts/agent_sync/linear_relay.py`: one comment per `(issue, topic)`, edited in place, ten minute batching, direct messages and private streams excluded | built (AFC-502, PR #899) |
| The listener's read API the eavesdropper reads | `scripts/agent_sync/listener_api.py`: five signed GET endpoints under `/listener/v1/<SEAT>/<record>` | built (AFC-330 part B) |
| The Operator waking MM on Lark | `docs/protocols/lark-mm-bridge.md`: the Operator runs the bridge with `--signal` and the text, and the bridge, the only holder of the Lark bot identity, sends it, runs the seat turn and puts the answer back in the same chat | built |
| The Fleet Reference sheet adapter | the exporter depends on an adapter surface (`inspect()`, `validateHeaders()`, `readRows()`, `appendSession()`, `updateSessionRow()`, `appendEvent()`) that is deliberately another seat's slice, so the exporter is written against it rather than owning it | built, owned elsewhere |

Two facts about the existing code shape the discussion below.

**The relay already detects an issue named in a message body.**  `linear_relay.py` calls
`find_issue_ids(self.regex, topic, content)`, so a message that names an issue its topic does not
is relayed today.  What is wrong is the keying, not the detection:  `ledger_key(issue, stream_id,
topic)` folds that message into the standing comment for `(issue, topic)`, so a message about an
issue the topic is not about lands in a comment that claims the topic is about it.

**The wake tag is composed for every seat, including the Operator.**  `scripts/agent_sync/wakes.py`
builds `"[%s·wake] re=%d\n%s"`, and `scripts/agent_sync/adapters.py` sets the same shape as the
adapter's `reply_prefix`.  There is no path today that lets a seat speak in its own right with
its own tag instead of the wake-reply tag.

**The Lark path already carries the attribution Jay wants.**  The Lark bridge records that the
sender travels with the turn, so the seat inbox and the Zulip mirror attribute the Operator's
signal to the Operator and it does not wake the seat as if the owner had asked.  That is the
behaviour the Operator needs on every surface, and on Lark it is already the behaviour.

## 2a.  The wake surfaces

A seat is not where the Operator is.  MM is on Lark, Jet is on WhatsApp, and the other seats are
on Zulip.

**MM on Lark is done.**  The Operator already has the signal path, and the signal is attributed
to the Operator rather than to the owner, which is the rule.

**Jet on WhatsApp is not.**  The bridge is a single reader and never sends:  its own module
docstring says so.  So the Operator has no way to wake Jet on WhatsApp, because there is nothing
to send with.  That is the same missing work as the reply leg in AFC-545, and it is the reason
that item matters beyond its own description:  without an outbound path there is no Operator wake
on WhatsApp at all.

**Zulip is the general case.**  The wake adapter is per platform and the registry already carries
`platform` and `verified_host` for every session, so the surface a seat is woken on is a property
of the session rather than a decision the Operator makes per message.

## 3. What does not exist, and the shape it should take

### 3.0  What WISE and ECHO recommend

Their input, recorded as theirs, in the topic "Operator role: listener, letters, relay and how it
talks" on Oct 25, 2026.

**Keep each seat's own records, and give the Operator the fleet view.**  The Operator answers a
seat's questions, and a seat keeps what is already its own.  This answers Jay's "the Operator
should have its eavesdropper on the server" without a seat losing its own ground.

**A session summary carries purpose, the owning issue, the source thread, repository and pull
request links, the current status, and a contact.**  A letter read six weeks later should still
say what the conversation was for and who to ask.

**Location is a coarse class plus a stable identifier, supplied by the session registration and
never inferred.**  Server, Mac or cloud, and then the seat or instance that holds it.

**The summary can be corrected later.**  A conversation changes what it is about, so the field
has to be writable after the letter is issued.

**An off-topic issue mention keeps its own attribution and link, and is never folded into the
topic's standing comment.**  Batching those by issue may cut the noise, and that batch is kept
distinct from the standing comment rather than merged into it.

**Withdrawn, and not carried forward.**  WISE and ECHO first recommended that the Operator answer
a seat in a new post in the same topic, and then withdrew it:  they had framed the Operator as a
conversational assistant answering seats' questions, and Jay's clarification is that the Operator
is mostly deterministic and should not converse with users.  The withdrawal is the part that
stands, so the discussion here is about defined event inputs, routing and matching rules, outputs
and bounded exception behaviour, and the Operator's one question about a tag to wake is that
bounded exception rather than a conversational turn.  The rest of their recommendations stand only
where they are consistent with that role.

**What they asked for, and what they asked about.**  They asked for the document, for the wake tag
composer, for the merged owner-in-the-direct-message guard, for the Lark bridge implementation, for
the source of the new wake-surface requirements, and for the provenance of the three decisions so
that Jay is not asked the same thing twice.  All of that is in section 3.3 and section 7.

### 3.1  The Operator as the listener with the eavesdropper

The read API exists and the hosted Worker answers `eavesdropper_status`, `eavesdropper_inbox`,
`eavesdropper_owner_queue`, `eavesdropper_wakes` and `eavesdropper_held_back` for a hosted seat.

The open question is one of grant, not of plumbing:  does a seat keep reading its own records
through the eavesdropper, or does the Operator become the single reader of the whole fleet's records?
The second is a much bigger grant, because an Operator that reads every seat's inbox can see
everything the fleet sees.  It is also the only shape that matches "the Operator is the thing that
listens".

The shape that fits:  the Operator holds the eavesdropper for the whole fleet and answers a
seat's questions about its own records, and a seat's own eavesdropper read is narrowed to what that
seat already owns.  The decision for Jay is whether a seat's own eavesdropper read survives at all.

### 3.2  How the Operator acts

The Operator is mostly deterministic.  Its behaviour is defined event inputs, routing and matching
rules, defined outputs, and one bounded exception.  It does not converse with users, and it is not
a conversational assistant that answers seats.

Three concrete changes carry that:

1.  **A message it sends is its own, not a reply.**  The Operator's own message is composed and
    sent as a new message.  A reply carries the thread's context and reads as the conversation's
    own voice, which is the impersonation Jay is ruling out.

2.  **The Operator does not wear a seat's wake tag.**  The wake tag is composed in two places,
    `wakes.py` and the adapter's `reply_prefix`, so this is two places:  the `OPERATOR` seat
    composes its own tag, and the wake-reply tag stays for a seat answering on the owner's behalf.

3.  **The pairing test decides post against direct message, and it decides only that.**  A message
    that pairs with a thread gets the Operator's own message in that thread.  A message that pairs
    with no thread gets the Operator's one question, and that direct message is the only direct
    message the fleet sends.  The question itself is bounded:  it asks the message's poster or
    sender about a tag to wake, and it is not an opening for a conversation.

**The headless consultant is a different thing.**  Jay's boundary is that the Muse spark 1.3
contributor or the MiniMax M3.1 fallback is for the headless consultant, and it is not the
Operator's own voice.  So the Operator's deterministic outputs and the consultant's reasoning are
separate, and the consultant is not what the Operator listens with.  Jay's own note is that
separating incoming content from authority, and consultation from execution, are the useful
barriers here, and that a barrier may also help with prompt-injection risk, which is a rationale
and not a proof of immunity.

**The direct message consequence, which is new and follows from today's rule.**  AFC-469 merged
today says a direct message may go out only when the owner is in the conversation, checked by
user id.  The Operator's fallback direct message goes to the message's poster or sender.  So the
Operator's fallback direct message has to carry Jay as well, or the guard merged today will refuse
it.  Jay has ruled that there is no exemption from that rule, so this is not a choice for the
Operator:  it either includes him or it does not send.  That is worth confirming explicitly, because
the alternative reading is that a question about a tag to wake goes to the owner directly.

### 3.3  The letter's fact bundle

The registry row already carries most of what Jay asks for.  Reading the `sessions` table:

| Jay asks for | Field today | Note |
| --- | --- | --- |
| The session id | `native_session_id` | present |
| Where it lives:  server, Mac, cloud | `verified_host` | present, and it already holds the coarse class:  the registry's `--location` argument is documented as "the fleet location (mac, server, cloud), never an OS hostname", and the live row reads `mac` |
| What it is about | `title`, `project` | present, but a title is not a brief list of what the conversation covers |
| The rest of the facts | `seat`, `platform`, `profile_scope`, `state`, `wake_capability`, `registered_at`, `last_seen_at`, `closed_at`, `thread_link`, `apple_note_link`, `source` | present |

So the gap is one thing, not two:  a short summary of what the conversation is about, carrying
purpose, the owning issue, the source links, the current status and a contact, and correctable
after the letter is issued.  The location is already recorded, so it needs a column for the
summary and not for itself.

**The one proposed sheet column**, on the Sessions tab beside the ones that already exist:

| Column | Carries | Written by |
| --- | --- | --- |
| About | purpose, owning issue, source thread, repository and pull request links, current status and contact, correctable after the letter is issued | the registry, and whoever owns the conversation |

The Sessions tab already carries a Host column for the location, so the tab needs one new
column and not two.

**The sheet adapter is not in this repository.**  `scripts/agent_sync/operator_sheets_export.mjs`
writes the Sessions and Events tabs and depends on an adapter surface  (`inspect()`,
`validateHeaders()`, `readRows()`, `appendSession()`, `updateSessionRow()`, `appendEvent()`)  that its
own header records as deliberately another seat's slice.  So the new column is a request to that
adapter's owner, not a change made here, and the sheet's own header list and validation have to
accept it before a row can carry it.

**The citations for the pieces this discussion rests on.**

*  The wake tag composer:  `scripts/agent_sync/wakes.py`, `"[%s·wake] re=%d\n%s"`.
*  The owner-in-the-direct-message guard, merged Oct 25, 2026:  `scripts/agent_sync/zulip.py`,
  `direct_recipient_sets()` and `check_dm_guard()`, and the client call site that runs the guard for
  a POST and a PATCH.  Pull request 947, AFC-469.
*  The Lark bridge:  `docs/protocols/lark-mm-bridge.md` and `scripts/lark-mm-bridge.py`, and the
  Operator's signal path through `--signal`.
*  The relay:  `scripts/agent_sync/linear_relay.py`, `find_issue_ids()` and `ledger_key()`.
*  The registry and the letters:  `scripts/agent_sync/operator_registry.py`, the `sessions` and
  `alias_assignments` tables.
*  The exporter:  `scripts/agent_sync/operator_sheets_export.mjs`.

### 3.4  The Linear mirror, one message at a time

The relay's key becomes two-keyed:  an issue named by the topic keeps the standing
`(issue, topic)` comment;  an issue named only by the message gets its own comment, keyed by the
message, holding that message and a link to the Zulip topic.  The existing ten minute batching and
the one-standing-comment behaviour for topic-named issues stay exactly as they are.

## 4. What is a decision and what is a task

**Jay's decisions, and nothing else can settle them.**  All three have a main shape from Jay on
Oct 25, 2026, and what is left is the residual, so that nobody asks him the same thing twice.

*  **The Operator holds the eavesdropper, on the server.**  That is ruled.  What is open is the
    second half that WISE and ECHO answered:  a seat keeps its own records, or the Operator
    becomes the single reader of everything the fleet sees.
*  **The Operator asks by direct message to the message's poster or sender, and the direct
    message is the fleet's only exception.**  That is ruled.  What is open is the audience, because
    the owner-in-the-conversation guard merged today (AFC-469, pull request 947) requires Jay in a
    direct message, so the fallback either carries him or the question goes to him instead.
*  **A message that names an issue its topic does not gets its own Linear post.**  That is ruled.
    What is open is the granularity WISE and ECHO raised:  one comment per message, or those
    batched by issue and kept distinct from the topic's standing comment.

**Tasks, once the decisions are made.**

*  The Operator's own tag, and the post-versus-reply rule, in the two places the wake tag is
    composed.
*  The pairing test that decides post against direct message.
*  The location column and the short summary column, in the registry and on the sheet.
*  The relay's second key, for the message-named issue.
*  The Operator's own voice on the server, so it authors its own threads instead of a seat
    reporting on them.
*  The outbound WhatsApp leg, which is the same work as AFC-545 and is also what gives the
    Operator a way to wake Jet on WhatsApp.

## 4a.  A proposed division of the work

Three slices, one seat each, and each is asked rather than assigned.  A seat that wants a
different slice, or both, says so.

**The Operator's egress and its own voice.**  The tag the Operator speaks under, the rule that a
message it sends is its own and never a reply inline, the pairing test that decides post against
direct message, and the eavesdropper grant question.  This is the listener and identity layer,
where the wake tag is composed and where the seats are told apart.

**The WhatsApp surface.**  What it takes for the Operator to wake Jet on WhatsApp, which is the
outbound leg and the bridge's send path, and the receive half that has to be running first.  This
slice also owns the question of what a question about a tag to wake looks like when the seat lives
on WhatsApp rather than on Zulip.

**The registry and the letter.**  The location a session lives at, the short summary of what a
conversation is about, and the new column on the Fleet Reference sheet, because the sheet adapter
is deliberately another seat's slice and the column has to be asked for, not assumed.

**The line that does not move.**  The letter rules stand:  a letter is for a tagged address, only
the Operator issues one, a direct message needs no letter, and a seat never chooses its own
letter.  The no-wake signal stands:  a leading eyes emoji means never wake anyone, per message,
on Zulip.

## 5. What this discussion does not change

The Operator is not a seat and does not get a letter.  A letter is for a tagged address and only
the Operator issues one.  A direct message needs no letter.  The no-wake signal from AFC-468 is
unchanged:  a leading eyes emoji means never wake anyone, and it is a per-message signal on
Zulip only.

## 6.  Open questions, and which are answered

1.  Does a seat need the eavesdropper for its own records once the Operator holds it for the
    fleet, or is the Operator's answer enough?  **Answered, in recommendation form, by WISE and
    ECHO:**  keep each seat's own records, and give the Operator the fleet view.  Jay's call is
    still needed on the shape, and this is the recommendation that goes with it.
2.  Where should the Operator's message live, so that a seat that asked sees the answer in the
    thread it asked in, without the Operator ever replying to the post that asked?  **Withdrawn
    by WISE and ECHO and not carried forward:**  the Operator is mostly deterministic and does not
    converse with users, so "the Operator answers in the thread" is the wrong shape to design for.
    What stands is the narrower rule:  a message it sends is its own, never a reply.
3.  What belongs in the short summary of what a conversation is about, so that a letter read six
    weeks later still says what it was for?  **Answered, in recommendation form, by WISE and
    ECHO:**  purpose, owning issue, source thread, repository and pull request links, current
    status and contact, and correctable later.
4.  Is one Linear comment per message the right granularity when a topic is not about the issue, or
    does that flood the issue?  **Answered, in recommendation form, by WISE and ECHO:**  keep each
    message's own attribution and link, never fold it into the topic's standing comment, and
    batching those by issue is a separate thing that may cut the noise.

**Still open, and only Jay can settle them.**

*  Whether the seat keeps its own eavesdropper read, or the Operator becomes the single reader.
*  Whether the Operator's fallback direct message carries Jay, or the question goes to him.
*  Whether the off-topic issue messages are batched by issue or one comment each.
*  Whether sending on WhatsApp is approved at all, which is the same approval AFC-545 waits on.

## 7.  The rollout, and who holds each part

*  **The Operator's own voice on the server.**  The Operator seat is already a service seat with
    its credentials from Infisical and its identity read from the environment.  What is missing is
    the Operator publishing its own threads rather than a seat reporting on them.
*  **The Lark path.**  Built and running.  The Operator's signal reaches MM's Lark chat today.
*  **The WhatsApp receive half.**  Owned by CODEX as the approved executor, with Jet owning the
    subscription, the binding ceremony and the acceptance receipts.  Blocked on the deployed
    Worker being behind the merged read-routing, on the poller being stopped with the binding
    absent, and on the rollout approval.
*  **The WhatsApp outbound half.**  AFC-545, the missing code.  Blocked on the approval to send
    at all, which the current approval excludes in its own words.
*  **The sheet columns.**  A request to the adapter's owner, because the exporter is written
    against an adapter surface that is deliberately another seat's slice and is not in this
    repository.
