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

The dedupe lives in the queue, not here:  the daemon calls this only for an entry with no
`notified_at` stamp and stamps it after a sent outcome, so a restart cannot re-ask and a
failed send is retried on the next wake.  The answer intake is the daemon's
_maybe_registration_answer, which accepts only an answer posted in the same channel/topic.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import secretscan
from . import service_identity as SI
from . import wakes as W
from . import zulip as Z


def _outcome(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    out = {"status": status, "reason": reason}
    out.update(extra)
    return out


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
) -> dict[str, Any]:
    """Post the one location question in the chat's channel/topic, as the Operator bot.

    `question` is the queue's own text (operator_registration.question_for);  `entry_id`
    rides along so the log line can name the entry without the body.  `channel`/`topic`
    are the chat's return route from its own lease.  Returns a dict with `status`
    sent/refused and `reason`;  on a sent outcome the caller stamps the entry with
    operator_registration.mark_notified.
    """
    environment = dict(env if env is not None else os.environ)
    text = str(question or "").strip()
    if not text:
        return _outcome("refused", "no question to send")
    channel = str(channel or "").strip()
    topic = str(topic or "").strip()
    if not channel or not topic:
        return _outcome("refused", "the chat has no known channel/topic to ask in")
    # The question names a chat:  a mention in it would notify somebody in a topic that is
    # only a question, and a secret-shaped string would put a secret in front of the chat.
    body = W.neutralize_mentions(text)

    creds = credentials
    if creds is None:
        try:
            creds = SI.operator_credentials(environment, home=home, fetch=fetch)
        except Z.CredentialError as exc:
            return _outcome("refused", "the Operator credential is missing: %s" % exc)
        except Exception as exc:
            return _outcome(
                "refused", "the Operator credential could not be read: %s" % type(exc).__name__
            )
    if client is None:
        try:
            client = Z.ZulipClient(creds, realm or creds.site)
        except Exception as exc:
            return _outcome(
                "refused", "the Operator client could not be built: %s" % type(exc).__name__
            )

    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool) or owner_user_id <= 0:
        return _outcome("refused", "the owner is not pinned, so the Operator bot cannot be checked")
    try:
        me = client.get("users/me")
    except Exception as exc:
        return _outcome(
            "refused", "the Operator identity could not be confirmed: %s" % type(exc).__name__
        )
    try:
        is_operator = SI.operator_bot(me, environment, int(owner_user_id))
    except Exception as exc:
        return _outcome(
            "refused", "the Operator identity is not configured: %s" % type(exc).__name__
        )
    if not is_operator:
        return _outcome("refused", "the authenticated bot is not the pinned Operator")

    hidden = list(secretscan.basic_forms(str(creds.email), str(creds.key)))
    if secretscan.scan(body, hidden):
        return _outcome("refused", "the question was withheld: it matched the secret scan")

    try:
        result = client.post(
            "messages", {"type": "stream", "to": channel, "topic": topic, "content": body}
        )
    except Exception as exc:
        # The outcome is unknown:  the post may already sit in the topic, so the caller
        # must not stamp the entry - the next wake retries, and the queue's one pending
        # question per session is what keeps a duplicate from piling up.
        return _outcome("refused", "the send did not complete: %s" % type(exc).__name__)
    message_id = result.get("id") if isinstance(result, Mapping) else None
    return _outcome("sent", "the location question went to the chat's topic", message_id=message_id)
