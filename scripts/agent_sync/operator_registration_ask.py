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
list (operator_candidates.py):  a reservation goes down before the post, an `unknown`
outcome (the post may already sit in the topic) blocks any replay inside the window, only
a definite refusal or a provably-unsent failure releases the key, and a replay after a
landed post returns the cached message id instead of posting a second copy.  The answer
intake is the daemon's _maybe_registration_answer, which accepts only an answer posted in
the same channel/topic.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

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
    delivery is never replayed inside the window:  the question may already sit in the
    topic, and a duplicate question is not automatically benign.
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
            return _outcome(
                "refused",
                "the Operator credential was rejected.  Preview: %s"
                % OC._masked(getattr(creds, "key", "")),
                delivery_status=OC.DELIVERY_REFUSED,
                code="credential_rejected",
            )
        return _outcome(
            "refused",
            "the Operator identity could not be confirmed: %s" % exc,
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

    path = _ledger_path(environment)
    key = _fingerprint(channel, topic, body)
    allowed, since, stored = OC._reserve(path, key, at, window, 0)
    if not allowed:
        state = str(stored.get("state") or "")
        if state == OC.DELIVERY_OK:
            # A successful send is cached:  a retry returns the same answer with the same
            # message id instead of posting the question a second time.
            return _outcome(
                "sent",
                "the location question already went to the chat's topic %d seconds ago"
                % int(since),
                delivery_status=OC.DELIVERY_OK,
                message_id=stored.get("message_id"),
            )
        # `unknown` (or an unsettled pending):  the question may already sit in the
        # topic, so it is not replayed inside the window.
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
                "the location question delivery is unknown: %s" % exc,
                delivery_status=OC.DELIVERY_UNKNOWN,
            )
        OC._release(path, key, at, window)
        return _outcome(
            "refused",
            "the location question was not sent and may be retried: %s" % exc,
            delivery_status=OC.DELIVERY_RETRYABLE,
        )
    except Z.AgentSyncError as exc:
        # Zulip answered and refused:  the question definitely did not go out, so the
        # key is released and the refusal is terminal.
        status = getattr(exc, "status", getattr(exc, "status_code", None))
        OC._release(path, key, at, window)
        if status in (401, 403):
            return _outcome(
                "refused",
                "the Operator credential was rejected.  Preview: %s"
                % OC._masked(getattr(creds, "key", "")),
                delivery_status=OC.DELIVERY_REFUSED,
                code="credential_rejected",
            )
        return _outcome(
            "refused",
            "the location question could not be sent: %s" % exc,
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
