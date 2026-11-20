"""The wake router every platform shares (OPS-1048;  owner design, Nov 10, 2026;  owner ruling,
Nov 11, 2026:  "the router is supposed to be for every platform").

The chain.  The listener captures a tagged message to the seat inbox and POSTs one signed
simple-tools-wake/1 body to `/wake/<SEAT>` on the one router service (`wake_service`:  one
process, one loopback port, every platform).  That seat's `Router` picks the chat
deterministically, then delivers and wakes it.  A platform supplies only how to wake one of its
chats:  a `Platform` that names its seat, its mention and its `deliver` function, registered by
one line in `wake_service.BUILDERS`.

One process, a router per seat.  Each seat served has its own `Router`, with its own state
folder, pending queue, workers, delivery cap and receipts, so a fault, an outage or a busy chat
in one seat cannot reach another seat's wakes.  Each Router asks its own `KeySource` for the
bearer;  the service gives every seat the one key WAKE_ROUTER_KEY.  `Seats` holds them and routes
by path:

    POST /wake/<SEAT>             a wake for that seat, checked against that seat's KeySource
    GET  /receipt/<SEAT>/<id>     that seat's record of a wake, with the same bearer
    GET  /receipt/<id>            the CLAUDE seat's record, the form from before
    GET  /health                  every seat, no bearer:  200 only when every installed seat is
                                  healthy
    GET  /health/<SEAT>           one seat

A seat that is registered and not served answers 503 with a reason token, and `/health` names
it.  One whose module is not installed yet shows `not-installed` and leaves the overall health
alone;  one whose module is there but broken (its builder missing or failing) counts against it.
A seat nobody registered is 404.  A served seat is unhealthy while it has no routine key or no
registry address.  The body's `seat` must equal the path's, so a wake for one seat can never
land in another's queue.

The pick.  An addressed letter;  then a fragment of a session id, at least 8 hex characters, that
identifies exactly one live chat;  then a Linear issue id from the topic or the excerpt, found in a
chat's registry `issues` (newest `seen_at` first) or, for a record with no `issues`, in its title;
then the topic's words in a chat's title;  then recency.  A tie within a step goes to the most
recent of the tied chats.  An optional decider (`Consult`, off by default) may choose before
recency.  Nobody is asked to clarify and an ambiguous wake is never held:  only `no-live-session`
and an unreadable registry do not deliver.  `no-live-session` is a bounded retry, because a chat
may register before `max_attempts` runs out;  a registry that could not be read, on a host that is
configured to reach it, is not counted against the wake's attempts at all, because the registry may
come back long after `max_attempts` would have been spent, and it is bounded by `max_age_seconds`
alone.  An answer that is not an answer is malformed data rather than an outage, so it keeps the
bounded attempts, and so does a host with no registry address or key, which is a deployment fault
and not an outage.  The receiving chat
is always told how it was linked (`Wake.link_note`), and for recency that the router is not sure the
wake is meant for it.

The letter.  A chat is written as its platform's name and a letter of exactly two capitals,
"Claude AA".  `letter_of` reads which one a message addresses, the same way for every platform:
the body's `letter`;  else a real mention followed by the letter (`@**Claude** AA`);  else the
recipient list of the post's leading header (`[GROK: BB.2.M → CLAUDE AA, CODEX BB]`), the fleet's
own way to address a chat;  else `@Claude AA` anywhere;  else `Claude AA` at the start of a line or
of the body, after the header and its mentions;  else `Claude AA,` or `Claude AA:` later in a line,
when the text names no other chat of the seat.  The three plain forms do not read code, an inline
span or a fenced block, which shows how a chat is written and addresses nobody.  A letter no live
chat holds falls through the pick as no letter does, but the wake still carries it, and a wake that
carries a letter is not picked again when its chat is released before delivery (`_released`).

Another seat's rows.  A chat registered on this platform under another seat's tag is no target
here (AFC-772:  three Claude chats were registered as seat ZC and their letters went unanswered).
Who is picked does not change;  the router only says what it left out, in two places.  When the
addressed letter is open on such a row, the receipt names that seat in `letter_other_seat`, and
stderr gets the code `letter-other-seat:<SEAT>`.  Each read of the platform's history counts the
open chats it left out for that reason alone (`other_seat`), and `/health/<SEAT>` gives the latest
count as `other_seat_rows`.  Health needs no key, so it does not say when that read happened:  that
would show when a seat was last woken.  The count is absent until a history has been read in this
process, health asks the registry nothing, and the count never changes `healthy`.  A row with no
seat or a malformed one is not counted:  it names nobody.

A reply link.  The listener may add `"linked_by": "reply"` when the message answers one a chat
posted, with the letter of that chat.  Any other value is ignored.  It changes no step of the pick:
when that letter picks the chat, the receipt says `picked: "reply"` and `reason: "reply"`, and the
chat is told it was linked by reply.

A session id may carry a `sess_` prefix, in the registry or in the text.  It is not part of the
fragment:  a fragment is compared with the id's hex alone.

The sender is never the target.  A fleet post header carries the sender's own fragment, as in
`[SEAT·4984a346: AA.4.S → OTHER]`.  So a leading header is not searched for a fragment, except the
text after its arrow, which is an addressee, and no step picks a chat whose session id starts with
the fragment in that header.

What is searched.  The listener wraps the message in one marker pair, `BEGIN_UNTRUSTED_… nonce=N`
first and `END_UNTRUSTED_… nonce=N` last (adapters.routine_excerpt), and that frame is unwrapped:
it counts only when its END is the last line.  Any other marked block, nested in the frame or not, is
dropped whole, from its BEGIN line to the END line with the same kind and nonce, or to the end of
the text when it never closes:  nothing inside one is a header, a letter, a fragment or an issue.
Quoted text addresses nobody:  a ```` ```quote ```` (or `~~~quote`) block and a line starting with
`>` are left out of the letter and fragment search, though an issue id there still counts.

A platform whose delivery is just one command needs no `deliver` of its own:  `command_platform`
builds the whole `Platform` from a short spec.  Otherwise, a `deliver` is one plain function:

    def deliver(target: Target, wake: Wake) -> Delivery

`target` is the picked chat (`session_id`, `letter`, `host`, `title`, `thread_link`, `generation`).
`wake` is metadata only (`wake_id`, `message_id`, `channel`, `topic`, `zulip_link`, `sender`, `dm`,
`letter`, `picked`, `reason`, `link_note`), and never holds the message body:  a platform's prompt
names the thread, says to read it and includes `link_note` as it stands.  It runs on a worker
thread, may take as long as it needs, and is never called twice at once for the same `session_id`.
It returns a `Delivery` whose `outcome` is one of:

    sent         handed over, with no proof it was read;  set `ack` to "confirmed" if you have proof
    completed    the chat ran the wake to the end (a headless resume that exited 0)
    busy         the chat cannot take it yet;  the wake waits, and this uses up no attempt
    unreachable  the chat's host did not answer;  the wake waits, and this uses up no attempt
    failed       this try failed and cannot have run the wake;  it is retried up to `max_attempts`
    refused      it can never work, or a retry could run the turn twice;  no retry
    unknown      it may have run the wake and nobody can tell;  it is not retried (a retry could
                 run the turn twice) and not recorded as delivered;  the receipt keeps it visible

Any exception counts as `failed`, so a `deliver` that may already have started the turn must catch
its own errors from that point on and return `refused`.  `route`, `code` and `note` are short fixed
codes for the receipt, never message text and never a credential.  A receipt says `delivered: false`
for a refusal only when its code is in `NEVER_RAN` or in the seat's own module's `NEVER_RAN` (the
Router's `never_ran`):  any other refusal may follow a turn that started, so it says
`delivered: null`, because nobody knows.

The answer and the durability.  The listener waits at most its routine timeout, resends with the
same id, and treats any 2xx as accepted, so it never retries after one.  So the POST only checks
and reserves:  the accepted wake is written to `pending/<wake_id>.json` and fsynced BEFORE the 202
goes out, a worker does the pick and the delivery, a restart replays whatever is still pending, and
the entry is dropped only once the final receipt is written.  An id with a final receipt answers
that record;  an id still pending answers 202 again and starts nothing.

A wake that may already have run a turn is never run again:  a duplicate turn is worse than a final,
visible failure, because the sender can see the receipt and send again.  So an in-flight mark is
made durable before `deliver` is called (no mark, no delivery) and cleared when the outcome is
recorded.  A replay that finds the mark writes a `dead` receipt with the code
`interrupted-during-delivery`, whose `delivered` is null because nobody knows, and does not deliver.
A wake the router never started is replayed and delivered, and its receipt says `replayed`.  SIGTERM
stops new work and waits up to `shutdown_wait_seconds` for running deliveries, so a normal restart
interrupts none of them.

A pick can go stale while its wake waits, so the registry is asked again immediately before the
delivery, ahead of the in-flight mark.  If the letter no longer names that session at the generation
picked, nothing is delivered:  a wake addressed to a letter ends in a `dead` receipt with the code
`target-released`, because the letter's new holder is not who the sender meant, and any other wake
goes back through the pick.  A registry that cannot be read is a bounded retry, never a blind
delivery, and so is a live row that does not name a well-formed chat of this seat:  a row with no
seat, or a malformed one, is no target and no proof of release either.  Receipts name the selected
generation as `picked_generation`.

Settings not yet confirmed.  A router whose settings have not come from a successful read in this
process does not know whether delivery is on, and "off" would close every pending wake as `held`.
So until the config says `confirmed`, workers leave wakes pending, use up no attempt and look again;
`max_age_seconds` still bounds the wait, and only a plain dict of settings is confirmed from the
start.

One chat's wakes run in the order they came.  A later wake waits behind an earlier one for the same
chat until that one is settled;  it is not failed as busy.  A wake that still cannot be delivered,
or is older than `max_age_seconds` when its turn comes, ends in a `dead` receipt, visible in the
seat's state directory and at GET /receipt/<SEAT>/<id>, and its message is in the seat inbox
regardless.

Turns (AFC-831).  One idle turn of a chat may carry several of its pending wakes:  a head and up
to seven riders, so a chat that was busy for an hour is not woken twenty times when it goes idle.
It is an opt-in.  A platform takes part only by declaring `Platform.deliver_turn`,

    def deliver_turn(target: Target, head: Wake, riders: tuple[Wake, ...], turn_id: str) -> Turn

with the same rules as `deliver` (an exception is `failed`), and only while the setting
`turn_max_wakes` (WAKE_ROUTER_TURN_MAX_WAKES;  unset means off) is 2 or more;  the core
never goes past TURN_MAX_WAKES.  Every other seat, and every wake that cannot ride, takes the path
above, unchanged.  Only exact addresses ride (`certain`:  a letter, a reply or a session id) and
only to the same session, letter, generation and host as the head, so a pick the router is not
sure of is delivered alone, as before, and a turn never makes it certain.  The pick is the
router's own, made on the addressing without the sender's own quoted tags, so a sender quoting
itself never makes a wake ride.  A run is contiguous:  the walk from the head stops at the first
wake of that chat that cannot ride, so one chat's wakes still run in the order they came.  A head
whose chat has answered it `busy` goes alone from then on (`busy_seen`, kept in its pending
file):  riders are not marked for every try at a chat that is in a live turn, and the wakes
behind that head ride the next turn.  A registry outage is answered as it is for a wake alone:
the check before delivery runs before any rider is marked, and an outage there or in the turn's
answer sends the head back with no attempt spent and no `busy_seen`, and every rider back with
nothing used (`_settle_turn`).  Membership is fixed before any
effect:  each rider's pending file is marked first (an attempt, the in-flight mark and `ride`),
then the head's file takes the manifest (`turn`) with its in-flight mark as the last write before
`deliver_turn`.  A crash after that commit closes every member the turn may have carried through
the replay rule above, as `unknown` with the code `interrupted-during-delivery` (a wake outside a
turn keeps `dead`), and never runs one twice.  A rider whose head holds no committed manifest of
its turn, or whose settled head says the turn did not carry it, ran nothing:  the replay takes its
marks back with nothing used.  A rider counts as carried only when the far side names it, in
order, in a readable echo of the offer (`Turn.carried`):  no echo means only the head was
carried, and an echo that cannot be read after a turn ran closes the riders `unknown` with
`turn-unconfirmed`.  A rider not carried goes back to pending with nothing used, and leads or
rides a later turn.  Each member keeps its own receipt id, done file and ledger row, and its
receipt adds a `turn` object:  its id and role, and for the head the members, their message ids
and what was carried (`[]` when nothing was, null when the answer could not say).  The prompt is
one fixed core text (`turn_text`):  ids and the router's own notes only, at most
TURN_MAX_PROMPT_CHARS long.  A platform gives a turn its one-wake time limit once per member, so a
turn can outlast `shutdown_wait_seconds`:  a restart during one closes its members as above and
never runs one twice.

A delivery can run for many minutes, so it does not keep a place in the worker pool:  a worker that
starts one hands its place to a fresh thread and ends once that wake is settled, and a chat that
hangs cannot starve another chat.  `max_deliveries` caps how many run at once, because each may be a
whole headless turn;  a wake over the cap waits and uses up no attempt.

The test flag.  A body with `"test": true` is answered at once with the whole pick and its note (the
fields in `test_answer`), writes a receipt marked as a test and delivers nothing.  It never calls
the decider and never reserves its id.

The answers carry no `ok` field on purpose:  the listener reads a body with a boolean `ok` as the
hosted Worker's settled outcome (adapters.routine_answer), which this is not.

No message body is stored, logged or forwarded.  The excerpt is read for an addressed letter, issue
ids and session-id fragments, and then dropped:  only those validated tokens are kept.
Zulip-authored names pass the secret scanner before they are kept.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import errno
import hmac
import json
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from re import Pattern
from typing import Any

from . import registry_api, secretscan

CONTRACT = "simple-tools-wake/1"
MAX_BODY = 64 * 1024
WAKE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
STOP_WORDS = frozenset(["the", "and", "for", "with", "from", "this", "that", "ops", "afc"])
NAME_CHARS = 200
# A Linear issue id as the fleet writes it.  Its own pattern, because STOP_WORDS drops `ops` and
# `afc` and the id must not depend on the topic's words.  The edges keep `P-BF-6` from reading as
# `BF-6`.
ISSUE_ID = r"[A-Z][A-Z0-9]{1,9}-[0-9]{1,7}"
ISSUE_RE = re.compile(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % ISSUE_ID)
MAX_WAKE_ISSUES = 8
MAX_ROW_ISSUES = 8
# A piece of a session id:  lower-case hex, hyphens allowed where the id has them, after an optional
# `sess_` prefix that is not kept.
SESSION_PREFIX = "sess_"
FRAGMENT_RE = re.compile(
    r"(?<![0-9A-Za-z_-])(?:%s)?([0-9a-f]+(?:-[0-9a-f]+)*)(?![0-9A-Za-z_-])" % SESSION_PREFIX
)
# A fleet post header's inside:  a seat tag, then its own fragment or one of the marks below.
HEADER_RE = re.compile(r"[A-Z][A-Z0-9_-]*(·(?:%s)?[0-9a-f-]{8,})?(.*)" % SESSION_PREFIX, re.DOTALL)
HEADER_SESSION_RE = re.compile(r":\s*[A-Z]{1,2}[.-][0-9]+[.-][A-Z]\b")
# A chat written plainly, as the fleet writes one:  the name (filled in where it is used), a gap,
# and the letter, which is exactly two capitals.  `Claude cc`, `Claude C`, `Claude CCC`,
# `Claude Code` and `CLAUDE-CC` are not one.
PLAIN_LETTER = r"%s[ \t]+([A-Z]{2})\b"
# What a plain name may not follow:  a word character, or the `@`, `*`, `/` or `.` of an address, a
# mention's stars or a path.  So `jay@Claude CC` and the silent `@_**Claude** CC` name nobody.
NOT_A_TAIL = r"(?<![\w@*/.])"
# Code shows how a chat is written and addresses nobody:  the opening line of a fenced block, and an
# inline span on one line (`blank_code_spans` pairs the backtick runs;  a pattern for it
# backtracks without bound on a long run of backticks).
CODE_FENCE_RE = re.compile(r"[ \t]*(`{3,}|~{3,})[^`\n]*")
BACKTICK_RUN_RE = re.compile(r"`+")
# The mentions a post opens with, real or silent, as the posting tools write them after the header.
LEADING_MENTIONS_RE = re.compile(r"\A(?:\s*@_?\*\*[^*\n]+\*\*[,;]?)+")
# An untrusted-text marker line:  BEGIN or END, then the kind (`_ZULIP`, or nothing).
MARKER_LINE_RE = re.compile(r"(BEGIN|END)_UNTRUSTED([A-Z0-9_]*)\b")
# The opening fence of a Zulip quote block.
QUOTE_FENCE_RE = re.compile(r"[ \t]*(`{3,}|~{3,})[ \t]*quote[ \t]*")
FRAGMENT_MIN_HEX = 8
FRAGMENT_MAX_CHARS = 36
# A longer unbroken run of hex is a commit or a key, not a stated session id, and is not kept.
FRAGMENT_MAX_PLAIN = 12
MAX_FRAGMENTS = 8
# The registry's times for a row, best first.  letter_history returns only `assigned_at` today.
RECENCY_FIELDS = ("last_seen_at", "assigned_at")
HISTORY_LIMIT = 200
# Short enough that two reads fit inside the listener's default 15-second wait on a test wake.
TEST_REGISTRY_TIMEOUT = 6
REGISTRY_TIMEOUT = 15
DONE_KEEP_SECONDS = 30 * 86400
# Longer than the listener's routine timeout, so only a client that has stalled is cut off.
REQUEST_TIMEOUT = 30
# What `Seats.stop` allows past a seat's own shutdown wait before it stops waiting for that seat.
STOP_MARGIN_SECONDS = 5.0
OUTCOMES = ("sent", "completed", "busy", "unreachable", "failed", "refused", "unknown")
INTERRUPTED = "interrupted-during-delivery"
EXPIRED = "wake-expired"
RELEASED = "target-released"
# A live registry row the check before delivery cannot read as a chat of this seat.
UNREADABLE_ROW = "registry-row-unreadable"
# The registry has no address or no key on this host:  a deployment fault, and not an outage.
# It is answered `failed`, so the retry is counted and the wake ends with the code that says so,
# and the note names the variable that is missing.
NOT_CONFIGURED = "registry-not-configured"
# The registry could not be read at all:  an outage, and not a deployment fault.  Every platform
# answers it the same way, and the router retries it without spending one of the wake's five
# attempts, so an outage cannot kill a wake the registry would have carried.
REGISTRY_UNAVAILABLE = "registry-unavailable"
# The registry answered with something that is not an answer:  malformed external data, and not
# an outage.  It is answered `failed` as well, so the wake ends after the bounded attempts
# instead of waiting on a registry that is broken rather than down.
UNREADABLE_ANSWER = "registry-answer-unreadable"
# A registered seat whose platform module is not on this host yet.
NOT_INSTALLED = "not-installed"
#: The name the note carries when the shared loader file itself could not be read:  the loader
#: file is the one source a loader read can fail on, and it carries the same name on every host.
#: It is a token the transport keeps.
REGISTRY_LOADER_SOURCE = "operator-registry-env"
#: The name the note carries when the registry's own configuration cannot be read and the error
#: names no setting:  the source this host reads its configuration from, which is the shared
#: loader file on the Mac and the router's own environment on the server.  It is a token the
#: transport keeps, so it carries no space.
REGISTRY_HOST_SOURCE = "operator-registry-env" if sys.platform == "darwin" else "router-environment"
#: Whether this host reads its registry configuration from the shared loader file.  The Mac's
#: `wake-deliver` keeps that file, and the server's router reads the knobs it fetched, so the
#: loader probe is asked only where the loader file is this host's source.  It is a module
#: constant, so a test can ask the probe on any host.
REGISTRY_LOADER_HOST = sys.platform == "darwin"
#: How much of a registry answer's own words a delivery note carries:  a variable name and a
#: reason, and never a value.  A note crosses to another host, whose readers keep only 80
#: characters of it, so the source shapes it to that length rather than the reader cutting it.
NOTE_CHARS = 80
# A platform module with no handler of the name its table entry gives:  nothing ran.  It is one
# code for every platform, not one per platform, so it sits in NEVER_RAN here.
HANDLER_MISSING = "platform-handler-missing"
# A seat tag as the fleet writes one (identity._SEAT_RE).  A registry row must name its seat:  a
# missing one is never read as this platform's.
SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")
# The paths the one service answers.  A seat in a path is a seat tag and a wake id is what
# WAKE_ID_RE allows, so no path can name a folder.  `/receipt/<id>` with no seat is CLAUDE's, the
# form from before the service served every platform.
WAKE_PATH_RE = re.compile(r"/wake/(%s)" % SEAT_RE.pattern)
RECEIPT_PATH_RE = re.compile(
    r"/receipt/(?:(%s)/)?([A-Za-z0-9][A-Za-z0-9._-]{0,127})" % SEAT_RE.pattern
)
HEALTH_PATH_RE = re.compile(r"/health/(%s)" % SEAT_RE.pattern)
LEGACY_RECEIPT_SEAT = "CLAUDE"
UNCONFIRMED = "settings-unconfirmed"
# The `refused` codes that can only come before anything ran.  It is a list of what is known safe,
# so a new code reads as unknown until someone adds it here.
NEVER_RAN = frozenset(
    {
        RELEASED,
        "bad-request",
        "bad-session-id",
        "command-option-shaped-value",
        "no-ssh-destination",
        "no-transcript",
        "platform-handler-missing",
        "platform-not-installed",
        "session-cwd-gone",
        "unknown-host",
        "unknown-platform",
    }
)
# What the receiving chat is told, by pick reason.  Fixed sentences on purpose:  the only thing that
# can vary is {issue}, and that has matched ISSUE_ID, so no Zulip text reaches a chat's prompt.
UNSURE = "and the router is not sure this wake is meant for this chat."
LINK_NOTES = {
    "letter": "The router linked this wake to this chat because the sender addressed its letter.",
    "reply": "This wake was linked by reply.",
    "session-id": "This wake was linked by session id.",
    "linear-issue": "This wake was linked by Linear issue {issue}.",
    "linear-issue-tie": "This wake was linked by Linear issue {issue}, "
    "as the most recent of several chats on it.",
    "topic-match": "The router linked this wake to this chat because its title matches the topic.",
    "topic-tie": "The router linked this wake to this chat because its title matches the topic, "
    "as the most recent of several equal matches.",
    "consultant-pick": "The router's decider chose this chat for this wake, " + UNSURE,
    "sole-live-chat": "The router could not link this wake to any chat and this is the only "
    "live one, " + UNSURE,
    "most-recent": "The router could not link this wake to any chat and this is the most "
    "recent live one, " + UNSURE,
}

# What a `command_platform` argv may name.  Each value is the router's own or has been validated;
# nothing Zulip-authored (topic, sender, channel, link) has a field, so none can reach an argv.
COMMAND_FIELDS = ("session_id", "wake_id", "message_id", "link_note", "wake_text")
COMMAND_FIELD_RE = re.compile(r"\{([A-Za-z_]+)\}")
COMMAND_TIMEOUT = 900
COMMAND_TEXT = (
    "You were tagged in Zulip (wake id {wake_id}).  {note}Read the message in Zulip before you "
    "act:  zulip_get_message with message_id {message_id}.  Answer in that thread under your "
    "seat's rules."
)
# Turns (module docstring, Turns):  one idle turn of a chat carrying several of its wakes.
TURN_CONTRACT = "simple-tools-turn/1"
TURN_MAX_WAKES = 8  # head + at most 7 riders;  the knob can only lower it
TURN_MAX_PROMPT_CHARS = 3500  # turn_text() with a prefix of up to TURN_PREFIX_MAX
TURN_PREFIX_MAX = 40
TURN_ID_RE = re.compile(r"^t-[0-9a-f]{16}$")
MESSAGE_ID_MAX = 2**63 - 1
# The picks that are exact addresses:  the sender or the listener named the chat.  Nothing the
# router inferred (an issue, a topic, a tie, a decider, recency) rides.
CERTAIN_REASONS = frozenset({"letter", "reply", "session-id"})
# A turn ran and its answer cannot say which riders it carried.
TURN_UNCONFIRMED = "turn-unconfirmed"
# An answer that names riders as carried by a turn that ran nothing.
TURN_CONFLICT = "turn-answer-conflict"
# A `deliver_turn` that answered something that is not a Turn.
TURN_BAD_ANSWER = "deliver-bad-turn"
# What an offer says of each rider:  the router's own values, nothing Zulip-authored.
TURN_MEMBER_FIELDS = ("wake_id", "message_id", "picked", "reason", "link_note")
TURN_TEXT = (
    "You were tagged in Zulip {count} times (turn {turn_id}).  The message text is not quoted "
    "here on purpose.  Read each message in Zulip before you act on it, one at a time:  "
    "zulip_get_message with its message_id, at the default length first and with full: true "
    "only when you need the rest.  Answer each in its own thread under your seat's rules."
)
TURN_LINE = "- wake id {wake_id}:  message_id {message_id}.  {note}"

# The settings a router reads and what each one is if nothing sets it.  Their values come from
# Infisical through wake_settings;  fleet_knobs/registry.py holds the same defaults, and a test keeps
# the two equal.
ROUTER_DEFAULTS: dict[str, Any] = {
    # Off until someone arms it:  with this false the router picks and receipts, and delivers nothing.
    "delivery_enabled": False,
    "workers": 4,
    "max_deliveries": 16,
    "max_attempts": 5,
    "retry_backoff": [30, 120, 300, 900],
    "busy_retry_seconds": 30,
    "unreachable_retry_seconds": 300,
    "max_age_seconds": 86400,
    # What a SIGTERM waits for running deliveries.  It covers the longest delivery a default config
    # can start, and the unit's TimeoutStopSec is longer than this.  A delivery still running at the
    # end of it is closed by the replay as interrupted, never run again.
    "shutdown_wait_seconds": 1000,
    # Most wakes one idle turn may carry for one chat;  unset (None) is off, and only a platform
    # with deliver_turn uses more (module docstring, Turns).  `_turn_cap` treats anything that is
    # not an int as off, so an unset knob and an unreadable one both mean single wakes.
    "turn_max_wakes": None,
}


@dataclass(frozen=True)
class Target:
    """One chat a wake can go to."""

    session_id: str
    letter: str = ""
    host: str = ""
    title: str = ""
    thread_link: str = ""
    generation: int = 0  # the registry's generation of `letter`;  0 only when built outside a pick


@dataclass(frozen=True)
class Wake:
    """A wake's metadata.  It has no field for the message body, by design."""

    wake_id: str
    seat: str = ""
    message_id: int = 0
    channel: str = ""
    topic: str = ""
    zulip_link: str = ""
    sender: str = ""
    dm: bool = False
    letter: str = ""
    # The router fills these three just before `deliver`;  they are empty until then.
    picked: str = ""
    reason: str = ""
    link_note: str = ""
    # Tokens read from the excerpt and validated.  The excerpt itself is never kept.
    issue_ids: tuple[str, ...] = ()
    session_fragments: tuple[str, ...] = ()
    sender_fragment: str = ""
    linked_by: str = ""  # "reply" when the listener says the letter came from a reply, else ""


