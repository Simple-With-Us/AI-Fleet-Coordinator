"""The Operator's registration question, posted in the registering chat's own topic (AFC-412).

Owner direction, Oct 23 2026:  when the Operator receives a session/chat id, it asks the
chat's location before the session letter is assigned - and the registering chat is who
receives the question and answers it.  The question therefore goes to the chat's own
Zulip channel/topic (the return route its lease names), never to the owner by DM.  Same
sending rules as the candidate list (operator_candidates.py, owner rulings Oct 21-22 2026):

  - it goes out as the pinned Operator bot, never as a seat's own bot, and the authenticated
    identity is checked before anything is sent;
  - the body is the question text the queue produced - ids and labels, never a message body,
    a title or a key - and it is secret-scanned before the send;
  - the answer is a dict, never an exception into the wake path.

The dedupe is two layers.  The queue stamps an entry `notified_at` after a sent outcome, so
a restart cannot re-ask.  The send itself holds the same delivery ledger as the candidate
list (operator_candidates.py):  a reservation goes down before the post, a landed
question answers from the ledger instead of posting twice, and an `unknown` outcome (the
post may already sit in the topic) is never resent on a timer - it is reconciled against
the topic itself, where the question's unique ref identifies a landed copy.  Only a
definite refusal, a provably-unsent failure, or a read-back that proves the question
absent frees the send.  The answer
intake is the daemon's _maybe_registration_answer, which accepts only an answer posted in
the same channel/topic.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import live as L
from . import operator_candidates as OC
from . import secretscan
from . import service_identity as SI
from . import wakes as W
from . import zulip as Z


def _outcome(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    out = {"status": status, "reason": reason}
    out.update(extra)
    return out


def _fingerprint(channel: str, topic: str, body: str) -> str:
    return OC._fingerprint(0, "%s\n%s\n%s" % (channel.casefold(), topic.casefold(), body))


def _ledger_path(environment: Mapping[str, str]) -> str:
    """The delivery ledger, beside the Operator seat's other state."""
    return os.path.join(L.state_root(environment), "OPERATOR", "operator-registration-asks.json")


#: Read-back paging:  newest-first pages of 50, capped at 10 pages.  Absence is only
#: PROVEN when a short page shows the topic's history is exhausted;  hitting the cap
#: leaves the delivery unknown rather than guessing.
READ_BACK_PAGE = 50
READ_BACK_MAX_PAGES = 10
#: One absolute deadline shared by every page of a single read-back:  a slow or hanging
#: topic read must not stall the send path past this, whatever the page count.
READ_BACK_DEADLINE_SECONDS = 60.0


def _question_message_id(
    client: Any, channel: str, topic: str, marker: str, sender_id: int
) -> tuple[int | None, str | None, bool]:
    """(the landed question's id, error kind, proven-absent) for one topic read-back.

    A match is the Operator bot's OWN post carrying the question's unique ref:  an
    answer quoting the ref, or anyone else's message, never counts.  Absence is proven
    only when the topic's history is exhausted;  an unreadable topic or a hit page cap
    is "could not check", never "not there".
    """
    narrow = json.dumps(
        [
            {"operator": "channel", "operand": channel},
            {"operator": "topic", "operand": topic},
        ]
    )
    anchor = "newest"
    deadline = time.monotonic() + READ_BACK_DEADLINE_SECONDS
    for _page in range(READ_BACK_MAX_PAGES):
        if time.monotonic() > deadline:
            return None, "the topic read-back ran out of time", False
        params: dict[str, Any] = {
            "anchor": anchor,
            "num_before": READ_BACK_PAGE,
            "num_after": 0,
            "narrow": narrow,
        }
        if anchor != "newest":
            params["include_anchor"] = "false"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "the topic read-back ran out of time", False
        try:
            # Each page gets only what is left of the shared absolute deadline, so one
            # hung request can never stretch the walk past it.
            result = client.get("messages", params, timeout=remaining)
        except Exception as exc:
            return None, type(exc).__name__, False
        rows = result.get("messages") if isinstance(result, Mapping) else None
        messages = [m for m in (rows or []) if isinstance(m, Mapping)]
        for message in messages:
            if message.get("sender_id") != sender_id:
                continue
            if marker not in str(message.get("content") or ""):
                continue
            message_id = message.get("id")
            if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
                return int(message_id), None, False
            # The ref landed but its id is unusable:  say the read-back failed rather
            # than guessing absence (a repost would duplicate) or paging on a bad cursor.
            return None, "the landed question's message id was malformed", False
        if len(messages) < READ_BACK_PAGE:
            return None, None, True
        # The page cursor is only sound when every row carries a real positive-int id:
        # a bool, a zero or a negative id would page the history wrongly, so a malformed
        # row makes the whole read-back unverifiable rather than silently skipped.
        ids: list[int] = []
        for m in messages:
            mid = m.get("id")
            if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
                return None, "the topic history carried a malformed message id", False
            ids.append(mid)
        anchor = str(min(ids))
    return None, None, False


