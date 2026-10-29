"""The Operator's status message:  what became of a wake, told to the sender.

Owner ruling, Thu, Oct 29 2026, about 3:35 AM:  the Operator, when it comments at all, speaks as
the Operator and not as a seat, and it mostly sends a direct message to the sender.  That message
goes one to one, from the Operator bot to the sender, with the owner left out, because the Operator
is a deterministic process and what it says is not about the project or any detail the owner needs
to see.  The owner's words:  "DM one to one since from bot to another bot but since deterministic
bot not something about like the project or details I want to be able to see".

Two things follow, and this module enforces both:

  - the text is built here from a fixed vocabulary.  A caller names a message id, a seat tag, an
    outcome and a reason from the lists below, and nothing else reaches the sender:  no topic, no
    channel, no quoted message and no model-written sentence.  A value outside the lists is a
    refusal, so a free-text field cannot be added by accident;
  - the send goes through `ZulipClient.operator_status_dm`, the one send that skips the
    owner-in-the-conversation guard (AFC-469).  `ZulipClient.request` enforces the exemption on the
    exact shape of the call:  a client verified as the pinned Operator bot, one recipient, a status
    tag and no mention.

The Operator's clarifying question is not here.  Its reply leg is not built:  the sender's answer
would be a direct message to the Operator, which the owner guard refuses for a bot, and nothing
reads such an answer yet (clarify.py is not wired in).  The candidate list (operator_candidates.py)
is a different message:  it names session ids and titles, so it stays a conversation the owner is in.

The answer is a dict, never an exception, like the other Operator senders.  `status` is `sent`,
`refused` or `duplicate`.  A wake outcome is reported to a sender once:  the same outcome for the
same message to the same recipient is not sent again for a day, and a write whose outcome is
unknown is never replayed.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import live as L
from . import operator_candidates as OC
from . import secretscan
from . import service_identity as SI
from . import zulip as Z

KIND_STATUS = "status"
KINDS = (KIND_STATUS,)

#: What a status message can say happened to a wake.  The sentence is fixed;  only the seat tag is
#: filled in.  "Operator" speaks about itself in the third person, because an automated process
#: must not read as a person.
OUTCOMES: Mapping[str, str] = {
    "woken": "{seat} was woken.",
    "alerted": "{seat} was alerted and will see it at its next check.",
    "queued": "It is queued for the next session of {seat}.",
    "held": "It is held back, and a catch-up wake for {seat} will cover it.",
    "dropped": "It was not delivered to {seat}.",
    "paused": "Waking {seat} is paused, so nothing was delivered.",
}

#: Why a wake was held back or dropped.  Fixed sentences again, so a reason cannot carry a detail.
REASONS: Mapping[str, str] = {
    "budget": "The seat is over its wake budget.",
    "quota": "The seat's provider quota is used up.",
    "loop_guard": "The loop guard stopped a repeat wake.",
    "stale": "The message was too old to wake anyone.",
    "paused": "Waking is paused.",
    "no_session": "No live session could take it.",
    "unreachable": "The seat could not be reached.",
}

#: The outcomes that may carry a reason.  A woken seat has nothing to explain, and `paused` says
#: why in its own sentence.
REASON_OUTCOMES = frozenset({"held", "dropped"})

#: A seat tag:  `MM`, `CLAUDE`, `GB-ORACLE`.
SEAT_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")

#: The Zulip bot types that can take a status message:  generic (1) and outgoing webhook (3).  A
#: fleet seat bot is either, and the router treats both as fleet bots.  An incoming webhook (2) and
#: an embedded bot (4) have no inbox to read it from.
RECIPIENT_BOT_TYPES = (1, 3)

STATE_DIR = "OPERATOR"
STATE_FILE = "operator-status-dms.json"
#: How long a status is remembered.  The key carries the message id, which never repeats, so one
#: wake outcome for one message goes to one recipient once;  a later, different outcome for the
#: same message (queued, then woken) is a different key and is reported.
DEDUPE_WINDOW = 24 * 3600.0

#: Two literal ASCII spaces between sentences:  the Zulip client turns them into the owner's
#: sentence gap on the way out.
GAP = "  "


# --------------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------------


def _seat(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not SEAT_RE.fullmatch(text):
        raise ValueError("not a seat tag")
    return text


def _message_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("not a message id")
    return value


def build_text(
    kind: str,
    *,
    reply_to: int,
    seat: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
) -> str:
    """The text of one status message, from the fixed vocabulary alone.

    `kind` is `status`.  `reply_to` is the Zulip id of the sender's own message, `seat` the seat tag
    it named, `outcome` what became of the wake, and `reason` (only for a held or dropped wake) why.
    Any value outside the lists raises ValueError."""
    if kind not in KINDS:
        raise ValueError("not an Operator status kind")
    mid = _message_id(reply_to)
    who = _seat(seat)
    if outcome not in OUTCOMES:
        raise ValueError("not a status outcome")
    sentences = ["Your message tagged %s." % who, OUTCOMES[outcome].format(seat=who)]
    if reason is not None:
        if outcome not in REASON_OUTCOMES:
            raise ValueError("this outcome carries no reason")
        if reason not in REASONS:
            raise ValueError("not a status reason")
        sentences.append(REASONS[reason])
    return "%s re=%d\n%s" % (Z.OPERATOR_STATUS_TAGS[0], mid, GAP.join(sentences))


# --------------------------------------------------------------------------------------------
# The connection
# --------------------------------------------------------------------------------------------


def _outcome(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "reason": reason}
    out.update(extra)
    return out


def connect_operator(
    *,
    env: Mapping[str, str],
    owner_user_id: int,
    home: str | os.PathLike[str] | None = None,
    realm: str | None = None,
    client: Any = None,
    credentials: Any = None,
    fetch: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """A Zulip client that has been verified as the pinned Operator bot, or the reason it could not
    be.  Never raises.

    The credential comes from Fleet-OPS `/zulip_operator` (service_identity), the client's
    `users/me` has to be the pinned Operator bot (id, address, bot flag and the owner as its
    owner), and only then is the client marked with the Operator's id and address.  The asking
    seat's own credential is never used here.  A client passed in is marked in place, so a caller
    that shares one should pass a client of its own."""
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id <= 0:
        return _outcome("refused", "the owner is not pinned, so the Operator bot cannot be checked")
    creds = credentials
    if creds is None:
        try:
            creds = SI.operator_credentials(
                env, home=Path(home) if home is not None else None, fetch=fetch
            )
        except Z.CredentialError as exc:
            return _outcome("refused", "the Operator credential is missing: %s" % exc)
        except Exception as exc:
            return _outcome(
                "refused", "the Operator credential could not be read: %s" % type(exc).__name__
            )
    if client is None:
        try:
            client = Z.ZulipClient(creds, realm or creds.site, owner_user_id=owner_user_id)
        except Exception as exc:
            return _outcome(
                "refused", "the Operator client could not be built: %s" % type(exc).__name__
            )
    try:
        me = client.get("users/me")
    except Z.AgentSyncError as exc:
        status = getattr(exc, "status", getattr(exc, "status_code", None))
        if status in (401, 403):
            return _outcome(
                "refused",
                "the Operator credential was rejected.  Preview: %s"
                % OC._masked(getattr(creds, "key", "")),
                code="credential_rejected",
            )
        return _outcome("refused", "the Operator identity could not be confirmed: %s" % exc)
    except Exception as exc:
        return _outcome(
            "refused", "the Operator identity could not be confirmed: %s" % type(exc).__name__
        )
    try:
        is_operator = SI.operator_bot(me, dict(env), owner_user_id)
    except Z.CredentialError as exc:
        return _outcome("refused", "the Operator identity is not configured: %s" % exc)
    except Exception as exc:
        return _outcome(
            "refused", "the Operator identity is not configured: %s" % type(exc).__name__
        )
    if not is_operator:
        return _outcome("refused", "the authenticated bot is not the pinned Operator")
    mark = getattr(client, "mark_operator_verified", None)
    if not callable(mark):
        return _outcome("refused", "the client cannot send an Operator status message")
    try:
        mark(me["user_id"], me["email"])
    except Exception as exc:
        return _outcome(
            "refused", "the Operator client could not be marked: %s" % type(exc).__name__
        )
    return _outcome("ok", "connected as the pinned Operator bot", client=client, creds=creds, me=me)


# --------------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------------


def state_path(env: Mapping[str, str]) -> str:
    """The Operator's own dedupe file:  `<state root>/OPERATOR/operator-status-dms.json`."""
    return os.path.join(L.state_root(env), STATE_DIR, STATE_FILE)