@dataclass(frozen=True)
class Delivery:
    """What a platform's `deliver` did.  See the module docstring for the outcomes."""

    outcome: str
    route: str = ""
    ack: str = "unknown"
    code: str = ""
    note: str = ""


@dataclass(frozen=True)
class Turn:
    """What a platform's deliver_turn did.  `delivery` is the head's, exactly as deliver returns
    one.  `carried` lists the rider wake ids the far side put in the turn's prompt, in order;
    None means no echo, so only the head was carried.  `confirmed` lists the riders the chat
    itself recorded, a subset of `carried`.  `unreadable` means a turn was offered and the answer
    cannot say what it carried."""

    delivery: Delivery
    carried: tuple[str, ...] | None = None
    confirmed: tuple[str, ...] = ()
    unreadable: bool = False


@dataclass(frozen=True)
class Platform:
    """What one platform supplies to the shared router."""

    seat: str
    platform: str
    mention: str
    target_re: Pattern[str]
    deliver: Callable[[Target, Wake], Delivery]
    # The session ids among the candidates that must never be picked (a sub-agent, a headless run).
    exclude: Callable[[list[Target]], set[str]] | None = None
    stop_words: frozenset[str] = frozenset()
    # The opt-in to turns (module docstring, Turns), last so a positional Platform still builds:
    # deliver_turn(target, head, riders, turn_id) -> Turn.  None, the default, is today's path.
    deliver_turn: Callable[[Target, Wake, tuple[Wake, ...], str], Turn] | None = None


#: The seam for an optional decider, off by default.  It receives the candidates and the wake's
#: metadata and returns {"session_id": id} or None.  A missing decider, a None, an exception or an
#: id that is not a candidate all fall through to recency.
Consult = Callable[[list[Target], Wake], Mapping[str, Any] | None]


@dataclass(frozen=True)
class Pick:
    target: Target | None = None
    picked: str = ""  # letter|reply|session-id|linear-issue|topic-match|consultant|most-recent|""
    reason: str = ""
    letter_live: bool = False
    retry: bool = False
    candidates: tuple[str, ...] = ()
    link_note: str = ""
    # The seat tag on the addressed letter's open row when it is another seat's, else "".
    letter_other_seat: str = ""
    # What the registry said, as a transport-safe token:  the variable that is missing.
    note: str = ""


def clean(value: object, limit: int = NAME_CHARS) -> str:
    """Zulip-authored text as it may be kept:  one line, bounded, and withheld if secret-shaped."""
    text = " ".join(str(value or "").split())[:limit]
    return secretscan.WITHHELD if secretscan.scan(text) else text


#: The bounds an untrusted JSON value is held to:  how deep it may nest and how many nodes it
#: may carry.  A byte cap alone does not bound the work the parser and the walker do.
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 1000


def bounded_json(
    value: object, max_depth: int = MAX_JSON_DEPTH, max_nodes: int = MAX_JSON_NODES
) -> bool:
    """Whether a parsed JSON value is within the depth and the node bounds.

    The walk is iterative, so a deeply nested value cannot exhaust the interpreter's stack, and
    a cycle is refused rather than followed.  A value that is not JSON at all (a set, an object
    from a caller's own code) is refused too, so the helper is safe for a parsed value and for
    one handed to it directly."""
    pending: list[tuple[object, int, bool]] = [(value, 1, False)]
    active: set[int] = set()
    nodes = 0
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False
        if isinstance(item, dict):
            identity = id(item)
            if identity in active or any(not isinstance(key, str) for key in item):
                return False
            if nodes + len(item) > max_nodes or (item and depth >= max_depth):
                return False
            active.add(identity)
            pending.append((item, depth, True))
            pending.extend((child, depth + 1, False) for child in item.values())
        elif isinstance(item, list):
            identity = id(item)
            if identity in active:
                return False
            if nodes + len(item) > max_nodes or (item and depth >= max_depth):
                return False
            active.add(identity)
            pending.append((item, depth, True))
            pending.extend((child, depth + 1, False) for child in item)
        elif (
            item is None
            or type(item) in (str, bool, int)
            or (type(item) is float and math.isfinite(item))
        ):
            continue
        else:
            return False
    return True