def send_location_question(
    *,
    question: str,
    entry_id: str,
    channel: str,
    topic: str,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
    owner_user_id: int = 0,
    realm: str | None = None,
    now: float | None = None,
    client: Any = None,
    credentials: Any = None,
    fetch: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
    window: float = OC.DEDUPE_WINDOW,
) -> dict[str, Any]:
    """Post the one location question in the chat's channel/topic, as the Operator bot.

    `question` is the queue's own text (operator_registration.question_for);  `entry_id`
    rides along so the log line can name the entry without the body.  `channel`/`topic`
    are the chat's return route from its own lease.  Returns a dict with `status`
    sent/refused, `reason`, and `delivery_status` (operator_candidates' DELIVERY_*):
    on a sent outcome the caller stamps the entry with mark_notified.  An `unknown`
    delivery is reconciled by reading the topic back for the question's unique ref, never
    resent on a timer:  the question may already sit in the topic, and a duplicate
    question is not automatically benign.
    """
    environment = dict(env if env is not None else os.environ)
    at = time.time() if now is None else float(now)
    text = str(question or "").strip()
    if not text:
        return _outcome("refused", "no question to send", delivery_status=OC.DELIVERY_REFUSED)
    channel = str(channel or "").strip()
    topic = str(topic or "").strip()
    if not channel or not topic:
        return _outcome(
            "refused",
            "the chat has no known channel/topic to ask in",
            delivery_status=OC.DELIVERY_REFUSED,
        )
    # The question names a chat:  a mention in it would notify somebody in a topic that is
    # only a question, and a secret-shaped string would put a secret in front of the chat.
    body = W.neutralize_mentions(text)

    creds = credentials
    if creds is None:
        try:
            creds = SI.operator_credentials(environment, home=home, fetch=fetch)
        except Z.CredentialError as exc:
            return _outcome(
                "refused",
                "the Operator credential is missing: %s" % exc,
                delivery_status=OC.DELIVERY_REFUSED,
            )
        except Exception as exc:
            return _outcome(
                "refused",
                "the Operator credential could not be read: %s" % type(exc).__name__,
                delivery_status=OC.DELIVERY_REFUSED,
            )
    if client is None:
        try:
            client = Z.ZulipClient(creds, realm or creds.site)
        except Exception as exc:
            return _outcome(
                "refused",
                "the Operator client could not be built: %s" % type(exc).__name__,
                delivery_status=OC.DELIVERY_REFUSED,
            )

    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool) or owner_user_id <= 0:
        return _outcome(
            "refused",
            "the owner is not pinned, so the Operator bot cannot be checked",
            delivery_status=OC.DELIVERY_REFUSED,
        )
    try:
        me = client.get("users/me")
    except Z.AgentSyncError as exc:
        # A rejected credential is named by a masked preview only, never by any part of
        # the key a caller could not otherwise see (the candidate list's convention).
        status = getattr(exc, "status", getattr(exc, "status_code", None))
        if status in (401, 403):
            # Never any part of the key:  the credential is named by its identity
            # (the bot email) and the server's code only.
            return _outcome(
                "refused",
                "the Operator credential (%s) was rejected (%s)"
                % (getattr(creds, "email", "?"), status),
                delivery_status=OC.DELIVERY_REFUSED,
                code="credential_rejected",
            )
        return _outcome(
            "refused",
            "the Operator identity could not be confirmed: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_REFUSED,
        )
    except Exception as exc:
        return _outcome(
            "refused",
            "the Operator identity could not be confirmed: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_REFUSED,
        )
    try:
        is_operator = SI.operator_bot(me, environment, int(owner_user_id))
    except Exception as exc:
        return _outcome(
            "refused",
            "the Operator identity is not configured: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_REFUSED,
        )
    if not is_operator:
        return _outcome(
            "refused",
            "the authenticated bot is not the pinned Operator",
            delivery_status=OC.DELIVERY_REFUSED,
        )

    hidden = list(secretscan.basic_forms(str(creds.email), str(creds.key)))
    if secretscan.scan(body, hidden):
        return _outcome(
            "refused",
            "the question was withheld: it matched the secret scan",
            delivery_status=OC.DELIVERY_REFUSED,
            code="secret",
            field="question",
        )
    # The route reaches the same API call:  scan it at this boundary too, even though
    # the queue validated it at persistence - a hand-built caller gets the same refusal.
    for field, value in (("channel", channel), ("topic", topic)):
        if secretscan.scan(value, hidden):
            return _outcome(
                "refused",
                "the question was withheld: the route matched the secret scan",
                delivery_status=OC.DELIVERY_REFUSED,
                code="secret",
                field=field,
            )

    path = _ledger_path(environment)
    key = _fingerprint(channel, topic, body)
    stored = OC._sends(path).get(key)
    state = str((stored or {}).get("state") or "")
    if state == OC.DELIVERY_OK:
        # The question provably landed (the row carries its id):  a repeated call means
        # the caller's stamp was lost, so answer from the ledger instead of posting a
        # second copy.
        return _outcome(
            "sent",
            "the location question already went to the chat's topic",
            delivery_status=OC.DELIVERY_OK,
            message_id=stored.get("message_id"),
        )
    if state in (OC.DELIVERY_PENDING, OC.DELIVERY_UNKNOWN):
        # A window's expiry never proves a post missed:  reconcile against the topic
        # itself.  The question carries a unique ref, so a landed copy is identifiable.
        landed_id, read_error, proven_absent = _question_message_id(
            client, channel, topic, entry_id, me.get("user_id")
        )
        if read_error is not None:
            return _outcome(
                "refused",
                "the location question's delivery is unknown and the topic could not be "
                "checked (%s)" % read_error,
                delivery_status=OC.DELIVERY_UNKNOWN,
            )
        if landed_id is None and not proven_absent:
            return _outcome(
                "refused",
                "the location question's delivery is unknown and the topic's history "
                "was not exhausted",
                delivery_status=OC.DELIVERY_UNKNOWN,
            )
        if landed_id is not None:
            try:
                OC._record(path, key, at, window, 0, landed_id, OC.DELIVERY_OK)
            except Exception:
                pass
            return _outcome(
                "sent",
                "the location question was already in the chat's topic",
                delivery_status=OC.DELIVERY_OK,
                message_id=landed_id,
            )
        # Provably absent from the topic:  safe to post.  The residual race is explicit:
        # the original post could land between this check and the retry, which is a
        # duplicate question post - never a duplicate registration (the queue keeps one
        # pending entry, and only the first answer registers).
        OC._release(path, key, at, window)
    allowed, _since, stored = OC._reserve(path, key, at, window, 0)
    if not allowed:
        # Settled concurrently between the check and the reservation:  do not double-post.
        return _outcome(
            "refused",
            "an identical location question to this topic has an unsettled delivery "
            "outcome and is not replayed",
            delivery_status=OC.DELIVERY_UNKNOWN,
        )

    try:
        result = client.post(
            "messages", {"type": "stream", "to": channel, "topic": topic, "content": body}
        )
    except Z.NetworkError as exc:
        # A timeout or a post-send disconnect leaves the outcome unknown:  the question
        # may already sit in the topic, so the key is kept and replay is refused.  A
        # failure that provably never reached Zulip is retryable and gives the key back.
        if getattr(exc, "maybe_sent", False):
            OC._record(path, key, at, window, 0, None, OC.DELIVERY_UNKNOWN)
            return _outcome(
                "refused",
                "the location question delivery is unknown: %s" % type(exc).__name__,
                delivery_status=OC.DELIVERY_UNKNOWN,
            )
        OC._release(path, key, at, window)
        return _outcome(
            "refused",
            "the location question was not sent and may be retried: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_RETRYABLE,
        )
    except Z.AgentSyncError as exc:
        # Zulip answered.  What the answer means for a later pass differs by status:
        # - 429 rate limit:  the post was rejected before processing (the client already
        #   spent its bounded in-request retries), so it definitely did not go out - the
        #   key is released and a later pass may retry.
        # - 408 or 5xx:  the server failed mid-handling, so the question may already sit
        #   in the topic - the key is KEPT, the outcome is unknown, and the read-back
        #   reconciliation decides any resend.
        # - 401/403:  the credential was rejected - terminal until the credential changes.
        # - Any other definite 4xx/odd answer:  the post was rejected - terminal.
        status = getattr(exc, "status", getattr(exc, "status_code", None))
        if isinstance(status, int) and status == 429:
            OC._release(path, key, at, window)
            return _outcome(
                "refused",
                "the location question was rate-limited and may be retried: %s"
                % type(exc).__name__,
                delivery_status=OC.DELIVERY_RETRYABLE,
            )
        if isinstance(status, int) and (status == 408 or 500 <= status < 600):
            OC._record(path, key, at, window, 0, None, OC.DELIVERY_UNKNOWN)
            return _outcome(
                "refused",
                "the location question delivery is unknown (%s): %s"
                % (status, type(exc).__name__),
                delivery_status=OC.DELIVERY_UNKNOWN,
            )
        if not isinstance(status, int) or isinstance(status, bool):
            # No HTTP status at all:  what the server did with the post is indeterminate,
            # so the question may already sit in the topic - keep the key, say unknown,
            # and let the read-back reconciliation decide any resend.
            OC._record(path, key, at, window, 0, None, OC.DELIVERY_UNKNOWN)
            return _outcome(
                "refused",
                "the location question delivery is unknown: %s" % type(exc).__name__,
                delivery_status=OC.DELIVERY_UNKNOWN,
            )
        OC._release(path, key, at, window)
        if status in (401, 403):
            return _outcome(
                "refused",
                "the Operator credential (%s) was rejected (%s)"
                % (getattr(creds, "email", "?"), status),
                delivery_status=OC.DELIVERY_REFUSED,
                code="credential_rejected",
            )
        return _outcome(
            "refused",
            "the location question could not be sent: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_REFUSED,
        )
    except Exception as exc:
        # The POST returned and something after it failed:  the question may already sit
        # in the topic, so the key is KEPT and the outcome is unknown.  Releasing it
        # here would let the next pass post a second copy of a question that did land.
        OC._record(path, key, at, window, 0, None, OC.DELIVERY_UNKNOWN)
        return _outcome(
            "refused",
            "the location question delivery is unknown: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_UNKNOWN,
        )
    message_id = result.get("id") if isinstance(result, Mapping) else None
    message_id = (
        int(message_id)
        if isinstance(message_id, int) and not isinstance(message_id, bool)
        else None
    )
    try:
        OC._record(path, key, at, window, 0, message_id, OC.DELIVERY_OK)
    except Exception:
        # Zulip accepted the question and it carries an id, so the delivery happened.
        # The state write is what failed, and the reservation stays, so the next pass is
        # refused as a duplicate rather than posting a second copy.
        pass
    return _outcome(
        "sent",
        "the location question went to the chat's topic",
        delivery_status=OC.DELIVERY_OK,
        message_id=message_id,
    )
