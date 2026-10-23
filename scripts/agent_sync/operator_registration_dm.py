"""The Operator's registration-question DM (AFC-412 follow-up).

One direct message from the Operator bot to the owner carrying the one location question for
an unlettered session, on the same sending rules as the candidate list
(operator_candidates.py, owner rulings Oct 21-22 2026):

  - it goes out as the pinned Operator bot, never as a seat's own bot, and the authenticated
    identity is checked before anything is sent;
  - the recipient is the pinned owner (or a `[operator] dm_allow` user), resolved against the
    realm, never a raw id from a trigger;
  - the body is the question text the queue produced - ids and labels, never a message body,
    a title or a key - and it is secret-scanned before the send;
  - the answer is a dict, never an exception into the wake path.

The dedupe lives in the queue, not here:  the daemon calls this only for an entry with no
`notified_at` stamp and stamps it after a sent outcome, so a restart cannot re-ask and a
failed send is retried on the next wake.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import operator_candidates as OC
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
    recipient: Any,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
    owner_user_id: int = 0,
    realm: str | None = None,
    now: float | None = None,
    client: Any = None,
    credentials: Any = None,
    fetch: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Send the one location question to the owner, as the Operator bot.

    `question` is the queue's own text (operator_registration.question_for);  `entry_id`
    rides along so the log line can name the entry without the body.  `recipient` is the
    owner's user id.  Returns a dict with `status` sent/refused and `reason`;  on a sent
    outcome the caller stamps the entry with operator_registration.mark_notified.
    """
    environment = dict(env if env is not None else os.environ)
    text = str(question or "").strip()
    if not text:
        return _outcome("refused", "no question to send")
    # The question names a chat:  a mention in it would notify somebody in a DM that is only
    # a question, and a secret-shaped string would put a secret in front of the owner.
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

    try:
        raw_members = client.get("users")
    except Exception as exc:
        return _outcome("refused", "the recipient could not be looked up: %s" % type(exc).__name__)
    if not isinstance(raw_members, Mapping) or not isinstance(raw_members.get("members"), list):
        return _outcome("refused", "the Zulip users response is malformed")
    recipient_id, why = OC.resolve_recipient(raw_members["members"], recipient)
    if recipient_id is None:
        return _outcome("refused", "the recipient is not resolvable: %s" % why)
    if recipient_id == me.get("user_id"):
        return _outcome("refused", "the recipient is the Operator itself")

    hidden = list(secretscan.basic_forms(str(creds.email), str(creds.key)))
    if secretscan.scan(body, hidden):
        return _outcome("refused", "the question was withheld: it matched the secret scan")

    try:
        result = client.post("messages", {"type": "direct", "to": [recipient_id], "content": body})
    except Exception as exc:
        # The outcome is unknown:  the DM may already sit in the owner's inbox, so the
        # caller must not stamp the entry - the next wake retries, and the queue's one
        # pending question per session is what keeps a duplicate from piling up.
        return _outcome("refused", "the send did not complete: %s" % type(exc).__name__)
    message_id = result.get("id") if isinstance(result, Mapping) else None
    return _outcome("sent", "the location question went to the owner", message_id=message_id)