def letter_of(payload: Mapping[str, Any], mention: str) -> str:
    """The letter the sender addressed, or "".  A letter is exactly two capitals:  a main chat's
    doubled pair (AA) or a child thread's pair (DE).  An explicit field wins.  Otherwise the
    excerpt is read with its quotes left out, and the first of these forms that names a letter
    gives it, in this order and the same way for every platform:

    1. a real mention anywhere (@**Name** AA, with or without Zulip's |id, the letter on its line
       or the next, the name said again or not:  @**Name** Name AA);
    2. the recipient list of the post's leading header, the fleet's own way to address a chat
       (`[GROK: BB.2.M → CLAUDE AA, CODEX BB]`;  see `header_letters`);
    3. the plain at-form anywhere (@Name AA or @Name Name AA, the name after the @ in any ASCII
       case), unless it is the tail of a word, an address or a path (`jay@Name AA`);
    4. the plain form at the start of a line (Name AA), or of the body after the leading header
       and the mentions that follow it (`opening_lines`);
    5. the plain form later in a line with a comma or a colon after the letter (", Name AA,
       please"), only when the text names no other chat by that mention name
       (`sole_plain_letter`).

    In 2 to 4, and where the name is said again, it is the platform's mention name or its seat
    tag, except that 3 and 4 leave out a seat tag of two capitals (ZC, MC), which is itself
    letter-shaped ("ZC PR is merged" names no chat);  the mention in 1 and the name in 5 are the
    mention name alone.  3 to 5 do not read code
    (`uncoded`).  Within a form the first letter wins.  The seat's own tag is not a letter, though
    a post often opens with it under the mention ("ZC, can you").  A silent mention (@_**Name**)
    in the body addresses nobody by itself;  the name said again after one that opens the body is the plain form of 4
    ("@_**Name** Name AA")."""
    explicit = str(payload.get("letter") or "").strip()
    if re.fullmatch(r"[A-Z]{1,2}", explicit):
        return explicit
    excerpt = str(payload.get("excerpt") or "")
    text = unquoted(trusted_text(excerpt))
    seat = str(payload.get("seat") or "")
    known = [re.escape(name) for name in dict.fromkeys((mention, seat)) if name]
    if not known:
        return ""
    names = "(?:" + "|".join(known) + ")"
    again = r"(?:%s[ \t]+)?" % names  # the name said again after a mention of it
    # A seat tag of two capitals (ZC, MC) is itself letter-shaped, so in the plain forms "ZC PR is
    # merged" would read PR as a letter.  Those forms take such a tag out;  the header's recipient
    # list, where the tag is explicit, keeps it.
    plain_known = [
        re.escape(name)
        for name in dict.fromkeys((mention, "" if re.fullmatch(r"[A-Z]{2}", seat) else seat))
        if name
    ]
    plain = "(?:" + "|".join(plain_known) + ")" if plain_known else ""
    prose = uncoded(text)

    def first(letters: Iterable[str]) -> str:
        return next((letter for letter in letters if letter != seat), "")

    mentioned = r"@\*\*%s(?:\|\d+)?\*\*\s*%s([A-Z]{2})\b" % (re.escape(mention), again)
    at_form = NOT_A_TAIL + r"@(?ai:%s)[ \t]+%s([A-Z]{2})\b" % (plain, again) if plain else ""
    line_start = r"(?m)^[ \t]*" + PLAIN_LETTER % plain if plain else ""
    return (
        first(re.findall(mentioned, text))
        or first(header_letters(excerpt, names))
        or (first(re.findall(at_form, prose)) if at_form else "")
        or (
            first(re.findall(line_start, uncoded(opening_lines(excerpt, text))))
            if line_start
            else ""
        )
        # The router's own last resort, on its own line so it can go alone:  without it a plain
        # name later in a line is a reference and addresses nobody.
        or sole_plain_letter(prose, mention, seat)
    )


def header_letters(excerpt: str, names: str) -> list[str]:
    """The letters a leading post header's recipient list gives one of these names, in the list's
    order.  The list is the text after the header's arrow (`→`, or `->` as some write it), split
    on commas.  An item counts when it is exactly the name and the letter:  `Name AA` or `@Name
    AA`, the name in any ASCII case, because a header writes the seat tag (`CLAUDE AA`) and a
    person the name (`Claude AA`);  or the name as a mention, `@**Name** AA`, which the fleet's
    posting tools make silent in a bot's post (`@_**Name** AA`), so in this list alone a silent
    mention is the address.  The letter may carry its session token (`CODEX BB.1.M`).  An item
    that is the name alone names the seat and no chat.  `split_excerpt` finds the header, so the
    listener's frame, a marked block and a cut header are read here as everywhere else."""
    sender, addressing = split_excerpt(excerpt)
    if not sender:
        return []
    listed = addressing.partition("\n")[0] or sender.partition("->")[2]
    name = "(?ai:%s)" % names
    item = re.compile(
        r"[ \t\u200a]*(?:@_?\*\*%s(?:\|\d+)?\*\*|@?%s)[ \t]+([A-Z]{2})(?:\.[0-9]+\.[A-Z])?[ \t\u200a]*"
        % (name, name)
    )
    return [found.group(1) for found in map(item.fullmatch, listed.split(",")) if found]


def uncoded(text: str) -> str:
    """The text with its code left blank:  fenced blocks (to their closing fence, or to the end
    when there is none) and inline `spans`.  Code shows how a chat is written, as a post about the
    router does, and addresses nobody.  Every line keeps its place."""
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        if fence is not None:
            mark, length = fence
            if re.fullmatch(r"[ \t]*%s{%d,}[ \t]*" % (re.escape(mark), length), line):
                fence = None
            kept.append("")
            continue
        opening = CODE_FENCE_RE.fullmatch(line)
        if opening is not None:
            fence = (opening.group(1)[0], len(opening.group(1)))
            kept.append("")
            continue
        kept.append(blank_code_spans(line))
    return "\n".join(kept)


def blank_code_spans(line: str) -> str:
    """The line with its inline code spans replaced by a space each, paired as Markdown pairs
    them:  a run of backticks opens a span that the next run of the same length closes, and a run
    with no such partner is plain text.  One pass over the runs, so a long run of backticks (the
    listener keeps 2,000 characters, a direct post up to 64 KB) costs no more than its length."""
    runs = [(found.start(), found.end()) for found in BACKTICK_RUN_RE.finditer(line)]
    if len(runs) < 2:
        return line
    by_length: dict[int, list[int]] = {}
    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(index)
    pieces: list[str] = []
    kept_from = 0
    index = 0
    while index < len(runs):
        start, end = runs[index]
        same = by_length[end - start]
        at = bisect_right(same, index)
        if at == len(same):
            index += 1
            continue
        closing = same[at]
        pieces.append(line[kept_from:start] + " ")
        kept_from = runs[closing][1]
        index = closing + 1
    pieces.append(line[kept_from:])
    return "".join(pieces)


def opening_lines(excerpt: str, text: str) -> str:
    """`text` (the excerpt, unquoted) with what a post opens with taken off:  its leading header,
    when `split_excerpt` finds one, and the mentions that follow.  A fleet post puts its body on
    the header's line, so this is what makes the body's first words the start of a line, as they
    are in a post with no header."""
    if split_excerpt(excerpt)[0]:
        text = text.partition("]")[2]  # the header opens the text and holds no `]` of its own
    return LEADING_MENTIONS_RE.sub("", text, count=1)


def sole_plain_letter(text: str, mention: str, seat: str) -> str:
    """The last resort, and the router's own choice (AFC-694), made under the owner's liberal-wake
    ruling (Nov 10, 2026) and not his wording:  a plain "Name AA" later in a line addresses that
    chat when it is written as an address, with a comma or a colon after the letter ("thanks,
    Claude CC, that works"), and the text names no other chat by that mention name (a seat tag
    or a silent mention elsewhere is not counted).  Anything else is a
    reference and names nobody, so the pick falls back:  a name with no comma or colon after it
    ("ask Claude AA about it") and two different letters ("so, Claude CC, see what Claude DD
    said").  A possessive ("Claude CC's result") is a reference that does not count as a second
    chat.  Only the mention name is read, because a seat tag of two capitals looks like a letter
    in a list of seats ("cc ZC MM, AG").  It can be removed alone:  delete its line in
    `letter_of`."""
    if not mention:
        return ""
    plain = NOT_A_TAIL + PLAIN_LETTER % re.escape(mention)
    named = set(re.findall(plain + r"(?!['’]s\b)", text)) - {seat}
    addressed = set(re.findall(plain + r"(?=[,:])", text))
    return named.pop() if len(named) == 1 and named <= addressed else ""


def link_note_ok(text: object) -> bool:
    """Whether this is a sentence the router could have written."""
    for note in LINK_NOTES.values():
        pattern = re.escape(note).replace(re.escape("{issue}"), ISSUE_ID)
        if isinstance(text, str) and re.fullmatch(pattern, text):
            return True
    return False


def _is_post_header(inner: str) -> bool:
    """Whether a leading bracket is a fleet post header and not ordinary text.  It starts with a
    seat tag, then has the sender's fragment (`[SEAT·4984a346]`, with or without more after it), a
    session token (`[SEAT: AA.4.S`), a repo mark or an arrow."""
    found = HEADER_RE.fullmatch(inner.strip())
    if found is None:
        return False
    rest = found.group(2).lstrip()
    return (
        found.group(1) is not None
        or rest.startswith(("\U0001f4c1", "→"))
        or HEADER_SESSION_RE.match(rest) is not None
    )


def _marker(line: str) -> tuple[str, str, str] | None:
    """(BEGIN or END, the marker's kind, its nonce) for an untrusted-text marker line, else None."""
    found = MARKER_LINE_RE.match(line)
    if found is None:
        return None
    nonce = re.search(r"\bnonce=(\S*)", line)
    return found.group(1), found.group(2), nonce.group(1) if nonce else ""


def trusted_text(excerpt: str) -> str:
    """The excerpt without its listener frame and without any other marked block, whole.  A block
    that is opened and never closed runs to the end, so it cannot leak either."""
    lines = excerpt.split("\n")
    first, last = _marker(lines[0]), _marker(lines[-1])
    if (
        len(lines) >= 2
        and first is not None
        and last is not None
        and (first[0], last[0]) == ("BEGIN", "END")
        and first[1:] == last[1:]
    ):
        lines = lines[1:-1]
    kept: list[str] = []
    opened: tuple[str, str] | None = None
    for line in lines:
        mark = _marker(line)
        if opened is not None:
            if mark is not None and mark[0] == "END" and mark[1:] == opened:
                opened = None
            continue
        if mark is not None:
            if mark[0] == "BEGIN":
                opened = mark[1:]
            continue  # a stray END line is dropped too
        kept.append(line)
    return "\n".join(kept)


def unquoted(text: str) -> str:
    """The text without Zulip quotes:  ```` ```quote ```` and `~~~quote` blocks (to their closing
    fence, or to the end when there is none) and lines starting with `>`."""
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        if fence is not None:
            mark, length = fence
            if re.fullmatch(r"[ \t]*%s{%d,}[ \t]*" % (re.escape(mark), length), line):
                fence = None
            continue
        opening = QUOTE_FENCE_RE.fullmatch(line)
        if opening is not None:
            fence = (opening.group(1)[0], len(opening.group(1)))
            continue
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept)


def split_excerpt(excerpt: str) -> tuple[str, str]:
    """(the sender's part of a leading post header, the text that may address a chat).  Only
    trusted text is read (`trusted_text`).  Text after the header's arrow names an addressee, so it
    stays with the body;  the rest of the header is the sender's."""
    body = trusted_text(excerpt).lstrip()
    header = re.match(r"(?:(?:\U0001F440|:eyes:)\s*)?\[([^\]\n]*)\]", body)
    if header is None or not _is_post_header(header.group(1)):
        return "", body
    sender, _, addressee = header.group(1).partition("→")
    return sender, addressee + "\n" + body[header.end() :]


def fragments_in(text: str) -> tuple[str, ...]:
    """The tokens in this text shaped like part of a session id, bounded."""
    found: dict[str, None] = {}
    for token in FRAGMENT_RE.findall(text):
        hex_chars = len(token.replace("-", ""))
        if hex_chars < FRAGMENT_MIN_HEX or len(token) > FRAGMENT_MAX_CHARS:
            continue
        if ("-" not in token and hex_chars > FRAGMENT_MAX_PLAIN) or secretscan.scan(token):
            continue
        found[token] = None
        if len(found) == MAX_FRAGMENTS:
            break
    return tuple(found)


def _sender_seat(sender_part: str) -> str:
    """The seat tag a sender header opens with, or "".  Names the header's seat -- never the woken
    seat -- because only the sender's own quoted tags are stripped from the addressing, and a
    woken-seat filter would erase another chat's legitimate tag."""
    found = re.match(r"^[\U0001F440\s]*\[?\s*([A-Z][A-Z0-9_-]*)", sender_part or "")
    return found.group(1) if found else ""


def _drop_own_seat_quotes(addressing: str, seat: str) -> str:
    """`addressing` without bracket spans of the sender's own seat.  A quoted span of the sender's
    own seat is the sender writing about itself, and its interior fragment would pick the sender's
    own chat, which is never the target.  The span -- only the span -- goes, so another address on
    the same line survives;  it is matched anywhere in the line, so a mid-line self-quote is caught
    too;  and spans of other seats stay, because those are addresses.  Same-seat cross-session
    addressing goes by letter, and a wake with no sender header keeps the residual noted in the
    picking docs, bounded by the delivery loop guard."""
    if not seat:
        return addressing
    return re.sub(r"\[" + re.escape(seat) + r"[^\]\n]{0,512}\]", "", addressing)


def wake_from_payload(payload: Mapping[str, Any], mention: str) -> Wake:
    message_id = payload.get("message_id")
    excerpt = str(payload.get("excerpt") or "")
    seat = str(payload.get("seat") or "")
    sender_part, addressing = split_excerpt(excerpt)
    addressing = _drop_own_seat_quotes(addressing, _sender_seat(sender_part))
    return Wake(
        wake_id=str(payload.get("wake_id") or ""),
        seat=seat,
        message_id=message_id if type(message_id) is int else 0,
        channel=clean(payload.get("channel")),
        topic=clean(payload.get("topic")),
        zulip_link=clean(payload.get("zulip_link"), 500),
        sender=clean(payload.get("sender_full_name")),
        dm=payload.get("dm") is True,
        letter=letter_of(payload, mention),
        issue_ids=tuple(dict.fromkeys(ISSUE_RE.findall(trusted_text(excerpt))))[:MAX_WAKE_ISSUES],
        session_fragments=fragments_in(unquoted(addressing)),
        sender_fragment=next(iter(fragments_in(sender_part)), ""),
        linked_by="reply" if payload.get("linked_by") == "reply" else "",
    )


def private_dir(folder: Path) -> None:
    """Make this private state folder, refusing a symlink that would send its files elsewhere.  An
    OSError, like any other failure to write here, so every caller already handles it."""
    if folder.is_symlink():
        raise OSError(errno.ELOOP, "refusing a symlinked state folder", str(folder))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_write(path: Path, obj: Any) -> None:
    """One private file, fsynced, then renamed into place and its folder fsynced:  it survives a
    crash whole or not at all."""
    private_dir(path.parent)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_config(
    path: Path, defaults: Mapping[str, Any], *, required: bool = False
) -> dict[str, Any]:
    """The defaults overlaid with a JSON file.  It widens as freely as it narrows, so nothing that
    runs a router reads its settings this way:  wake_settings lets a local file only narrow."""
    return {**defaults, **read_local_config(path, defaults, required=required)}


def read_local_config(
    path: Path,
    defaults: Mapping[str, Any],
    *,
    required: bool = False,
    seat_lists: Sequence[str] = (),
) -> dict[str, Any]:
    """Only what a JSON file sets.  A file named explicitly must exist.  A file that is there must
    parse, name only known keys and match each default's type:  an unreadable or mistyped config is
    a refusal that names the path, never a quiet run on defaults.  A key in `seat_lists` is known
    too, and its value must be a list of seat tags."""
    config: dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise SystemExit("wake-router: the config file %s is not there" % path) from None
        return config
    except OSError as exc:
        raise SystemExit(
            "wake-router: the config file %s is unreadable (%s)" % (path, type(exc).__name__)
        ) from None
    try:
        loaded = json.loads(text)
    except ValueError:
        raise SystemExit("wake-router: the config file %s is not valid JSON" % path) from None
    if not isinstance(loaded, dict):
        raise SystemExit("wake-router: the config file %s is not a JSON object" % path)
    for key, value in loaded.items():
        if key in seat_lists:
            if not isinstance(value, list) or not all(
                isinstance(seat, str) and SEAT_RE.fullmatch(seat) for seat in value
            ):
                raise SystemExit("wake-router: %s: %s has the wrong type" % (path, key))
            config[key] = value
            continue
        if key not in defaults:
            raise SystemExit("wake-router: %s: unknown setting %s" % (path, key))
        if not _same_shape(value, defaults[key]):
            raise SystemExit("wake-router: %s: %s has the wrong type" % (path, key))
        config[key] = value
    return config