def _fingerprint(
    recipient_id: int, kind: str, reply_to: int, seat: str, outcome: str, reason: str | None
) -> str:
    material = "\n".join([str(recipient_id), kind, str(reply_to), seat, outcome, reason or ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _answer_for_stored(
    stored: Mapping[str, Any], since: float, recipient_id: int, text: str
) -> dict[str, Any]:
    """The answer for a key the ledger already holds, whatever its state."""
    state = str(stored.get("state") or "")
    if state == OC.DELIVERY_OK:
        return _outcome(
            "sent",
            "this message already went to the recipient %d seconds ago" % int(since),
            delivery_status=OC.DELIVERY_OK,
            recipient=stored.get("recipient", recipient_id),
            message_id=stored.get("message_id"),
            text=text,
        )
    if state == OC.DELIVERY_UNKNOWN:
        return _outcome(
            "refused",
            "an identical message to this recipient has an unknown delivery outcome and is not "
            "replayed",
            delivery_status=OC.DELIVERY_UNKNOWN,
            recipient=recipient_id,
            text=text,
        )
    return _outcome(
        "duplicate",
        "an identical message to this recipient %d seconds ago has an unsettled delivery outcome "
        "and is not replayed" % int(since),
        delivery_status=OC.DELIVERY_UNKNOWN,
        recipient=recipient_id,
        text=text,
    )


def _peek(path: str, key: str, at: float, window: float) -> tuple[float, dict[str, Any]] | None:
    """The stored row for a key and how long ago it was written, without writing anything.

    Read before any network call, so a repeat costs no Zulip request.  The reservation that makes
    a send safe is still taken later, under the file lock."""
    row = OC._prune(OC._sends(path), at, window).get(key)
    if not isinstance(row, Mapping):
        return None
    when = row.get("at")
    since = max(0.0, at - float(when)) if isinstance(when, (int, float)) else 0.0
    return since, dict(row)


def _recipient_problem(members: Sequence[Mapping[str, Any]], recipient_id: int) -> str:
    """Why this user id cannot take a status message, or "" when it can."""
    found = [m for m in members if m.get("user_id") == recipient_id]
    if not found:
        return "no user has the id %d" % recipient_id
    user = found[0]
    if not user.get("is_active", True):
        return "the recipient is a deactivated user"
    if user.get("is_bot") and user.get("bot_type", OC.GENERIC_BOT) not in RECIPIENT_BOT_TYPES:
        return "the recipient is a bot with no inbox to read it from"
    return ""


def send_status_dm(
    *,
    recipient_user_id: int,
    reply_to: int,
    seat: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
    kind: str = KIND_STATUS,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    owner_user_id: int = 0,
    realm: str | None = None,
    now: float | None = None,
    client: Any = None,
    credentials: Any = None,
    fetch: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
    window: float = DEDUPE_WINDOW,
) -> dict[str, Any]:
    """Send one Operator status message to the sender of a tagged message.

    `recipient_user_id` is the Zulip user id of that sender, a person or a bot, and only an
    integer id is accepted.  The message goes out one to one as the Operator bot (see the module
    note for why the owner is not in it).  The wake path may call this and never needs a try
    block."""
    try:
        return _send_status_dm(
            kind=kind,
            recipient_user_id=recipient_user_id,
            reply_to=reply_to,
            seat=seat,
            outcome=outcome,
            reason=reason,
            env=env,
            home=home,
            owner_user_id=owner_user_id,
            realm=realm,
            now=now,
            client=client,
            credentials=credentials,
            fetch=fetch,
            window=window,
        )
    except Exception as exc:  # a wake path must never see an exception from here
        return _outcome("refused", "the status message could not be sent: %s" % type(exc).__name__)


def _send_status_dm(
    *,
    kind: str,
    recipient_user_id: int,
    reply_to: int,
    seat: str | None,
    outcome: str | None,
    reason: str | None,
    env: Mapping[str, str] | None,
    home: str | os.PathLike[str] | None,
    owner_user_id: int,
    realm: str | None,
    now: float | None,
    client: Any,
    credentials: Any,
    fetch: Callable[[Sequence[str]], Mapping[str, str]] | None,
    window: float,
) -> dict[str, Any]:
    environment = dict(env if env is not None else os.environ)
    at = time.time() if now is None else float(now)
    try:
        text = build_text(kind, reply_to=reply_to, seat=seat, outcome=outcome, reason=reason)
    except ValueError as exc:
        return _outcome("refused", "the status message was refused: %s" % exc, code="vocabulary")
    if (
        isinstance(recipient_user_id, bool)
        or not isinstance(recipient_user_id, int)
        or recipient_user_id <= 0
    ):
        return _outcome(
            "refused",
            "the recipient must be a Zulip user id, not a name or an address",
            code="recipient",
        )
    recipient_id = recipient_user_id

    path = state_path(environment)
    key = _fingerprint(recipient_id, kind, reply_to, _seat(seat), str(outcome), reason)
    seen = _peek(path, key, at, window)
    if seen is not None:
        return _answer_for_stored(seen[1], seen[0], recipient_id, text)

    link = connect_operator(
        env=environment,
        owner_user_id=owner_user_id,
        home=home,
        realm=realm,
        client=client,
        credentials=credentials,
        fetch=fetch,
    )
    if link["status"] != "ok":
        return link
    operator, creds, me = link["client"], link["creds"], link["me"]

    try:
        raw_members = operator.get("users")
    except Z.AgentSyncError as exc:
        return _outcome("refused", "the recipient could not be looked up: %s" % exc)
    except Exception as exc:
        return _outcome("refused", "the recipient could not be looked up: %s" % type(exc).__name__)
    if not isinstance(raw_members, Mapping) or not isinstance(raw_members.get("members"), list):
        return _outcome("refused", "the Zulip users response is malformed")
    members = raw_members["members"]
    if any(not OC._member_ok(member) for member in members):
        return _outcome("refused", "a Zulip member record is malformed")
    if recipient_id == me.get("user_id"):
        return _outcome("refused", "the recipient is the Operator itself")
    problem = _recipient_problem(members, recipient_id)
    if problem:
        return _outcome("refused", "the recipient is not usable: %s" % problem)

    hidden = list(secretscan.basic_forms(str(creds.email), str(creds.key)))
    if secretscan.scan(text, hidden):
        # The scan result can carry the suspected secret, so only a stable code is returned.
        return _outcome(
            "refused",
            "the status message was withheld: it matched the secret scan",
            delivery_status=OC.DELIVERY_REFUSED,
            code="secret",
        )

    allowed, since, stored = OC._reserve(path, key, at, window, recipient_id)
    if not allowed:
        return _answer_for_stored(stored, since, recipient_id, text)

    try:
        result = operator.operator_status_dm(recipient_id, text)
    except Z.NetworkError as exc:
        # A timeout or a post-send disconnect leaves the outcome unknown:  the message may already
        # exist, so the key is kept and replay is refused.  A failure that provably never reached
        # Zulip gives the key back.
        if getattr(exc, "maybe_sent", False):
            OC._record(path, key, at, window, recipient_id, None, OC.DELIVERY_UNKNOWN)
            return _outcome(
                "refused",
                "the status message delivery is unknown: %s" % exc,
                delivery_status=OC.DELIVERY_UNKNOWN,
                recipient=recipient_id,
                text=text,
            )
        OC._release(path, key, at, window)
        return _outcome(
            "refused",
            "the status message was not sent and may be retried: %s" % exc,
            delivery_status=OC.DELIVERY_RETRYABLE,
            recipient=recipient_id,
            text=text,
        )
    except Z.DmGuardRefused as exc:
        # Our own guard refused before anything left, so the message certainly did not go out.
        OC._release(path, key, at, window)
        return _outcome(
            "refused",
            "the status message was refused by the client guard (%s)" % exc.code,
            delivery_status=OC.DELIVERY_REFUSED,
            code=exc.code,
            recipient=recipient_id,
        )
    except Z.AgentSyncError as exc:
        status = getattr(exc, "status", getattr(exc, "status_code", None))
        if isinstance(status, int) and not isinstance(status, bool):
            if status in (401, 403):
                OC._release(path, key, at, window)
                return _outcome(
                    "refused",
                    "the Operator credential was rejected.  Preview: %s"
                    % OC._masked(getattr(creds, "key", "")),
                    delivery_status=OC.DELIVERY_REFUSED,
                    code="credential_rejected",
                    recipient=recipient_id,
                )
            if status == 429:
                OC._release(path, key, at, window)
                return _outcome(
                    "refused",
                    "Zulip asked the Operator to slow down, so the message was not sent",
                    delivery_status=OC.DELIVERY_RETRYABLE,
                    recipient=recipient_id,
                    text=text,
                )
            if 400 <= status < 500 and status != 408:
                OC._release(path, key, at, window)
                return _outcome(
                    "refused",
                    "the status message could not be sent: %s" % exc,
                    delivery_status=OC.DELIVERY_REFUSED,
                    recipient=recipient_id,
                )
        # A gateway or server error, a request timeout, or an answer with no status:  the request
        # may have reached Zulip and been stored, so the key is kept and the outcome is unknown.
        OC._record(path, key, at, window, recipient_id, None, OC.DELIVERY_UNKNOWN)
        return _outcome(
            "refused",
            "the status message delivery is unknown: %s" % exc,
            delivery_status=OC.DELIVERY_UNKNOWN,
            recipient=recipient_id,
            text=text,
        )
    except Exception as exc:
        # The POST returned and something after it failed, so the message may be in the inbox:
        # the key is kept and the outcome is unknown, exactly like a post-send disconnect.
        OC._record(path, key, at, window, recipient_id, None, OC.DELIVERY_UNKNOWN)
        return _outcome(
            "refused",
            "the status message delivery is unknown: %s" % type(exc).__name__,
            delivery_status=OC.DELIVERY_UNKNOWN,
            recipient=recipient_id,
            text=text,
        )
    message_id = result.get("id") if isinstance(result, Mapping) else None
    message_id = (
        int(message_id)
        if isinstance(message_id, int) and not isinstance(message_id, bool)
        else None
    )
    try:
        OC._record(path, key, at, window, recipient_id, message_id, OC.DELIVERY_OK)
    except Exception:
        # Zulip accepted the message, so the delivery happened.  The state write is what failed,
        # and the key is left as the reservation wrote it (pending), so the next call is refused
        # as a duplicate and the message is not sent twice.
        pass
    return _outcome(
        "sent",
        "the status message went to the sender one to one as the Operator bot",
        delivery_status=OC.DELIVERY_OK,
        recipient=recipient_id,
        message_id=message_id,
        text=text,
    )