def _same_shape(value: object, default: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return type(value) is int and value > 0
    if isinstance(default, list):
        return (
            isinstance(value, list) and bool(value) and all(type(v) is int and v > 0 for v in value)
        )
    return isinstance(value, str)


# The shortest routine key the router accepts, as the old key file check enforced.
KEY_MIN_LENGTH = 16
# Where the router gets its routine key:  a callable answering (key, why), with the key empty and
# `why` a short reason token when there is none.  The router asks on every request and never
# keeps the answer.  The service passes the settings' secret knob (wake_settings.routine_key);
# a test passes a fixed key.
KeySource = Callable[[], tuple[str, str]]


def command_platform(
    seat: str,
    platform: str,
    mention: str,
    target_re: Pattern[str],
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout: int = COMMAND_TIMEOUT,
    exclude: Callable[[list[Target]], set[str]] | None = None,
    stop_words: frozenset[str] = frozenset(),
    runner: Callable[..., Any] = subprocess.run,
) -> Platform:
    """A `Platform` whose delivery is one command, run without a shell.  `argv` may name the
    `COMMAND_FIELDS` as {session_id} and so on, anywhere but its first item;  any other name in
    braces is a ValueError here, at startup.  A value that could read as an option is `refused`
    before anything runs.  Exit 0 is `sent` with `ack` unknown, because it proves only that the
    command ran.  A non-zero exit or a timeout is `refused`, because the turn may have started;  an
    OSError at start is `failed`.  The child gets `env`, or else only HOME and PATH:  the router's
    own environment holds its keys."""
    template = [argv] if isinstance(argv, str) else list(argv)
    named = {name for part in template for name in COMMAND_FIELD_RE.findall(part)}
    if len(template) < 2 or COMMAND_FIELD_RE.search(template[0]) or named - set(COMMAND_FIELDS):
        raise ValueError("command_platform: a program, its arguments and only known fields")
    child_env = (
        dict(env)
        if env is not None
        else {name: os.environ[name] for name in ("HOME", "PATH") if name in os.environ}
    )

    def deliver(target: Target, wake: Wake) -> Delivery:
        note = wake.link_note if link_note_ok(wake.link_note) else ""
        values = {
            "session_id": target.session_id,
            "wake_id": wake.wake_id,
            "message_id": str(wake.message_id),
            "link_note": note,
            "wake_text": COMMAND_TEXT.format(
                wake_id=wake.wake_id, note=note + "  " if note else "", message_id=wake.message_id
            ),
        }
        # `target_re` is the platform's own and may allow it, so this does not rely on that.
        if any(values[name].startswith("-") for name in named):
            return Delivery("refused", "command", code="command-option-shaped-value")
        # One pass per item, so a value is never read again as a field.
        command = [COMMAND_FIELD_RE.sub(lambda m: values[m.group(1)], part) for part in template]
        try:
            proc = runner(
                command,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Delivery("refused", "command", code="command-timeout")
        except OSError as exc:  # it never started:  the one failure that is safe to try again
            return Delivery("failed", "command", code="command-%s" % type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - it may have started, so it is final
            return Delivery("refused", "command", code="command-error-%s" % type(exc).__name__)
        if proc.returncode == 0:
            return Delivery("sent", "command")
        return Delivery("refused", "command", code="command-exit-%d" % proc.returncode)

    return Platform(seat, platform, mention, target_re, deliver, exclude, stop_words)


# ---- turns:  the pure helpers the router, the Mac dispatcher and an opted-in module share ------


def message_id_ok(value: object) -> bool:
    """Whether this is a Zulip message id a turn may list:  a positive int, never a bool."""
    return type(value) is int and 1 <= value <= MESSAGE_ID_MAX


def certain(pick: Pick) -> bool:
    """Whether a pick is an exact address (CERTAIN_REASONS) with the router's own note for it.
    Every pick the router is not sure of, and every one it inferred, is not."""
    return (
        pick.target is not None
        and pick.reason in CERTAIN_REASONS
        and pick.picked == pick.reason
        and pick.link_note == LINK_NOTES[pick.reason]
        and UNSURE not in pick.link_note
    )


def turn_key(target: Target) -> tuple[str, str, int, str]:
    """The exact chat a turn is for.  Title and thread link are left out on purpose:  they
    describe the chat and do not name another one."""
    return target.session_id, target.letter, target.generation, target.host


def turn_text(prefix: str, head: Wake, riders: Sequence[Wake], turn_id: str) -> str:
    """The one prompt of a turn:  the platform's fixed prefix, the fixed core text, and one line
    per wake, head first.  Only router values reach it:  ids, int message ids and the router's
    own link notes.  Topic, sender, channel and link never do.  The prefix is cut to
    TURN_PREFIX_MAX, the length the router's prompt cap allows for."""
    prefix = prefix[:TURN_PREFIX_MAX]
    members = (head, *riders)
    lines = [
        TURN_LINE.format(
            wake_id=wake.wake_id,
            message_id=wake.message_id,
            note=wake.link_note if link_note_ok(wake.link_note) else "",
        ).rstrip()
        for wake in members
    ]
    return prefix + TURN_TEXT.format(count=len(members), turn_id=turn_id) + "\n" + "\n".join(lines)


def turn_member(wake: Wake) -> dict[str, Any]:
    return {name: getattr(wake, name) for name in TURN_MEMBER_FIELDS}


def turn_offer(turn_id: str, riders: Sequence[Wake]) -> dict[str, Any]:
    """The riders a turn offers, as they go on the wire."""
    return {
        "contract": TURN_CONTRACT,
        "id": turn_id,
        "riders": [turn_member(wake) for wake in riders],
    }


def turn_riders_from(turn: object, head_id: object) -> tuple[Wake, ...]:
    """The riders an offer names, as far as they are valid:  the longest valid prefix, or () when
    the offer itself is not one.  The one validator, used by the router as a self-check, by the
    Mac dispatcher and by every module that takes turns."""
    if not isinstance(turn, dict) or turn.get("contract") != TURN_CONTRACT:
        return ()
    turn_id, records = turn.get("id"), turn.get("riders")
    if not isinstance(turn_id, str) or not TURN_ID_RE.fullmatch(turn_id):
        return ()
    if not isinstance(records, list) or not 1 <= len(records) <= TURN_MAX_WAKES - 1:
        return ()
    if not isinstance(head_id, str) or not WAKE_ID_RE.fullmatch(head_id):
        return ()
    kept: list[Wake] = []
    seen = {head_id}
    for record in records:
        if not isinstance(record, dict) or set(record) != set(TURN_MEMBER_FIELDS):
            break
        wake_id, reason = record["wake_id"], record["reason"]
        if (
            not isinstance(wake_id, str)
            or not WAKE_ID_RE.fullmatch(wake_id)
            or wake_id in seen
            or not message_id_ok(record["message_id"])
            or not isinstance(reason, str)
            or reason not in CERTAIN_REASONS
            or record["picked"] != reason
            or record["link_note"] != LINK_NOTES[reason]
        ):
            break
        seen.add(wake_id)
        kept.append(
            Wake(
                wake_id=wake_id,
                message_id=record["message_id"],
                picked=reason,
                reason=reason,
                link_note=record["link_note"],
            )
        )
    return tuple(kept)


def turn_echo(turn_id: str, carried: Iterable[str], confirmed: Iterable[str]) -> dict[str, Any]:
    """What the far side says a turn carried and what the chat recorded."""
    return {
        "contract": TURN_CONTRACT,
        "id": turn_id,
        "carried": list(carried),
        "confirmed": list(confirmed),
    }


def turn_echo_read(
    echo: object, turn_id: str, offered: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """(carried, confirmed) from an echo of this turn, or None when it cannot be read:  carried
    must be a prefix of what was offered, in order, and confirmed some of carried."""
    if not isinstance(echo, dict) or echo.get("contract") != TURN_CONTRACT:
        return None
    unreadable = echo.get("unreadable", False)
    if echo.get("id") != turn_id or (type(unreadable) is bool and unreadable):
        return None
    carried, confirmed = echo.get("carried"), echo.get("confirmed")
    if not isinstance(carried, list) or not all(isinstance(i, str) for i in carried):
        return None
    if carried != list(offered[: len(carried)]):
        return None
    if not isinstance(confirmed, list) or not all(isinstance(i, str) for i in confirmed):
        return None
    if len(set(confirmed)) != len(confirmed) or not set(confirmed) <= set(carried):
        return None
    return tuple(carried), tuple(confirmed)


def turn_result(delivery: Delivery, echo: object, turn_id: str, riders: Sequence[Wake]) -> Turn:
    """A platform's Turn from the head's delivery and the echo its answer carried, if any."""
    if echo is None:
        return Turn(delivery)
    read = turn_echo_read(echo, turn_id, [wake.wake_id for wake in riders])
    if read is None:
        return Turn(delivery, unreadable=True)
    return Turn(delivery, read[0], read[1])


def _carry_ok(carried: object, confirmed: object, offered: Sequence[str]) -> bool:
    """Whether a Turn's own carry list fits what was offered (turn_echo_read, for a Turn a module
    built itself):  a tuple of str that is a prefix of the offer, and confirmed some of it."""
    return (
        isinstance(carried, tuple)
        and all(isinstance(i, str) for i in carried)
        and list(carried) == list(offered[: len(carried)])
        and isinstance(confirmed, tuple)
        and all(isinstance(i, str) for i in confirmed)
        and len(set(confirmed)) == len(confirmed)
        and set(confirmed) <= set(carried)
    )


def _turn_record(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `turn` object a member's receipt adds, or None for a wake outside a turn."""
    turn, ride = entry.get("turn"), entry.get("ride")
    if isinstance(turn, dict):
        return {
            "id": turn["id"],
            "role": "head",
            "members": [entry["wake"]["wake_id"], *turn["riders"]],
            "message_ids": list(turn["message_ids"]),
            "carried": turn.get("carried"),
        }
    if isinstance(ride, dict):
        return {"id": ride["turn"], "role": "rider", "head": ride["head"], "index": ride["index"]}
    return None


class Router:
    """The pick, the durable queue, the workers and the receipts for one seat.  The service runs
    one per seat (`Seats`), and none shares a folder, a queue, a worker or a key with another."""

    def __init__(
        self,
        platform: Platform,
        env: Mapping[str, str],
        config: Mapping[str, Any],
        state: Path,
        key: KeySource,
        consult: Consult | None = None,
        clock: Callable[[], float] = time.time,
        never_ran: Iterable[str] = (),
    ) -> None:
        self.platform = platform
        # The refusals known to come before anything ran:  this module's, and the platform's own
        # (its module's NEVER_RAN), so the pinned set here does not grow with each platform.
        self.never_ran = NEVER_RAN | frozenset(never_ran)
        self.env = env
        self.config = config
        self.state = state
        self._key = key
        self.consult = consult
        self.clock = clock
        self._entries: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._cond = threading.Condition()
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._retiring: set[threading.Thread] = set()
        # The latest read of the platform's history:  (open chats left out only because their row
        # names another seat, when it was read).  None until one has been read in this process.
        self.other_seat_scan: tuple[int, float] | None = None

    def routine_key(self) -> tuple[str, str]:
        """(key, why) for this request:  the key, or "" and a reason token.  A source that fails
        or answers a short key gives no key, so nothing is ever let through unchecked."""
        try:
            key, why = self._key()
        except Exception as exc:  # noqa: BLE001 - no key is the answer, never a crash
            return "", "key-error-%s" % type(exc).__name__
        if not isinstance(key, str) or len(key) < KEY_MIN_LENGTH:
            return "", (why if isinstance(why, str) and why else "key-short")
        return key, ""

    # ---- the pick ---------------------------------------------------------------------------

    def _valid(self, row: Mapping[str, Any]) -> Target | None:
        return target_from_row(row, self.platform.seat, self.platform.target_re)

    def _other_seat(self, row: Mapping[str, Any]) -> str:
        return other_seat(row, self.platform.seat, self.platform.target_re)

    def other_seat_health(self) -> dict[str, Any]:
        """What this seat's health says about the latest history read:  a count, never an id, a
        title or the time of the read (health needs no key, and the time would show when the seat
        was last woken), and {} before any read.  It reads only what a pick left here."""
        scan = self.other_seat_scan
        if scan is None:
            return {}
        return {"other_seat_rows": scan[0]}

    def _excluded(self, targets: list[Target]) -> set[str]:
        return self.platform.exclude(targets) if self.platform.exclude and targets else set()

    def _words(self, text: str) -> set[str]:
        stop = STOP_WORDS | self.platform.stop_words | {self.platform.platform}
        return {w.lower() for w in WORD_RE.findall(text or "") if w.lower() not in stop}

    def pick(self, wake: Wake, *, consult: bool = True, timeout: int = REGISTRY_TIMEOUT) -> Pick:
        """The rules, in order:  an addressed letter;  a session-id fragment;  a Linear issue;  the
        chat whose title best shares the topic's words;  the optional decider;  recency.  A tie goes
        to the most recent of the tied, and no step picks the sender's own chat.  A registry that
        cannot be read asks for a retry:  it is not "no live chat".  No live chat asks for one too,
        because a chat may register in time.  A chat whose row names another seat is never picked;
        the pick only records that the addressed letter is on one (`letter_other_seat`) and how
        many the history held (`other_seat_scan`)."""
        letter_live = False
        letter_other_seat = ""

        def chosen(
            target: Target, picked: str, reason: str, among: Any = (), issue: str = ""
        ) -> Pick:
            return Pick(
                target=target,
                picked=picked,
                reason=reason,
                letter_live=letter_live,
                candidates=tuple(t.letter for t in among),
                link_note=LINK_NOTES[reason].format(issue=issue),
                letter_other_seat=letter_other_seat,
            )

        if wake.letter:
            answer = registry_api.call(
                self.env,
                "resolve",
                {"platform": self.platform.platform, "letter": wake.letter},
                timeout=timeout,
            )
            found = answer.get("found") if isinstance(answer, Mapping) else None
            if type(found) is not bool or "error" in answer:
                # Unreadable, so not a dead letter:  falling back would wake a chat not addressed.
                return Pick(
                    reason=registry_trouble(answer),
                    retry=True,
                    note=registry_note(answer.get("error")) if isinstance(answer, Mapping) else "",
                )
            live = found and not _closed(answer)
            target = self._valid(answer) if live else None
            if target is not None:
                letter_live = True
                # An excluded chat is not a place a wake can go, so the chain carries on without it.
                if target.session_id not in self._excluded([target]) and not _is_sender(
                    target, wake
                ):
                    how = "reply" if wake.linked_by == "reply" else "letter"
                    return chosen(target, how, how)
            elif live:
                # The letter is open on a chat of another seat:  no target here, and said so.
                letter_other_seat = self._other_seat(answer)
                if letter_other_seat:
                    self._log("letter-other-seat:" + letter_other_seat, wake.wake_id)
        answer = registry_api.call(
            self.env,
            "history",
            {"platform": self.platform.platform, "limit": HISTORY_LIMIT},
            timeout=timeout,
        )
        if not isinstance(answer, Mapping) or "error" in answer:
            # The letter was read before the outage, so what it said still goes on the receipt.
            return Pick(
                reason=registry_trouble(answer),
                retry=True,
                letter_other_seat=letter_other_seat,
                note=registry_note(answer.get("error")) if isinstance(answer, Mapping) else "",
            )
        live: dict[str, Target] = {}
        seen: dict[str, float | None] = {}
        row_issues: dict[str, list[tuple[str, float | None]]] = {}
        others: set[str] = set()  # open chats left out only because their row names another seat
        for row in answer.get("items") or []:
            if not isinstance(row, dict):
                continue
            if _closed(row):
                continue
            target = self._valid(row)
            if target is None:
                if self._other_seat(row):
                    others.add(str(row.get("native_session_id")))
                continue
            if target.session_id not in live:
                live[target.session_id] = target
                seen[target.session_id] = _row_time(row)
                row_issues[target.session_id] = _issues_of(row)
        # One assignment, so a health read on another thread sees a whole pair.
        self.other_seat_scan = (len(others - live.keys()), self.clock())
        excluded = self._excluded(list(live.values()))
        candidates = [
            t for sid, t in live.items() if sid not in excluded and not _is_sender(t, wake)
        ]
        if not candidates:
            return Pick(
                reason="no-live-session",
                letter_live=letter_live,
                retry=True,
                letter_other_seat=letter_other_seat,
            )

        named = _named_session(wake, live)
        target = next((t for t in candidates if t.session_id == named), None)
        if target is not None:
            return chosen(target, "session-id", "session-id")

        wanted = list(dict.fromkeys(ISSUE_RE.findall(wake.topic) + list(wake.issue_ids)))
        linked = self._on_issue(candidates, wanted, row_issues, seen)
        if linked is not None:
            target, issue, among = linked
            reason = "linear-issue-tie" if len(among) > 1 else "linear-issue"
            return chosen(target, "linear-issue", reason, among if len(among) > 1 else (), issue)

        topic_words = self._words(wake.topic)
        scored = [(len(topic_words & self._words(t.title)), t) for t in candidates]
        best = max(score for score, _ in scored)
        if best > 0:
            top = [t for score, t in scored if score == best]
            if len(top) == 1:
                return chosen(top[0], "topic-match", "topic-match")
            return chosen(_most_recent(top, seen), "topic-match", "topic-tie", top)

        if len(candidates) == 1:
            return chosen(candidates[0], "most-recent", "sole-live-chat")
        decided = self._decide(candidates, wake) if consult else None
        if decided is not None:
            return chosen(decided, "consultant", "consultant-pick", candidates)
        return chosen(_most_recent(candidates, seen), "most-recent", "most-recent", candidates)

    @staticmethod
    def _on_issue(
        candidates: list[Target],
        wanted: list[str],
        row_issues: Mapping[str, list[tuple[str, float | None]]],
        seen: Mapping[str, float | None],
    ) -> tuple[Target, str, list[Target]] | None:
        """The chat linked by one of these issue ids, the id, and all the chats that matched.  The
        registry's `issues` come first, newest `seen_at` first.  A chat's title is the fallback, and
        only for a record that has no `issues`."""
        if not wanted:
            return None
        newest: dict[str, tuple[float | None, str]] = {}
        for target in candidates:
            hits = [(when, i) for i, when in row_issues[target.session_id] if i in wanted]
            if hits:
                newest[target.session_id] = max(hits, key=lambda h: (h[0] or 0.0, h[1]))
        if newest:
            among = [t for t in candidates if t.session_id in newest]
            best = _most_recent(among, {sid: hit[0] for sid, hit in newest.items()})
            return best, newest[best.session_id][1], among
        titled = {}
        for target in candidates:
            in_title = set(ISSUE_RE.findall(target.title))
            issue = next((i for i in wanted if i in in_title), "")
            if issue and not row_issues[target.session_id]:
                titled[target.session_id] = issue
        if not titled:
            return None
        among = [t for t in candidates if t.session_id in titled]
        best = _most_recent(among, seen)
        return best, titled[best.session_id], among

    def _decide(self, candidates: list[Target], wake: Wake) -> Target | None:
        """The optional decider's choice, or None for anything but one of these candidates."""
        if self.consult is None:
            return None
        try:
            answer = self.consult(list(candidates), wake)
        except Exception as exc:  # noqa: BLE001 - a decider's failure only means recency decides
            self._log("consultant-error-%s" % type(exc).__name__, wake.wake_id)
            return None
        if not isinstance(answer, Mapping):
            return None
        return next((t for t in candidates if t.session_id == answer.get("session_id")), None)

    # ---- accepting --------------------------------------------------------------------------

    def _pending(self, wake_id: str) -> Path:
        return self.state / "pending" / (wake_id + ".json")

    def _done(self, wake_id: str) -> Path:
        return self.state / "done" / (wake_id + ".json")

    def accept(self, wake: Wake) -> tuple[int, dict[str, Any]]:
        """Reserve one wake durably and answer.  A failure to write is a 503, so the listener
        retries it;  a 202 is only ever sent for a wake that is on disk."""
        with self._cond:
            record = _read_json(self._done(wake.wake_id))
            if isinstance(record, dict):
                return 200, record
            held = self._entries.get(wake.wake_id)
            if held is not None:
                return 202, self._accepted(held)
            entry = {
                "wake": asdict(wake),
                "receipt_id": secrets.token_hex(8),
                "accepted_at": self.clock(),
                "next_at": 0.0,
                "attempts": 0,
                "failures": 0,
                "pick": None,
            }
            try:
                atomic_write(self._pending(wake.wake_id), entry)
            except OSError as exc:
                self._log("accept-write-failed-%s" % type(exc).__name__, wake.wake_id)
                return 503, {"error": "the wake could not be recorded", "wake_id": wake.wake_id}
            self._entries[wake.wake_id] = entry
            self._cond.notify_all()
            return 202, self._accepted(entry)

    @staticmethod
    def _accepted(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "wake_id": entry["wake"]["wake_id"],
            "receipt_id": entry["receipt_id"],
            "status": "accepted",
        }

    def test_answer(self, wake: Wake) -> dict[str, Any]:
        """A test wake's answer:  `test`, `wake_id`, `receipt_id`, `picked`, `picked_letter`,
        `picked_generation`, `session_id`, `reason`, `candidates`, `link_note`, `delivered`,
        `letter_other_seat`.  It delivers nothing and reserves nothing."""
        pick = self.pick(wake, consult=False, timeout=TEST_REGISTRY_TIMEOUT)
        answer = self._receipt(
            {"wake": asdict(wake), "receipt_id": secrets.token_hex(8), "attempts": 0},
            pick,
            status="test",
            delivered=False,
            result=None,
        )
        answer["test"] = True
        self._append_log(answer)
        return answer

    def receipt(self, wake_id: str) -> dict[str, Any] | None:
        """A wake's record:  the final receipt, or its accepted state while it is pending."""
        with self._cond:
            record = _read_json(self._done(wake_id))
            if isinstance(record, dict):
                return record
            entry = self._entries.get(wake_id)
            return self._accepted(entry) if entry is not None else None

    # ---- the workers ------------------------------------------------------------------------

    def start(self) -> None:
        """Replay what a restart left pending, then start the workers."""
        self.replay()
        with self._cond:
            for _ in range(int(self.config["workers"])):
                self._add_worker()

    def _add_worker(self) -> None:
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _give_up_slot(self) -> None:
        """A delivery may run for many minutes and must not cost another chat a worker, so this
        thread's place in the pool goes to a fresh one, and this thread ends once its wake is
        settled.  One delivery per chat and `max_deliveries` bound what that can start."""
        current = threading.current_thread()
        with self._cond:
            if self._stopping or current not in self._threads:
                return  # no new work is wanted, or this is `run_once` on a caller's own thread
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            try:
                self._add_worker()
            except RuntimeError:
                return  # no thread to be had:  this worker just stays busy, as it would have
            self._retiring.add(current)

    def stop(self, wait: float = 5.0) -> bool:
        """Claim nothing more and wait up to `wait` seconds in all for running deliveries.  Answers
        whether every worker finished."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        deadline = time.monotonic() + wait
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def replay(self) -> int:
        """Load every pending file.  One with a final receipt finished just before a crash, so it
        is only removed.  One that is not a whole entry stays on disk, set aside and named on
        stderr, and is never run.  One still marked in flight may have run its turn, so it is
        settled as interrupted and only its receipt is left to write, unless it is a rider whose
        turn provably did not carry it (`_ride_released`):  that one is plain again."""
        self._prune()
        folder = self.state / "pending"
        loaded = 0
        with self._cond:
            found: dict[str, dict[str, Any]] = {}
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                wake_id = path.name[: -len(".json")]
                if self._done(wake_id).exists():
                    path.unlink(missing_ok=True)
                    continue
                entry = _read_json(path)
                if not _loadable(entry, wake_id):
                    self._log("pending-unreadable", wake_id)
                    continue
                found[wake_id] = entry
            # Decided on the files as they were found, before any of them is changed below.
            released = {wake_id for wake_id, e in found.items() if self._ride_released(e, found)}
            for wake_id, entry in found.items():
                entry["wake"] = asdict(Wake(**entry["wake"]))
                entry["replayed"] = True
                entry["next_at"] = 0.0
                if wake_id in released:
                    self._log("ride-released", wake_id)
                    entry["attempts"] = max(0, entry["attempts"] - 1)
                    entry.pop("in_flight", None)
                if not entry.get("in_flight") and entry.get("receipt") is None:
                    # A turn mark with no in-flight mark ran nothing:  the wake is plain again.
                    entry.pop("turn", None)
                    entry.pop("ride", None)
                if entry.get("in_flight") and entry.get("receipt") is None:
                    self._log(INTERRUPTED, wake_id)
                    entry["receipt"] = self._interrupted(entry)
                self._entries[wake_id] = entry
                loaded += 1
            self._cond.notify_all()
        return loaded

    def _ride_released(self, entry: Mapping[str, Any], found: Mapping[str, Any]) -> bool:
        """Whether a rider still marked in flight provably was not carried by its turn, read from
        its head:  the head's pending file holds no manifest of that turn (it was never
        committed, or its try came back with nothing run and the head was retried), or the
        head's settled receipt of that turn lists what it carried and this rider is not in it.
        Anything less certain (the head's file set aside, gone with no receipt, or a turn still
        in flight) is no proof, and the rider is closed as interrupted."""
        ride = entry.get("ride")
        if not isinstance(ride, dict) or not entry.get("in_flight"):
            return False
        if entry.get("receipt") is not None:
            return False
        turn_id, head_id = ride["turn"], ride["head"]
        if not TURN_ID_RE.fullmatch(turn_id) or not WAKE_ID_RE.fullmatch(head_id):
            return False
        wake_id = entry["wake"]["wake_id"]
        head = found.get(head_id)
        if head is not None:
            turn = head.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != turn_id:
                return True  # no manifest of this turn:  it never reached `deliver_turn`
            record = head.get("receipt")
            if record is None and not head.get("in_flight"):
                return True  # a manifest with no in-flight mark ran nothing (`replay`)
        elif self._pending(head_id).exists():
            return False  # the head's file is there and could not be read
        else:
            record = _read_json(self._done(head_id))
            if not isinstance(record, dict):
                return False  # no head and no receipt:  nothing says what the turn did
        if not isinstance(record, dict):
            return False  # the head's turn is still in flight
        told = record.get("turn")
        if not isinstance(told, dict) or told.get("id") != turn_id:
            return True  # the head was settled by another try:  this turn ran nothing
        carried = told.get("carried")
        return isinstance(carried, list) and wake_id not in carried

    def _interrupted(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """The final receipt of a wake that may have run its turn:  dead, and delivered unknown.
        A member of a turn is `unknown` instead (module docstring, Turns):  the head and every
        rider alike, since none can tell what the turn carried."""
        record = entry.get("pick")
        pick = _pick_from(record) if record else Pick(reason=INTERRUPTED)
        result = Delivery("failed", code=INTERRUPTED)
        status = "unknown" if entry.get("turn") or entry.get("ride") else "dead"
        return self._receipt(entry, pick, status=status, delivered=None, result=result)

    def _prune(self) -> None:
        folder = self.state / "done"
        cutoff = self.clock() - DONE_KEEP_SECONDS
        for path in folder.glob("*.json") if folder.is_dir() else []:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _claim(self) -> dict[str, Any] | None:
        """The oldest wake that can run now, under the lock.  A wake behind one that is running or
        waiting for the same chat stays behind it, so one chat's wakes run in the order they came.
        A wake whose outcome is settled has only its receipt left, and holds nobody up."""
        now = self.clock()
        blocked: set[str] = set(self._active)
        blocked.update(_session_of(e) for e in self._entries.values() if e.get("running"))
        full = self._full()
        picking = False  # an earlier wake still being picked may turn out to want the same chat
        for entry in sorted(self._entries.values(), key=_order):
            if entry.get("running"):
                picking = picking or entry["pick"] is None
                continue
            session = "" if entry.get("receipt") is not None else _session_of(entry)
            if session and (picking or full or session in blocked):
                continue
            if entry["next_at"] > now:
                if session:
                    blocked.add(session)
                continue
            entry["running"] = True
            return entry
        return None

    def _full(self) -> bool:
        """Whether as many deliveries are running as may run at once."""
        return len(self._active) >= int(self.config["max_deliveries"])

    def _behind(self, entry: Mapping[str, Any], session: str) -> bool:
        """Whether an earlier unsettled wake wants this chat, or is being picked and may."""
        mine = _order(entry)
        for other in self._entries.values():
            if other is entry or other.get("receipt") is not None or _order(other) >= mine:
                continue
            if _session_of(other) == session or (other.get("running") and other["pick"] is None):
                return True
        return False

    def _loop(self) -> None:
        while True:
            try:
                with self._cond:
                    current = threading.current_thread()
                    if self._stopping or current in self._retiring:
                        self._retiring.discard(current)
                        return
                    entry = self._claim()
                    if entry is None:
                        self._cond.wait(timeout=1.0)
                        continue
                self._run(entry)
            except Exception as exc:  # noqa: BLE001 - a dead worker would still look healthy
                self._log("worker-loop-error-%s" % type(exc).__name__, "-")
                time.sleep(1.0)

    def run_once(self) -> bool:
        """Run one claimable wake on this thread, or answer False.  The workers' body, for tests."""
        with self._cond:
            entry = self._claim()
        if entry is None:
            return False
        self._run(entry)
        return True

    def _run(self, entry: dict[str, Any]) -> None:
        try:
            self._process(entry)
        except Exception as exc:  # noqa: BLE001 - one bad wake must not take a worker with it
            code = "worker-error-%s" % type(exc).__name__
            self._log(code, entry["wake"]["wake_id"])
            try:
                if entry.get("in_flight") and entry.get("receipt") is None:
                    # The delivery may have run, and its outcome was lost:  never run it again.
                    entry["receipt"] = self._interrupted(entry)
                    entry.pop("in_flight", None)
                    self._save(entry)
                    self._write_receipt(entry)
                else:
                    self._retry(entry, counted=True, code=code)
            except Exception as again:  # noqa: BLE001 - it stays claimed, so it cannot run twice
                self._log("worker-error-%s" % type(again).__name__, entry["wake"]["wake_id"])

    def _process(self, entry: dict[str, Any]) -> None:
        if entry.get("receipt") is not None:
            self._write_receipt(entry)  # already settled:  only its receipt is outstanding
            return
        if self.clock() - entry["accepted_at"] >= int(self.config["max_age_seconds"]):
            # An outage can outlast a wake:  nobody is waiting on one this old any more.
            old = _pick_from(entry["pick"]) if entry["pick"] else Pick(reason=EXPIRED)
            result = Delivery("failed", code=EXPIRED)
            self._finish(entry, old, status="dead", delivered=False, result=result)
            return
        if not getattr(self.config, "confirmed", True):
            # Before the pick, so a pick is never kept across an outage and delivered stale.
            kept = _pick_from(entry["pick"]) if entry["pick"] else None
            waiting = Delivery("busy", code=UNCONFIRMED)
            self._retry(entry, counted=False, code=UNCONFIRMED, pick=kept, result=waiting)
            return
        wake = Wake(**entry["wake"])
        if entry["pick"] is None:
            pick = self.pick(wake)
            if pick.retry:
                if pick.reason == REGISTRY_UNAVAILABLE:
                    # A registry that cannot be read is an outage, not a failed delivery.
                    self._retry_outage(entry, pick=pick)
                    return
                self._retry(
                    entry,
                    counted=True,
                    code=pick.reason,
                    pick=pick,
                    result=Delivery("failed", code=pick.reason, note=pick.note),
                )
                return
            entry["pick"] = asdict(pick)
            self._save(entry)
        pick = _pick_from(entry["pick"])
        if pick.target is None:
            self._finish(entry, pick, status="held", delivered=False, result=None)
            return
        if not self.config["delivery_enabled"]:
            held = replace(pick, reason="delivery-disabled")
            self._finish(entry, held, status="held", delivered=False, result=None)
            return
        session = pick.target.session_id
        with self._cond:
            if (
                self._stopping
                or self._full()
                or session in self._active
                or self._behind(entry, session)
            ):
                # Shutting down, at the cap, or another wake has this chat first:  go back with the
                # pick recorded, using no attempt.  Its place in the order is kept by `_claim`.
                entry["running"] = False
                self._cond.notify_all()
                return
            self._active.add(session)
        # The chat stays held until the outcome is settled, so a later wake cannot start between
        # this delivery ending and its result being recorded.
        try:
            stale, stale_note = self._stale(pick.target)
            if stale == RELEASED:
                self._released(entry, pick, wake)
                return
            if stale:
                if stale == REGISTRY_UNAVAILABLE:
                    # The check before delivery could not read the registry:  an outage.
                    self._retry_outage(entry, pick=pick)
                    return
                self._retry(
                    entry,
                    counted=True,
                    code=stale,
                    pick=pick,
                    result=Delivery("failed", code=stale, note=stale_note),
                )
                return
            if self._turn_cap() >= 2 and not entry.get("busy_seen"):
                # A platform that takes turns:  the riders this idle turn may carry, marked on
                # disk before the head's commit (module docstring, Turns).  None, or a head
                # whose chat has answered it busy, and the wake goes alone, exactly as below.
                turn_id = "t-" + secrets.token_hex(8)
                riders = self._board(self._gather(entry, pick, turn_id), wake.wake_id, turn_id)
                if riders:
                    self._deliver_turn(entry, pick, wake, riders, turn_id)
                    return
            entry["attempts"] += 1
            entry["in_flight"] = True
            if not self._save(entry):
                # Without the durable mark a crash would run this again, so it does not start.
                entry["attempts"] -= 1
                entry.pop("in_flight", None)
                waiting = Delivery("busy", code="in-flight-mark-failed")
                self._retry(entry, counted=False, code=waiting.code, pick=pick, result=waiting)
                return
            told = replace(wake, picked=pick.picked, reason=pick.reason, link_note=pick.link_note)
            self._give_up_slot()
            try:
                result = self.platform.deliver(pick.target, told)
            except Exception as exc:  # noqa: BLE001 - a platform's failure is a failed try
                result = Delivery("failed", code="deliver-error-%s" % type(exc).__name__)
            self._settle(entry, pick, result)
        finally:
            with self._cond:
                self._active.discard(session)
                self._cond.notify_all()

    def _stale(self, target: Target) -> tuple[str, str]:
        """The reason this chat must not be delivered to, and the note that says why.

        The reason is empty when the registry still has this letter on this session at the
        generation picked, else `RELEASED` when it says so, or the code of a bounded retry when
        it cannot be read or its row cannot be read as a chat of this seat.  Neither of those
        is a yes or a no.  The note names the variable that is missing, when the registry named
        one, so the receipt of the failure says what to fix."""
        answer = registry_api.call(
            self.env,
            "resolve",
            {"platform": self.platform.platform, "letter": target.letter},
            timeout=REGISTRY_TIMEOUT,
        )
        if not isinstance(answer, Mapping) or "error" in answer:
            note = registry_note(answer.get("error")) if isinstance(answer, Mapping) else ""
            return registry_trouble(answer), note
        held = holds(answer, target, self.platform.seat, self.platform.target_re)
        if held is None:
            return UNREADABLE_ROW, ""
        return (RELEASED, "") if not held else ("", "")

    def _released(self, entry: dict[str, Any], pick: Pick, wake: Wake) -> None:
        """The picked chat is gone or re-registered, and nothing was delivered.  A wake addressed
        to a letter ends here.  Any other goes back through the pick, a bounded number of times so
        a registry whose two reads disagree cannot spin a worker."""
        entry["repicks"] = entry.get("repicks", 0) + 1
        if wake.letter or entry["repicks"] > int(self.config["max_attempts"]):
            result = Delivery("refused", code=RELEASED)
            self._finish(entry, pick, status="dead", delivered=False, result=result)
            return
        entry["pick"] = None
        self._save(entry)
        with self._cond:
            entry["running"] = False
            self._cond.notify_all()

    def _settle(self, entry: dict[str, Any], pick: Pick, result: Delivery) -> None:
        outcome = result.outcome if result.outcome in OUTCOMES else "failed"
        if result.code == REGISTRY_UNAVAILABLE and outcome not in ("unknown", "refused"):
            # A registry that cannot be read is an outage, not a failed delivery, whichever
            # platform answered it:  it spends none of the wake's attempts.  An `unknown`
            # answer stays final, because the delivery may have run and nobody can tell.
            self._retry_outage(entry, result=result, pick=pick)
            return
        if outcome in ("sent", "completed"):
            # A hand-over with no proof is recorded as unknown (null), never as delivered.
            delivered = True if outcome == "completed" or result.ack == "confirmed" else None
            self._finish(entry, pick, status="delivered", delivered=delivered, result=result)
        elif outcome == "refused":
            delivered = False if result.code in self.never_ran else None
            self._finish(entry, pick, status="dead", delivered=delivered, result=result)
        elif outcome == "failed":
            self._retry(entry, counted=True, code=result.code or "failed", pick=pick, result=result)
        elif outcome == "unknown":
            # The delivery may have run the wake and nobody can tell:  it is not recorded as
            # delivered, and it is not retried, because a retry could run the turn twice.  The
            # receipt keeps it visible, so an operator can see what is not settled.
            self._finish(entry, pick, status="unknown", delivered=None, result=result)
        else:
            held = result is not None and result.code in ("chat-live", "chat-live-parked")
            if outcome == "busy" and held and self._turn_cap() >= 2:
                # Its chat is in a live turn:  this wake goes alone from now on, so no rider is
                # marked for every try until the chat lets go (module docstring, Turns).  Only a
                # held chat counts:  a transient busy may clear, but a parked wake must also go
                # alone, because the hook may already have handed it over and batching it into a
                # later turn would run it again.
                entry["busy_seen"] = True
            self._retry(entry, counted=False, code=result.code or outcome, pick=pick, result=result)

    def _retry_outage(
        self,
        entry: dict[str, Any],
        result: Delivery | None = None,
        pick: Pick | None = None,
    ) -> None:
        """Requeue a wake whose registry could not be read.

        An outage is not a failed delivery:  it spends none of the wake's five attempts, so a
        registry that is down for twenty minutes cannot kill a wake it would have carried.
        The wait grows like a failed delivery's backoff, because an outage may last hours and
        every try costs a call to a service that is down;  the wake's age still bounds it.

        `result` is the answer that said so, when there is one:  the platform's own delivery
        answer keeps its route and its note.  The answer this builds for itself is the outage
        answer, never a failure:  a wake that ends while the registry is down says the registry
        was down, and does not blame malformed data."""
        self._log(REGISTRY_UNAVAILABLE, entry["wake"]["wake_id"])
        outages = int(entry.get("outages", 0)) + 1
        entry["outages"] = outages
        backoff = self.config["retry_backoff"]
        self._retry(
            entry,
            counted=False,
            code=REGISTRY_UNAVAILABLE,
            pick=pick,
            result=result or registry_outage(),
            wait=backoff[min(outages - 1, len(backoff) - 1)],
        )

    def _retry(
        self,
        entry: dict[str, Any],
        *,
        counted: bool,
        code: str,
        pick: Pick | None = None,
        result: Delivery | None = None,
        wait: float | None = None,
    ) -> None:
        """Requeue a wake.

        `counted` says whether this try spends one of the wake's attempts:  a failed delivery
        does, and a dependency that is merely unavailable does not.  `wait` is for the caller
        that knows better than the defaults:  a counted retry uses the backoff."""
        entry.pop("turn", None)  # a try that is tried again carried nobody
        now = self.clock()
        if counted:
            entry["failures"] += 1
        too_many = entry["failures"] >= int(self.config["max_attempts"])
        too_old = now - entry["accepted_at"] >= int(self.config["max_age_seconds"])
        if too_many or too_old:
            final = result or Delivery("failed", code=code)
            self._finish(
                entry, pick or Pick(reason=code), status="dead", delivered=False, result=final
            )
            return
        if counted:
            backoff = self.config["retry_backoff"]
            wait = backoff[min(entry["failures"] - 1, len(backoff) - 1)]
        elif wait is None:
            wait = (
                self.config["busy_retry_seconds"]
                if result is not None and result.outcome == "busy"
                else self.config["unreachable_retry_seconds"]
            )
        entry["next_at"] = now + wait
        entry.pop("in_flight", None)  # the outcome says this try did not run the wake
        # Saved while this worker still owns the entry:  once it is released another worker may
        # finish it, and a late save would bring its pending file back.
        self._save(entry)
        with self._cond:
            entry["running"] = False
            self._cond.notify_all()

    def _save(self, entry: Mapping[str, Any]) -> bool:
        """Keep the pending file current, so a replay keeps the pick, the counts and the in-flight
        mark, and answer whether it was written.  A failure leaves the previous file in place:  the
        wake is already on disk.  Only the in-flight mark has to be written before going on;  a
        mark that cannot then be cleared errs toward a final receipt, never a second delivery."""
        record = {k: v for k, v in entry.items() if k != "running"}
        try:
            atomic_write(self._pending(entry["wake"]["wake_id"]), record)
        except OSError as exc:
            self._log("pending-write-failed-%s" % type(exc).__name__, entry["wake"]["wake_id"])
            return False
        return True

    # ---- turns (module docstring, Turns) ----------------------------------------------------

    def _turn_cap(self) -> int:
        """How many wakes one turn may carry for this seat:  1, today's path, unless the platform
        declares deliver_turn and the setting is an int above 1;  never above TURN_MAX_WAKES."""
        if not callable(self.platform.deliver_turn):
            return 1
        value = self.config.get("turn_max_wakes", 1)
        if type(value) is not int:
            return 1
        return max(1, min(value, TURN_MAX_WAKES))

    @staticmethod
    def _told(entry: Mapping[str, Any]) -> Wake:
        """A wake as its chat is told about it:  with its pick's reason and note."""
        pick = _pick_from(entry["pick"])
        return replace(
            Wake(**entry["wake"]), picked=pick.picked, reason=pick.reason, link_note=pick.link_note
        )

    def _gather(self, entry: dict[str, Any], pick: Pick, turn_id: str) -> list[dict[str, Any]]:
        """The wakes that may ride with this head, marked running so no worker claims them:  the
        run of exact-address wakes for the same chat that comes next, in arrival order, within
        the caps.  An unpicked wake that could want this chat, or one of this chat that cannot
        ride, ends the run;  one addressed to another seat or letter is passed over like any
        other chat's wake.  There is no deduplication by message id."""
        target = pick.target
        if not certain(pick) or not message_id_ok(entry["wake"]["message_id"]) or self._stopping:
            return []
        if target is None:  # certain() has said so already;  this only tells a type checker
            return []
        head = self._told(entry)
        riders: list[dict[str, Any]] = []
        with self._cond:
            key, session = turn_key(target), target.session_id
            now, cap, mine = self.clock(), self._turn_cap(), _order(entry)
            for other in sorted(self._entries.values(), key=_order):
                if other is entry or _order(other) <= mine or other.get("receipt") is not None:
                    continue
                if other["pick"] is None:
                    # Unpicked:  it may want this chat, as in `_behind`, unless it is addressed
                    # to another seat or letter (a missing letter never proves another chat).
                    w = other.get("wake") or {}
                    m = entry.get("wake") or {}
                    wletter = w.get("letter") or ""
                    mletter = m.get("letter") or ""
                    if wletter and mletter and (w.get("seat") != m.get("seat") or wletter != mletter):
                        continue
                    break
                if _session_of(other) != session:
                    continue
                if not self._may_ride(other, key, now) or len(riders) + 2 > cap:
                    break
                told = [self._told(rider) for rider in [*riders, other]]
                text = turn_text(" " * TURN_PREFIX_MAX, head, told, turn_id)
                if len(text) > TURN_MAX_PROMPT_CHARS:
                    break
                riders.append(other)
            # The wire's own validator agrees with the walk, or the turn is cut where it does not.
            offer = turn_offer(turn_id, [self._told(rider) for rider in riders])
            riders = riders[: len(turn_riders_from(offer, entry["wake"]["wake_id"]))]
            for rider in riders:
                rider["running"] = True
        return riders

    def _may_ride(
        self, other: Mapping[str, Any], key: tuple[str, str, int, str], now: float
    ) -> bool:
        """Whether this pending wake may ride a turn for the chat `key` names now."""
        if other.get("running") or other.get("receipt") is not None:
            return False
        if any(mark in other for mark in ("in_flight", "ride", "turn")):
            return False
        pick = _pick_from(other["pick"])
        return (
            certain(pick)
            and pick.target is not None
            and turn_key(pick.target) == key
            and message_id_ok(other["wake"]["message_id"])
            and now - other["accepted_at"] < int(self.config["max_age_seconds"])
        )

    def _board(
        self, riders: list[dict[str, Any]], head_id: str, turn_id: str
    ) -> list[dict[str, Any]]:
        """Mark each rider on disk, in order, before the head's commit:  an attempt, the in-flight
        mark and `ride`.  The first mark that cannot be written cuts the turn there:  that rider
        and every later one go back untouched.  Answers the riders whose marks landed."""
        for index, rider in enumerate(riders, start=1):
            rider["attempts"] += 1
            rider["in_flight"] = True
            rider["ride"] = {"turn": turn_id, "head": head_id, "index": index}
            try:
                saved = self._save(rider)
            except Exception as exc:  # noqa: BLE001 - a mark that is not written is no mark
                self._log("ride-mark-error-%s" % type(exc).__name__, rider["wake"]["wake_id"])
                saved = False
            if saved:
                continue
            rider["attempts"] -= 1
            rider.pop("in_flight", None)
            rider.pop("ride", None)
            with self._cond:
                for left in riders[index - 1 :]:
                    left["running"] = False
                self._cond.notify_all()
            return riders[: index - 1]
        return riders

    def _unboard(self, riders: Iterable[dict[str, Any]]) -> None:
        """Riders the turn did not carry go back to pending with nothing used.  A save that fails
        leaves the marks in the file while memory is clear:  the wake's next save rewrites it,
        and a crash before that closes it as interrupted, never a second run."""
        for rider in riders:
            rider["attempts"] -= 1
            rider.pop("in_flight", None)
            rider.pop("ride", None)
            try:
                self._save(rider)
            except Exception as exc:  # noqa: BLE001 - logged;  the rider is released regardless
                self._log("unboard-error-%s" % type(exc).__name__, rider["wake"]["wake_id"])
            with self._cond:
                rider["running"] = False
                self._cond.notify_all()

    def _deliver_turn(
        self,
        entry: dict[str, Any],
        pick: Pick,
        wake: Wake,
        riders: list[dict[str, Any]],
        turn_id: str,
    ) -> None:
        """The head's commit, then one `deliver_turn`, then every member's outcome.  The commit is
        the last write before the effect:  without it nothing is delivered."""
        called = False
        try:
            entry["attempts"] += 1
            entry["in_flight"] = True
            entry["turn"] = {
                "id": turn_id,
                "riders": [rider["wake"]["wake_id"] for rider in riders],
                "message_ids": [entry["wake"]["message_id"]]
                + [rider["wake"]["message_id"] for rider in riders],
            }
            if not self._save(entry):  # the commit point:  the last write before the effect
                entry["attempts"] -= 1
                entry.pop("in_flight", None)
                entry.pop("turn", None)
                self._unboard(riders)
                waiting = Delivery("busy", code="in-flight-mark-failed")
                self._retry(entry, counted=False, code=waiting.code, pick=pick, result=waiting)
                return
            head = replace(wake, picked=pick.picked, reason=pick.reason, link_note=pick.link_note)
            told = tuple(self._told(rider) for rider in riders)
            self._give_up_slot()
            called = True
            try:
                # `_turn_cap` only allows a turn when deliver_turn is callable.
                turn = self.platform.deliver_turn(pick.target, head, told, turn_id)
            except Exception as exc:  # noqa: BLE001 - a platform's failure is a failed try
                turn = Turn(Delivery("failed", code="deliver-error-%s" % type(exc).__name__))
            if not isinstance(turn, Turn) or not isinstance(turn.delivery, Delivery):
                turn = Turn(Delivery("refused", code=TURN_BAD_ANSWER), unreadable=True)
            self._settle_turn(entry, pick, riders, turn)
        except Exception:
            self._abandon(riders, called)
            if not called and entry.get("receipt") is None and entry.pop("in_flight", None):
                # `deliver_turn` was never called, so nothing ran:  the head's commit is taken
                # back and `_run` retries it as a failed try, instead of closing it unknown.
                entry["attempts"] -= 1
                entry.pop("turn", None)
            raise  # `_run` closes a head that may have run, and retries any other

    def _abandon(self, riders: Iterable[dict[str, Any]], called: bool) -> None:
        """A worker's exception in a turn:  a rider still marked is closed as interrupted when
        `deliver_turn` may have run, or goes back when it cannot have;  any other is let go."""
        for rider in riders:
            wake_id = rider["wake"]["wake_id"]
            try:
                if self._entries.get(wake_id) is not rider:
                    continue  # settled and written
                if rider.get("receipt") is None and rider.get("in_flight"):
                    if called:
                        self._log(INTERRUPTED, wake_id)
                        rider["receipt"] = self._interrupted(rider)
                        rider.pop("in_flight", None)
                        self._save(rider)
                        self._write_receipt(rider)
                    else:
                        self._unboard([rider])
                    continue
                with self._cond:
                    rider["running"] = False
                    self._cond.notify_all()
            except Exception as exc:  # noqa: BLE001 - the next rider is still let go
                self._log("turn-abandon-error-%s" % type(exc).__name__, wake_id)

    def _settle_turn(
        self, entry: dict[str, Any], pick: Pick, riders: list[dict[str, Any]], turn: Turn
    ) -> None:
        """Every member's outcome:  the riders in order, each closed or sent back, then the head
        by today's rules (`_settle`).  A rider is acknowledged only when a readable carry list
        names it, and none is ever recorded as carried by a turn that cannot have run."""
        result = turn.delivery
        outcome = result.outcome if result.outcome in OUTCOMES else "failed"
        # An answer `_settle` waits out as a registry outage ran nothing, whatever its outcome
        # says:  the head goes back with no attempt spent, so its riders go back with it.
        outage = result.code == REGISTRY_UNAVAILABLE and outcome not in ("unknown", "refused")
        ran = not outage and (
            outcome in ("sent", "completed", "unknown")
            or (outcome == "refused" and result.code not in self.never_ran)
        )
        ids = [rider["wake"]["wake_id"] for rider in riders]
        # A module may build its Turn itself, so its carry list is checked here too.
        unreadable = bool(turn.unreadable)
        if turn.carried is not None and not _carry_ok(turn.carried, turn.confirmed, ids):
            unreadable = True
        readable = turn.carried is not None and not unreadable
        carried = set(turn.carried or ()) if readable else set()
        confirmed = set(turn.confirmed) if readable else set()

        def close(rider: dict[str, Any], status: str, delivered: bool | None, ack: str, code: str):
            final = Delivery(outcome, result.route, ack=ack, code=code, note=result.note)
            self._finish(
                rider, _pick_from(rider["pick"]), status=status, delivered=delivered, result=final
            )

        for rider, wake_id in zip(riders, ids, strict=True):
            ack = "confirmed" if wake_id in confirmed else "unknown"
            if not ran:
                if wake_id in carried:  # an answer that claims what cannot have happened
                    close(rider, "unknown", None, "unknown", TURN_CONFLICT)
                else:
                    self._unboard([rider])
            elif unreadable:
                close(rider, "unknown", None, "unknown", TURN_UNCONFIRMED)
            elif wake_id not in carried:  # no echo at all, or not this far:  not carried
                self._unboard([rider])
            elif outcome in ("sent", "completed"):
                if outcome == "completed" or wake_id in confirmed:
                    close(rider, "delivered", True, ack, result.code)
                else:
                    close(rider, "unknown", None, ack, TURN_UNCONFIRMED)
            elif outcome == "refused":
                close(rider, "dead", None, ack, result.code)
            else:
                close(rider, "unknown", None, ack, result.code)
        # What the head's receipt says was carried:  null only when the riders were closed
        # because nobody can tell;  [] when none was carried and they went back, and always []
        # when the turn cannot have run, even if its echo names riders.
        if not ran:
            carried_ids = []
        elif unreadable:
            carried_ids = None
        else:
            carried_ids = [i for i in ids if i in carried]
        entry["turn"]["carried"] = carried_ids
        self._settle(entry, pick, result)

    # ---- the receipt ------------------------------------------------------------------------

    def _receipt(
        self,
        entry: Mapping[str, Any],
        pick: Pick,
        *,
        status: str,
        delivered: bool | None,
        result: Delivery | None,
    ) -> dict[str, Any]:
        wake = entry["wake"]
        target = pick.target
        receipt = {
            "receipt_id": entry["receipt_id"],
            "wake_id": wake["wake_id"],
            "seat": self.platform.seat,
            "at": self.clock(),
            "test": False,
            "status": status,
            "letter": wake["letter"],
            "letter_live": pick.letter_live,
            # A seat tag (`row_seat`) or "":  the addressed letter is open under that other seat.
            "letter_other_seat": pick.letter_other_seat,
            "picked": pick.picked,
            "picked_letter": target.letter if target else "",
            "picked_generation": target.generation if target else 0,
            "session_id": target.session_id if target else "",
            "host": target.host if target else "",
            "thread_link": target.thread_link if target else "",
            "reason": pick.reason,
            "candidates": list(pick.candidates),
            "link_note": pick.link_note,
            "delivered": delivered,
            "route": result.route if result else "",
            "ack": result.ack if result else "",
            "code": result.code if result else "",
            "note": result.note if result else "",
            "attempts": entry["attempts"],
            "replayed": bool(entry.get("replayed")),
            "channel": wake["channel"],
            "topic": wake["topic"],
            "zulip_link": wake["zulip_link"],
        }
        turn = _turn_record(entry)  # only a member of a turn has one (module docstring, Turns)
        if turn is not None:
            receipt["turn"] = turn
        return receipt

    def _finish(
        self,
        entry: dict[str, Any],
        pick: Pick,
        *,
        status: str,
        delivered: bool | None,
        result: Delivery | None,
    ) -> None:
        """Write the final receipt, then drop the pending entry, in that order:  a crash between
        the two leaves a pending file that replay removes, never a wake with no record."""
        receipt = self._receipt(entry, pick, status=status, delivered=delivered, result=result)
        # The outcome is kept on the entry first:  while this process lives only the receipt is
        # retried.  It reaches a restart only if this save lands.  If the save fails too and the
        # router dies before the receipt is written, the file still has its in-flight mark, so the
        # replay closes it as interrupted;  a delivery that ran is still never run again.
        entry["receipt"] = receipt
        entry.pop("in_flight", None)
        self._save(entry)
        self._write_receipt(entry)

    def _write_receipt(self, entry: dict[str, Any]) -> None:
        wake_id = entry["wake"]["wake_id"]
        receipt = entry["receipt"]
        try:
            atomic_write(self._done(wake_id), receipt)
        except OSError as exc:
            # The wake stays pending and tries to write its receipt again;  it is not lost.
            self._log("receipt-write-failed-%s" % type(exc).__name__, wake_id)
            with self._cond:
                entry["next_at"] = self.clock() + float(self.config["busy_retry_seconds"])
                entry["running"] = False
                self._cond.notify_all()
            return
        self._append_log(receipt)
        with self._cond:
            self._pending(wake_id).unlink(missing_ok=True)
            self._entries.pop(wake_id, None)
            self._cond.notify_all()
        self._log("%s %s" % (receipt["status"], receipt["reason"] or receipt["code"]), wake_id)

    def _append_log(self, receipt: Mapping[str, Any]) -> None:
        path = self.state / "wakes.jsonl"
        try:
            private_dir(path.parent)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(receipt, sort_keys=True) + "\n")
        except OSError as exc:
            self._log("log-write-failed-%s" % type(exc).__name__, str(receipt.get("wake_id")))

    def _log(self, code: str, wake_id: str) -> None:
        """The seat, codes and wake ids only:  no payload, no names, no key."""
        sys.stderr.write("wake-router: %s %s %s\n" % (self.platform.seat, wake_id, code))


def registry_configured(env: Mapping[str, str]) -> bool:
    """Whether this env lets registry_api sign a call:  an address and a key, read with
    registry_api's own readers (SIMPLE_TOOLS_OPERATOR_REGISTRY_URL and _KEY).  A router's env is
    an explicit mapping, so they read only that mapping and never reach Infisical here."""
    return bool(registry_api.base_url(env)) and bool(registry_api.signing_key(env))


def registry_trouble(answer: object) -> str:
    """The code for a registry answer that is not a usable reading.

    `registry-not-configured` when this host has no registry address or key (registry_api's
    `not_configured`);  `registry-unavailable` when the registry could not be read, which is an
    outage and spends no attempt;  `registry-answer-unreadable` when the answer is not a
    reading at all, which is malformed external data and not an outage.  Nothing is delivered on
    any of them, and the shape of the answer is checked here, before any other code reads a
    field of it.

    The shape is the whole shape:  an error that is not a non-empty string, or a code that is
    not a string, is not an answer the registry speaks, so it is malformed data rather than a
    trouble to retry."""
    if not isinstance(answer, Mapping):
        return UNREADABLE_ANSWER
    error = answer.get("error")
    if not isinstance(error, str) or not error.strip():
        return UNREADABLE_ANSWER
    code = answer.get("code")
    if code is not None and not isinstance(code, str):
        return UNREADABLE_ANSWER
    if code == "not_configured":
        return NOT_CONFIGURED
    return REGISTRY_UNAVAILABLE


def registry_loader_settings(env: Mapping[str, str]) -> set[str]:
    """The registry settings this host's shared loader file assigns, by name.

    The Mac reads the registry's address and key from the shared loader file, and the
    loader's own reader swallows a read that fails:  a mapping without an address cannot
    say whether the loader holds none or could not be read.  This says which the file
    holds, from its own words and never a value:  a loader that assigns a setting holds
    one.

    The file is read a line at a time and every line is dropped after it is matched, so a
    value it holds is never kept.  A host with no loader file at all holds none, and
    `registry_failure` answers that one.  A loader that is there and cannot be read raises,
    so the caller can tell a host with no loader from a read that failed."""
    from . import platform_letter as PL

    home = str(env.get("HOME") or "")
    if not home:
        return set()
    names = (registry_api.URL_ENV, registry_api.KEY_VAR, registry_api.PRINCIPAL_ENV)
    found: set[str] = set()
    try:
        with open(
            os.path.join(home, PL.REGISTRY_ENV_SUBPATH), "r", encoding="utf-8", errors="replace"
        ) as handle:
            for line in handle:
                for name in names:
                    if re.match(r"\s*(?:export\s+)?%s\s*=" % re.escape(name), line):
                        found.add(name)
    except FileNotFoundError:
        # No loader file at all:  the host holds none of the registry's settings.
        return found
    return found


def registry_loader_lost(env: Mapping[str, str], registry_env: Mapping[str, str]) -> bool:
    """Whether the shared loader holds the registry's address and the mapping does not carry it.

    That is the read that failed:  the file says which settings it holds, and a mapping
    without the address the file holds lost it on the way.  A loader that is there and cannot
    be read lost it the same way.  A loader that was read and holds no address is a host that
    is not configured, and `registry_failure` answers that one with the variable that is
    missing.

    The probe is the Mac's, because the shared loader file is the Mac's source.  A host that
    reads its configuration elsewhere never lost the loader's address, so it never answers
    with the loader's name:  that host's own source is named instead."""
    if not REGISTRY_LOADER_HOST:
        return False  # the loader file is not this host's source, so it cannot have lost anything
    if registry_api.base_url(registry_env):
        return False
    try:
        held = registry_loader_settings(env)
    except OSError:
        # The loader is there and could not be read:  the read lost the address, and the
        # source that could not be read is named.
        return True
    return registry_api.URL_ENV in held


def registry_note(text: object) -> str:
    """The name of the setting a registry error names, as a token the transport keeps.

    The note crosses between hosts and is read back through a whitelist that admits only
    letters, digits, colon, underscore and dash, so free text is thrown away exactly when it
    is meant to help.  The setting's own name is what an operator needs, and it survives.

    Only the registry's own setting names are ever carried.  The text is the registry's
    answer, so a free run of capitals in it is whatever the registry put there, and a receipt
    is read by anyone who can read a wake:  nothing else may reach one."""
    raw = text if isinstance(text, str) else ""
    for name in (registry_api.URL_ENV, registry_api.KEY_VAR, registry_api.PRINCIPAL_ENV):
        if re.search(r"\b%s\b" % re.escape(name), raw):
            return name[:NOTE_CHARS]
    return ""


def registry_outage(route: str = "") -> Delivery:
    """The answer for a registry that could not be reached at all.

    Nothing ran and nothing is the chat's fault, so the router retries the wake without
    spending one of its five attempts, and the wake waits for the registry to come back.
    A host that is not configured to reach the registry is not an outage:  `registry_failure`
    answers that one, and it fails closed."""
    return Delivery("busy", route, code=REGISTRY_UNAVAILABLE)


def registry_failure(env: Mapping[str, str] | None = None, route: str = "") -> Delivery:
    """The answer for a registry call that raised.

    A host that is not configured to reach the registry at all has no address and no key.
    That is a deployment fault, which no retry can repair, so it fails closed:  `failed`, one
    of the wake's attempts spent, and the note names the variable that is missing.  Every
    other raise is a reachability failure, which is an outage:  `busy`, no attempt spent, and
    the wake waits for the registry to come back.

    The configuration is read with the registry API's own readers, so the answer names the
    same variable the registry itself names.  A configuration that cannot be read at all
    fails closed too:  the host's configuration is unverifiable and nothing is delivered
    under it.  Its note names the setting when the error names one, and this host's own
    configuration source otherwise, in a token the transport keeps."""
    try:
        answer = registry_api.configuration(env)
    except Exception as exc:  # noqa: BLE001 - a configuration nobody can read fails closed
        # The note carries the registry's own setting names and nothing else:  the error's
        # words may hold a secret, and a receipt is read by anyone who can read a wake.
        note = registry_note(str(exc)) or REGISTRY_HOST_SOURCE
        return Delivery("failed", route, code=NOT_CONFIGURED, note=note)
    configured = bool(answer.get("configured"))
    reason = str(answer.get("reason") or "")
    if not configured:
        return Delivery("failed", route, code=NOT_CONFIGURED, note=registry_note(reason))
    return registry_outage(route)


def registry_delivery(answer: object, route: str = "") -> Delivery:
    """The answer every platform gives when the registry answered something that is not a
    usable reading.

    The shape of the answer is checked before any field of it is read.  An answer that is not
    a mapping is malformed external data:  it is `failed` with the code that names it, so the
    wake ends after the bounded attempts instead of waiting on a registry that is broken
    rather than down.

    An answer that carries an error is a trouble.  A host with no registry address or key is
    `failed`, because a retry does not fix a deployment fault, and the note names the variable
    that is missing.  Anything else is an outage:  it is `busy`, it spends none of the wake's
    attempts, and it waits for the registry."""
    code = registry_trouble(answer)
    if code == REGISTRY_UNAVAILABLE:
        return Delivery("busy", route, code=code)
    note = ""
    if isinstance(answer, Mapping):
        note = registry_note(answer.get("error"))
    return Delivery("failed", route, code=code, note=note)


def _closed(row: Mapping[str, Any]) -> bool:
    return bool(row.get("retired_at") or row.get("closed_at") or row.get("state") == "closed")


def row_seat(row: Mapping[str, Any]) -> str:
    """The seat a registry row names, upper-cased, or "" when it names none or a malformed one."""
    raw = row.get("seat")
    if not isinstance(raw, str) or not raw.isascii():
        return ""
    tag = raw.upper()
    return tag if SEAT_RE.fullmatch(tag) else ""


def target_from_row(row: Mapping[str, Any], seat: str, target_re: Pattern[str]) -> Target | None:
    """The chat a registry row describes, or None if it is not one a wake can go to.  The row has
    to name this seat itself:  a row with no seat is not taken to be this platform's."""
    sid = str(row.get("native_session_id") or "")
    if not target_re.fullmatch(sid):
        return None  # a crafted row can never steer an argv token a child reads as a flag
    if not seat or row_seat(row) != seat:
        return None
    letter = str(row.get("label") or row.get("letter") or "")
    generation = row.get("generation")
    if not letter or type(generation) is not int or generation < 1:
        return None  # the check before delivery needs both, so it could never pass
    return Target(
        session_id=sid,
        letter=letter,
        host=str(row.get("verified_host") or ""),
        title=str(row.get("title") or ""),
        thread_link=str(row.get("thread_link") or ""),
        generation=generation,
    )


def other_seat(row: Mapping[str, Any], seat: str, target_re: Pattern[str]) -> str:
    """The seat tag an open registry row names when that is the only thing keeping it from being
    a chat of `seat`, else "".  The tag is `row_seat`'s, so it is shaped as a seat tag and nothing
    else of the row is kept.  A row with no seat or a malformed one answers "":  it names nobody,
    which is a row that cannot be read, not another seat's chat.  So does one whose session id,
    letter or generation would not make a target under its own seat either.  The caller has
    already ruled out a closed row, and the registry was asked for this platform's rows."""
    theirs = row_seat(row)
    if not seat or not theirs or theirs == seat:
        return ""
    return theirs if target_from_row(row, theirs, target_re) is not None else ""


def holds(
    answer: Mapping[str, Any], target: Target, seat: str, target_re: Pattern[str]
) -> bool | None:
    """Whether a registry `resolve` answer still has this letter open on this session at this
    generation.  False only when the registry says it does not:  the letter has no live holder, its
    chat is closed, or a well-formed chat of this seat holds it at another session or generation.
    None when a live answer is not a well-formed chat of this seat (no seat, a malformed one,
    another seat's, or a bad id or generation), because a row that cannot be read proves no
    release.  An answer that is not a mapping at all is None too, and so is one whose `found` is not
    exactly True or False (missing, None, 0, 1, a string).  The caller has already ruled out an
    answer that is an error."""
    if not isinstance(answer, Mapping):
        return None
    found = answer.get("found")
    if found is False:
        return False  # only the boolean itself is the registry saying no;  a 0 or a None is not
    if found is not True:
        return None
    if _closed(answer):
        return False
    current = target_from_row(answer, seat, target_re)
    if current is None:
        return None
    return current.session_id == target.session_id and current.generation == target.generation


def _session_of(entry: Mapping[str, Any]) -> str:
    pick = entry.get("pick") or {}
    target = pick.get("target") or {}
    return str(target.get("session_id") or "")


def _order(entry: Mapping[str, Any]) -> tuple[float, str]:
    """Arrival order, with the id breaking a tie so every check agrees on who is first."""
    return entry["accepted_at"], entry["wake"]["wake_id"]


def _loadable(entry: object, wake_id: str) -> bool:
    """Whether a pending file holds a whole entry for this id, with every field a worker reads."""
    if not isinstance(entry, dict):
        return False
    try:
        if entry["wake"]["wake_id"] != wake_id:
            return False
        Wake(**entry["wake"])
        if entry["pick"] is not None:
            _pick_from(entry["pick"])
        return (
            isinstance(entry["receipt_id"], str)
            and type(entry["accepted_at"]) in (int, float)
            and type(entry["next_at"]) in (int, float)
            and type(entry["attempts"]) is int
            and type(entry["failures"]) is int
            and type(entry.get("repicks", 0)) is int
            and isinstance(entry.get("receipt"), (dict, type(None)))
            and _turn_marks_ok(entry)
        )
    except (KeyError, TypeError, ValueError):
        return False


def _turn_marks_ok(entry: Mapping[str, Any]) -> bool:
    """Whether a pending file's turn marks are absent or whole:  `ride` and `turn` are objects
    of the shapes the router writes (module docstring, Turns), so a receipt can be made of them,
    and `busy_seen` is a bool.  An older router ignores these keys and still reads the in-flight
    mark beside them."""
    ride, turn = entry.get("ride"), entry.get("turn")
    if not isinstance(ride, (dict, type(None))) or not isinstance(turn, (dict, type(None))):
        return False
    if type(entry.get("busy_seen", False)) is not bool:
        return False
    if ride is not None and not (
        isinstance(ride.get("turn"), str)
        and isinstance(ride.get("head"), str)
        and type(ride.get("index")) is int
    ):
        return False
    if turn is None:
        return True

    def strings(value: object) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    message_ids = turn.get("message_ids")
    return (
        isinstance(turn.get("id"), str)
        and strings(turn.get("riders"))
        and isinstance(message_ids, list)
        and all(type(item) is int for item in message_ids)
        and (turn.get("carried") is None or strings(turn.get("carried")))
    )


def _pick_from(record: Mapping[str, Any]) -> Pick:
    known = {field.name for field in fields(Pick)}  # a file from an older router may have more
    values = {name: value for name, value in dict(record).items() if name in known}
    target = values.get("target")
    values["target"] = Target(**target) if target else None
    values["candidates"] = tuple(values.get("candidates") or ())
    return Pick(**values)


def _instant(value: object) -> float | None:
    """A registry time as epoch seconds, or None if it is not one.  Numbers, numeric strings and
    ISO strings with or without `Z` all occur;  a naive ISO string is UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    except ValueError:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in "Zz" else text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000 if number >= 1e11 else number  # milliseconds, from a JavaScript writer


def _bare_id(session_id: str) -> str:
    """A session id or a fragment of one as they are compared:  no `sess_` prefix, no hyphens."""
    return session_id.removeprefix(SESSION_PREFIX).replace("-", "")


def _is_sender(target: Target, wake: Wake) -> bool:
    bare = _bare_id(wake.sender_fragment)
    return bool(bare) and _bare_id(target.session_id).startswith(bare)


def _named_session(wake: Wake, live: Mapping[str, Target]) -> str:
    """The one live chat the wake's session-id fragments name, or "".  A fragment that fits several
    chats names none, two fragments that name different chats name none, and the sender's own chat
    is never the one named."""
    named: set[str] = set()
    for fragment in wake.session_fragments:
        bare = _bare_id(fragment)
        fits = [sid for sid in live if bare in _bare_id(sid)]
        if len(fits) == 1 and not _is_sender(live[fits[0]], wake):
            named.add(fits[0])
    return named.pop() if len(named) == 1 else ""


def _issues_of(row: Mapping[str, Any]) -> list[tuple[str, float | None]]:
    """A registry record's `issues`, as (id, seen_at).  A record may have none, and a malformed
    item is skipped."""
    value = row.get("issues")
    if not isinstance(value, list):
        return []
    found: list[tuple[str, float | None]] = []
    for item in value[:MAX_ROW_ISSUES]:
        issue = item.get("id") if isinstance(item, Mapping) else None
        if isinstance(issue, str) and ISSUE_RE.fullmatch(issue):
            found.append((issue, _instant(item.get("seen_at"))))
    return found


def _row_time(row: Mapping[str, Any]) -> float | None:
    for name in RECENCY_FIELDS:
        found = _instant(row.get(name))
        if found is not None:
            return found
    return None


def _most_recent(targets: list[Target], seen: Mapping[str, float | None]) -> Target:
    """The newest of these chats.  One with no usable time sorts last;  the id breaks a tie."""

    def key(target: Target) -> tuple[bool, float, str]:
        when = seen.get(target.session_id)
        return when is None, -(when or 0.0), target.session_id

    return min(targets, key=key)


class Seats:
    """Every seat one service serves:  a `Router` for each seat that was built, a reason token for
    each registered seat that was not, and the registered seats whose module is not installed
    yet.  Nothing here is shared between seats but the port:  each Router keeps its own folder,
    queue, workers and receipts."""

    def __init__(
        self,
        routers: Sequence[Router] = (),
        down: Mapping[str, str] | None = None,
        not_installed: Iterable[str] = (),
    ) -> None:
        self.routers: dict[str, Router] = {}
        for router in routers:
            seat = router.platform.seat
            if not SEAT_RE.fullmatch(seat) or seat in self.routers:
                raise ValueError("Seats:  each router needs a seat tag of its own")
            self.routers[seat] = router
        self.down: dict[str, str] = {}
        for seat, reason in (down or {}).items():
            if not SEAT_RE.fullmatch(seat) or seat in self.routers:
                raise ValueError("Seats:  a seat is served or down, never both")
            self.down[seat] = str(reason) or "unknown"
        # A seat whose module is not there yet is not served and is not a fault:  it shows as
        # not installed and leaves the service's health alone (wake_service, Which seats).
        self.not_installed: set[str] = set()
        for seat in not_installed:
            if not SEAT_RE.fullmatch(seat) or seat in self.routers or seat in self.down:
                raise ValueError("Seats:  a seat is served, down or not installed, only one")
            self.not_installed.add(seat)

    def names(self) -> list[str]:
        return sorted({*self.routers, *self.down, *self.not_installed})

    def unserved(self, seat: str) -> str | None:
        """Why a known seat is not served, or None when it is served or unknown."""
        if seat in self.not_installed:
            return NOT_INSTALLED
        return self.down.get(seat)

    def seat_health(self, seat: str) -> tuple[int, dict[str, Any]]:
        """One seat's state:  reason tokens, and for a served seat only the other-seat count
        (`Router.other_seat_health`), never a value and never the time of the read:  health
        needs no key, and that time would show when the seat was last woken."""
        router = self.routers.get(seat)
        if router is None:
            if seat in self.not_installed:
                return 503, {"seat": seat, "healthy": False, "reason": NOT_INSTALLED}
            reason = "seat-not-served:" + self.down.get(seat, "unknown")
            return 503, {"seat": seat, "healthy": False, "reason": reason}
        delivery = router.config.get("delivery_enabled") is True
        # The other-seat count is a warning beside the answer:  it never changes `healthy`.
        answer: dict[str, Any] = {"seat": seat, "delivery": delivery, **router.other_seat_health()}
        key, why = router.routine_key()
        if not key:
            reason = "routine-key-unavailable:" + why
            return 503, {**answer, "healthy": False, "reason": reason}
        if not registry_configured(router.env):
            # A router with no registry address or key can pick nothing, so it must not look
            # healthy.
            return 503, {**answer, "healthy": False, "reason": NOT_CONFIGURED}
        return 200, {**answer, "healthy": True}

    def health(self) -> tuple[int, dict[str, Any]]:
        """Every seat.  Healthy only when each seat that is installed is healthy and at least one
        is:  a seat not installed yet is listed and does not count against it."""
        every = {}
        for seat in self.names():
            state = dict(self.seat_health(seat)[1])
            state.pop("seat")
            every[seat] = state
        counted = [state for seat, state in every.items() if seat not in self.not_installed]
        healthy = bool(counted) and all(state["healthy"] for state in counted)
        return (200 if healthy else 503), {"healthy": healthy, "seats": every}

    def start(self) -> None:
        """Start each seat's router.  One that cannot start (its folder cannot be read, say) is
        stopped and reported down;  the others start regardless."""
        for seat, router in sorted(self.routers.items()):
            try:
                router.start()
            except Exception as exc:  # noqa: BLE001 - one seat's fault stays that seat's
                router.stop(0.0)
                del self.routers[seat]
                self.down[seat] = "start-error-%s" % type(exc).__name__
                router._log(self.down[seat], "-")

    def stop(self) -> bool:
        """Stop every seat at once, each waiting up to its own `shutdown_wait_seconds` for its
        running deliveries, and answer whether all of them finished.  Each seat's wait is bounded
        from one shared start, so the whole stop takes at most the longest seat's wait plus
        STOP_MARGIN_SECONDS.  A seat whose stop has not returned by then counts as not finished:
        its deliveries are left to the replay, which closes them as interrupted."""
        finished: dict[str, bool] = {}
        default = ROUTER_DEFAULTS["shutdown_wait_seconds"]
        waits = {
            seat: float(router.config.get("shutdown_wait_seconds", default))
            for seat, router in self.routers.items()
        }

        def one(seat: str, router: Router) -> None:
            finished[seat] = router.stop(waits[seat])

        threads = {
            seat: threading.Thread(target=one, args=(seat, router), daemon=True)
            for seat, router in self.routers.items()
        }
        began = time.monotonic()
        for thread in threads.values():
            thread.start()
        for seat, thread in threads.items():
            deadline = began + waits[seat] + STOP_MARGIN_SECONDS
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        done = {
            seat: finished.get(seat, False) and not threads[seat].is_alive() for seat in threads
        }
        for seat in sorted(seat for seat, ok in done.items() if not ok):
            self.routers[seat]._log("shutdown-left-deliveries-running", "-")
        return all(done.values())


def make_handler(target: Router | Seats) -> type[BaseHTTPRequestHandler]:
    """The HTTP handler for one service:  each path goes to the seat it names (module docstring,
    One process, a router per seat).  A single Router is served as the only seat."""
    seats = target if isinstance(target, Seats) else Seats([target])
    limit = REQUEST_TIMEOUT

    class Handler(BaseHTTPRequestHandler):
        timeout = limit  # a client that goes silent cannot hold a thread

        def setup(self) -> None:
            super().setup()
            # The socket timeout restarts on every byte, so a client that dribbles its headers or
            # its body needs one deadline for the whole request.
            self._deadline = threading.Timer(limit, self._cut_off)
            self._deadline.daemon = True
            self._deadline.start()

        def finish(self) -> None:
            self._deadline.cancel()
            super().finish()

        def _cut_off(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def log_message(self, fmt: str, *args: Any) -> None:  # the default prints paths
            pass

        def _answer(self, code: int, payload: Mapping[str, Any]) -> None:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.close_connection = True  # the client left, or the deadline cut it off

        def _router_for(self, seat: str) -> Router | None:
            """The seat's router, or None once a 404 (nobody registered it) or a 503 (it is
            registered and not served, with why) has been answered."""
            router = seats.routers.get(seat)
            if router is not None:
                return router
            reason = seats.unserved(seat)
            if reason is None:
                self._answer(404, {"error": "not found"})
            else:
                self._answer(503, {"error": "the seat %s is not served (%s)" % (seat, reason)})
            return None

        def _authorized(self, router: Router) -> bool:
            """Checked against the bearer this seat's router asks its own KeySource for."""
            key, why = router.routine_key()
            if not key:
                # Without the key nothing can be checked, so nothing is let in.
                self._answer(503, {"error": "the routine key is unavailable (%s)" % why})
                return False
            given = (self.headers.get("Authorization") or "").encode("utf-8")
            # Constant time:  the bearer is the only thing between the network and a wake.
            if not hmac.compare_digest(given, ("Bearer %s" % key).encode("utf-8")):
                self._answer(401, {"error": "bad bearer"})
                return False
            return True

        def do_GET(self) -> None:
            path = self.path or ""
            if path == "/health":
                self._answer(*seats.health())
                return
            found = HEALTH_PATH_RE.fullmatch(path)
            if found:
                if found.group(1) in seats.names():
                    self._answer(*seats.seat_health(found.group(1)))
                else:
                    self._answer(404, {"error": "not found"})
                return
            found = RECEIPT_PATH_RE.fullmatch(path)
            if not found:
                self._answer(404, {"error": "not found"})
                return
            router = self._router_for(found.group(1) or LEGACY_RECEIPT_SEAT)
            if router is None or not self._authorized(router):
                return
            record = router.receipt(found.group(2))
            if record is None:
                self._answer(404, {"error": "no such wake"})
            else:
                self._answer(200, record)

        def do_POST(self) -> None:
            found = WAKE_PATH_RE.fullmatch(self.path or "")
            if not found:
                self._answer(404, {"error": "not found"})
                return
            seat = found.group(1)
            router = self._router_for(seat)
            if router is None or not self._authorized(router):
                return
            raw_length = (self.headers.get("Content-Length") or "").strip()
            if not raw_length.isdigit() or len(raw_length) > 9:
                self._answer(400, {"error": "Content-Length missing or malformed"})
                return
            length = int(raw_length)
            if length <= 0 or length > MAX_BODY:
                self._answer(413, {"error": "body size refused"})
                return
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                self.close_connection = True  # cut off part way:  never act on part of a body
                return
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._answer(400, {"error": "body is not JSON"})
                return
            if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
                self._answer(400, {"error": "unknown contract"})
                return
            if str(payload.get("seat") or "") != seat:
                self._answer(403, {"error": "seat mismatch"})
                return
            if not WAKE_ID_RE.fullmatch(str(payload.get("wake_id") or "")):
                self._answer(400, {"error": "wake_id missing or malformed"})
                return
            wake = wake_from_payload(payload, router.platform.mention)
            if payload.get("test") is True:
                self._answer(200, router.test_answer(wake))
                return
            status, answer = router.accept(wake)
            self._answer(status, answer)

    return Handler


def serve(target: Router | Seats, bind: str, port: int) -> int:
    """Run every seat's router on one port.  A seat starts without its routine key or its
    registry address, since both come from Infisical, which may be down at start.  Without the
    key it refuses its requests with 503;  without the registry it picks nothing (each wake is a
    bounded `registry-not-configured` retry);  either way /health answers 503 with the reason, so
    the router never looks healthy while it cannot work, and it takes the values once a refresh
    reads them.  The port is bound before anything is replayed, so a second instance stops there
    and touches no pending wake.  SIGTERM closes the port, stops new work and waits for running
    deliveries, in every seat at once."""
    seats = target if isinstance(target, Seats) else Seats([target])
    try:
        server = ThreadingHTTPServer((bind, port), make_handler(seats))
    except OSError as exc:
        raise SystemExit(
            "wake-router: cannot bind %s:%d (%s)" % (bind, port, type(exc).__name__)
        ) from None
    server.daemon_threads = True

    def on_term(_signum: int, _frame: object) -> None:
        # shutdown() waits for serve_forever, which is this same thread, so it needs its own.
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        seats.start()
        served = ", ".join(sorted(seats.routers)) or "none"
        unserved = {**seats.down, **{seat: NOT_INSTALLED for seat in seats.not_installed}}
        down = ", ".join("%s (%s)" % item for item in sorted(unserved.items())) or "none"
        print(
            "wake-router listening on %s:%d for seats %s;  not served:  %s"
            % (bind, port, served, down),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        seats.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0
