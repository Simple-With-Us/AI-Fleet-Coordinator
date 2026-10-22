"""The Operator's candidate list:  the one direct message that tells the sender which sessions a
tagged address could mean.

A tagged address is a platform mention plus a session letter ("@**MiniMax** Z").  One channel topic
can hold several seats and several sessions, so a letter can be ambiguous.  The seat that was
addressed may ask the Operator which sessions the letter could mean; the Operator answers with a
direct message to the sender, from the Operator bot, so the seat's own thread stays clean (owner
ruling, Oct 21 2026:  the letter is for a tagged address, a DM is plain replies, and the
disambiguation ask never blocks).

Three rules shape this module:

  - the Operator bot sends it, never the seat's own bot:  the seat must not look like it is
    answering the owner out of turn, and the seat's own credentials never leave its lane;
  - it refuses rather than guesses:  an unresolvable recipient, a missing Operator credential or a
    bot that is not the pinned Operator is a clear outcome, never an exception into the wake path;
  - it does not repeat itself:  an identical candidate list to the same recipient inside a short
    window is refused, so a seat that asks twice does not spam the sender.

The dedupe state lives in the asking seat's own state directory
(`~/.agent-sync/<SEAT>/operator-candidates.json`), because the ask belongs to that seat, not to the
Operator.

The caller is the Operator's listener:  it holds the registry, the candidate sessions and the
sender's user id, and it calls `send_candidate_list` once per ask.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import identity as I
from . import live as L
from . import secretscan
from . import service_identity as SI
from . import wakes as W
from . import zulip as Z

STATE_FILE = "operator-candidates.json"
#: How long an identical candidate list to the same recipient is refused.  Long enough that a seat
#: which asks twice in one thread does not spam the sender, short enough that a later ask in the
#: same thread still gets an answer.
DEDUPE_WINDOW = 900.0
#: The list is a shortlist, not a dump:  a topic with many sessions still names a usable set.
MAX_CANDIDATES = 24
#: Zulip's generic bot type.  A webhook or integration bot cannot take a direct message.
GENERIC_BOT = 1
#: Two literal ASCII spaces between the sentences of a plain-text body.
GAP = "  "


# --------------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------------


def _one_line(text: Any, limit: int) -> str:
    """One line of printable text:  control characters dropped, whitespace collapsed, cut to a
    limit.  A title is a person's words, so it never gets to carry a line break or an escape."""
    cleaned = "".join(ch for ch in str(text or "") if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def candidate_view(row: Mapping[str, Any]) -> dict[str, str]:
    """The four fields a candidate carries, whatever the caller's row calls them:  the letter, the
    seat, the native session id and the title.  The registry names the letter `label`; a caller
    that already renamed it may say `letter`."""
    if not isinstance(row, Mapping):
        return {"letter": "", "seat": "", "session_id": "", "title": ""}
    return {
        "letter": _one_line(row.get("letter") or row.get("label"), 8),
        "seat": _one_line(row.get("seat"), 32),
        "session_id": _one_line(row.get("native_session_id") or row.get("session_id"), 64),
        "title": _one_line(row.get("title"), 120),
    }


def _candidate_sentence(view: Mapping[str, str]) -> str:
    """One sentence for one candidate:  its letter, its seat and its title, with the native
    session id so the sender can name the session and not only the letter."""
    letter = view.get("letter") or "?"
    seat = view.get("seat") or "an unnamed seat"
    title = view.get("title")
    session_id = view.get("session_id")
    if title:
        described = 'the %s session "%s"' % (seat, title)
    else:
        described = "the %s session" % seat
    if session_id:
        described += " (%s)" % session_id
    return "%s is %s." % (letter, described)


def render_body(
    *,
    seat: str,
    platform: str,
    letters: Sequence[str] = (),
    candidates: Sequence[Mapping[str, Any]] = (),
    default_letter: str | None = None,
    total_candidates: int | None = None,
) -> str:
    """The candidate list as one plain-text body.  Every sentence is separated by two literal
    ASCII spaces, and every candidate is one sentence:  its letter, its seat and its title.

    `candidates` may already be truncated to MAX_CANDIDATES;  `total_candidates` is how many
    sessions hold the letter in all, so the count sentence stays true when the list is short.

    The body says which letter the seat is using by default and that the ask does not block, so
    the sender can answer the seat without waiting for a second round trip.
    """
    views = [candidate_view(row) for row in candidates if isinstance(row, Mapping)]
    views = [view for view in views if view["letter"]]
    seat_name = _one_line(seat, 32) or "the seat"
    platform_name = _one_line(platform, 40) or "the platform"
    used = [_one_line(letter, 8) for letter in letters]
    used = [letter for letter in used if letter]
    # The real number of holders, never the truncated list length:  a short list must not read as
    # an exhaustive one.
    total = total_candidates if isinstance(total_candidates, int) else len(views)

    sentences: list[str] = [
        "Operator session candidates for the seat %s on %s." % (seat_name, platform_name)
    ]
    if used:
        if total > 1:
            sentences.append(
                "The address used the letter%s %s, and %d sessions could hold it."
                % ("" if len(used) == 1 else "s", ", ".join(used), total)
            )
        else:
            sentences.append(
                "The address used the letter%s %s."
                % ("" if len(used) == 1 else "s", ", ".join(used))
            )
    else:
        sentences.append("The address carried no letter.")
    sentences.extend(_candidate_sentence(view) for view in views)
    if default_letter:
        sentences.append(
            "%s is using letter %s by default." % (seat_name, _one_line(default_letter, 8))
        )
    else:
        sentences.append("%s has no default letter and will name the one it used." % seat_name)
    sentences.append(
        "The ask never blocks: the seat proceeds on its default letter, names the letter it "
        "used, and answers in its own thread."
    )
    sentences.append(
        "If none of these is the session you meant, reply with the letter and the Operator will "
        "re-bind the thread."
    )
    return GAP.join(sentences)


# --------------------------------------------------------------------------------------------
# The recipient
# --------------------------------------------------------------------------------------------


def _member_ok(member: Any) -> bool:
    """Whether one Zulip member record carries every field this module consumes, with the right
    shape.  Checked before the record is used, so a malformed record is a refusal rather than a
    failure somewhere downstream."""
    if not isinstance(member, Mapping):
        return False
    user_id = member.get("user_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return False
    for flag in ("is_active", "is_bot"):
        if not isinstance(member.get(flag, True), bool):
            return False
    bot_type = member.get("bot_type", GENERIC_BOT)
    if not isinstance(bot_type, int) or isinstance(bot_type, bool):
        return False
    return all(
        isinstance(member.get(name, ""), (str, type(None)))
        for name in ("full_name", "email", "delivery_email")
    )


def _members_match(members: Sequence[Mapping[str, Any]], needle: str) -> list[Mapping[str, Any]]:
    """The users a typed name could mean:  an exact full name, an email, a seat tag or a user
    id.  Anything else matches nothing, because a guess is worse than a refusal here."""
    text = needle.strip().casefold()
    if not text:
        return []
    found = [
        user
        for user in members
        if str(user.get("full_name") or "").casefold() == text
        or text
        in (
            str(user.get("email") or "").casefold(),
            str(user.get("delivery_email") or "").casefold(),
        )
    ]
    if not found:
        seat_needle = I.canonical_seat(needle).casefold()
        found = [
            user
            for user in members
            if user.get("is_bot") and I.seat_tag_for(user).casefold() == seat_needle
        ]
    if not found and text.isascii() and text.isdigit():
        found = [user for user in members if user.get("user_id") == int(text)]
    return found


def resolve_recipient(
    members: Sequence[Mapping[str, Any]], recipient: Any
) -> tuple[int | None, str]:
    """(the recipient's user id, why it failed).  A recipient that cannot be resolved is refused,
    never guessed:  a wrong id would put the list in front of the wrong person.

    `recipient` is a user id, a typed name (full name, email, seat tag) or a realm user record.
    """
    if recipient is None or recipient == "":
        return None, "no recipient named"
    if isinstance(recipient, bool):
        return None, "the recipient is not a user id"
    if isinstance(recipient, int):
        for user in members:
            if user.get("user_id") == recipient:
                return _usable(user)
        return None, "no user has the id %d" % recipient
    if isinstance(recipient, Mapping):
        user_id = recipient.get("user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None, "the recipient record carries no user id"
        for user in members:
            if user.get("user_id") == user_id:
                return _usable(user)
        return None, "no user has the id %d" % user_id
    if not isinstance(recipient, str):
        return None, "the recipient is not a user id or a name"
    found = _members_match(members, recipient)
    if not found:
        return None, "no user is named %r" % recipient.strip()[:60]
    if len(found) > 1:
        return None, "%r matches %d users; name one by email" % (recipient.strip()[:60], len(found))
    return _usable(found[0])


def _usable(user: Mapping[str, Any]) -> tuple[int | None, str]:
    """The id of a user that can actually take a direct message, or why not."""
    user_id = user.get("user_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None, "the recipient carries no user id"
    if not user.get("is_active", True):
        return None, "the recipient is a deactivated user"
    if user.get("is_bot") and user.get("bot_type", GENERIC_BOT) != GENERIC_BOT:
        return None, "the recipient is a webhook or integration bot"
    return int(user_id), ""


# --------------------------------------------------------------------------------------------
# The dedupe state
# --------------------------------------------------------------------------------------------


def state_path(env: Mapping[str, str], seat: str) -> str:
    """The asking seat's own state file:  `<state root>/<SEAT>/operator-candidates.json`."""
    return os.path.join(L.state_root(env), seat, STATE_FILE)


def _sends(path: str) -> dict[str, Any]:
    data = L.read_json(path)
    sends = data.get("sends")
    return dict(sends) if isinstance(sends, dict) else {}


def _prune(sends: Mapping[str, Any], at: float, window: float) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key, row in sends.items():
        when = row.get("at") if isinstance(row, Mapping) else None
        if isinstance(when, (int, float)) and not isinstance(when, bool) and at - when < window:
            keep[key] = dict(row)
    return keep


def _fingerprint(recipient_id: int, body: str) -> str:
    return hashlib.sha256(("%d\n%s" % (recipient_id, body)).encode("utf-8")).hexdigest()


#: Dedupe outcomes, so a retry never resends an acknowledged message and never blocks a
#: recoverable one:
#:
#:   - `pending`:  a reservation is held and the write is in flight.
#:   - `ok`:       Zulip accepted the message; the row carries its id, and a retry inside the
#:                 window returns that cached result instead of sending again.
#:   - `unknown`:  the write may or may not have reached Zulip.  Replay is refused, because the
#:                 message may already exist.
#:   - `refused`:  a definite refusal.  The key is released, so a clean retry is still possible.
DELIVERY_PENDING = "pending"
DELIVERY_OK = "ok"
DELIVERY_UNKNOWN = "unknown"
DELIVERY_REFUSED = "refused"


def _reserve(
    path: str, key: str, at: float, window: float, recipient_id: int
) -> tuple[bool, float, dict[str, Any]]:
    """(allowed to send, seconds since the last identical send, the stored row).

    The reservation is written before the message, so two asks at once cannot both send.  An
    existing `ok` row carries the cached result the caller should return instead of losing the
    message id on a retry;  an `unknown` or `refused` row blocks a replay inside the window,
    because the message may already exist or the credential is still bad.
    """
    with L.locked(path):
        sends = _prune(_sends(path), at, window)
        row = sends.get(key)
        if isinstance(row, Mapping):
            when = row.get("at")
            if isinstance(when, (int, float)) and not isinstance(when, bool):
                return False, max(0.0, at - float(when)), dict(row)
        sends[key] = {
            "at": at,
            "recipient": recipient_id,
            "state": DELIVERY_PENDING,
            "message_id": None,
        }
        L.write_json(path, {"sends": sends}, durable=True)
    return True, 0.0, {}


def _record(
    path: str,
    key: str,
    at: float,
    window: float,
    recipient_id: int,
    message_id: int | None,
    state: str,
) -> None:
    with L.locked(path):
        sends = _prune(_sends(path), at, window)
        sends[key] = {
            "at": at,
            "recipient": recipient_id,
            "state": state,
            "message_id": message_id,
        }
        L.write_json(path, {"sends": sends}, durable=True)


def _release(path: str, key: str, at: float, window: float) -> None:
    """Free a key for a send that certainly did not happen.

    Only a definite refusal calls this.  A write whose outcome is unknown keeps its key, so a
    later retry refuses the replay rather than sending a second copy of a message that may
    already exist.
    """
    with L.locked(path):
        sends = _prune(_sends(path), at, window)
        sends.pop(key, None)
        L.write_json(path, {"sends": sends}, durable=True)


def _masked(key: Any) -> str:
    """A credential preview safe to log and to return to the caller:  the first eight and the last
    four characters only.  A rejected Operator key must be identifiable without ever being
    written out in full."""
    text = str(key or "")
    if not text:
        return "missing"
    if len(text) <= 12:
        return "%s…" % text[:1]
    return "%s…%s" % (text[:8], text[-4:])


# --------------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------------


def _outcome(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "reason": reason}
    out.update(extra)
    return out


def _operator_client(creds: Any, realm: str | None) -> Any:
    return Z.ZulipClient(creds, realm or creds.site)


def send_candidate_list(
    *,
    seat: str,
    platform: str,
    letters: Sequence[str] | str = (),
    candidates: Sequence[Mapping[str, Any]] = (),
    recipient: Any = None,
    default_letter: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
    owner_user_id: int = 0,
    realm: str | None = None,
    now: float | None = None,
    client: Any = None,
    credentials: Any = None,
    fetch: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
    window: float = DEDUPE_WINDOW,
) -> dict[str, Any]:
    """Send the candidate list to the sender of a tagged address, as the Operator bot.

    `seat` is the seat that is asking, `platform` the platform its sessions live on, `letters` the
    letter(s) the sender used or could have meant, and `candidates` the candidate sessions (each
    with its letter, its seat, its native session id and its title).  `recipient` is the sender:  a
    user id, a typed name or a realm user record.  `default_letter` is the letter the seat is
    using by default; when it is missing the seat's own candidate names it.

    The answer is a dict, never an exception:  `status` is `sent`, `refused` or `duplicate`, and
    `reason` says why a refusal happened.  The message always goes out as the Operator bot, with
    the Operator credential and the Operator identity, never with the asking seat's own bot.
    """
    try:
        return _send_candidate_list(
            seat=seat,
            platform=platform,
            letters=letters,
            candidates=candidates,
            recipient=recipient,
            default_letter=default_letter,
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
        return _outcome("refused", "the candidate list could not be sent: %s" % type(exc).__name__)


def _send_candidate_list(
    *,
    seat: str,
    platform: str,
    letters: Sequence[str] | str,
    candidates: Sequence[Mapping[str, Any]],
    recipient: Any,
    default_letter: str | None,
    env: Mapping[str, str] | None,
    home: str | None,
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

    seat_name = _one_line(seat, 32).upper()
    if not seat_name:
        return _outcome("refused", "no seat named")
    platform_name = _one_line(platform, 40)
    if not platform_name:
        return _outcome("refused", "no platform named")
    if isinstance(letters, str):
        used_letters = [letters]
    else:
        used_letters = list(letters or [])
    views = [candidate_view(row) for row in (candidates or []) if isinstance(row, Mapping)]
    views = [view for view in views if view["letter"]]
    if not views:
        return _outcome("refused", "no candidate sessions to name")
    # Counted before the shortlist:  MAX_CANDIDATES caps what the body lists, never how many
    # sessions could hold the letter.
    total_candidates = len(views)
    if len(views) > MAX_CANDIDATES:
        views = views[:MAX_CANDIDATES]
    if not default_letter:
        own = [view["letter"] for view in views if view["seat"].upper() == seat_name]
        default_letter = own[0] if len(own) == 1 else None

    body = render_body(
        seat=seat_name,
        platform=platform_name,
        letters=used_letters,
        candidates=views,
        default_letter=default_letter,
        total_candidates=total_candidates,
    )
    # A title is a person's words:  a mention in it would notify somebody in a DM that is only a
    # list, and a secret-shaped string in it would put a secret in front of the owner.
    body = W.neutralize_mentions(body)

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
            client = _operator_client(creds, realm)
        except Exception as exc:
            return _outcome(
                "refused", "the Operator client could not be built: %s" % type(exc).__name__
            )

    # The bot that sends this must be the pinned Operator:  the asking seat's own bot never sends
    # the list, and a look-alike bot never sends it either.
    if not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool) or owner_user_id <= 0:
        return _outcome("refused", "the owner is not pinned, so the Operator bot cannot be checked")
    try:
        me = client.get("users/me")
    except Z.AgentSyncError as exc:
        return _outcome("refused", "the Operator identity could not be confirmed: %s" % exc)
    except Exception as exc:
        return _outcome(
            "refused", "the Operator identity could not be confirmed: %s" % type(exc).__name__
        )
    try:
        is_operator = SI.operator_bot(me, environment, int(owner_user_id))
    except Z.CredentialError as exc:
        return _outcome("refused", "the Operator identity is not configured: %s" % exc)
    except Exception as exc:
        return _outcome(
            "refused", "the Operator identity is not configured: %s" % type(exc).__name__
        )
    if not is_operator:
        return _outcome("refused", "the authenticated bot is not the pinned Operator")

    try:
        raw_members = client.get("users")
    except Z.AgentSyncError as exc:
        return _outcome("refused", "the recipient could not be looked up: %s" % exc)
    except Exception as exc:
        return _outcome("refused", "the recipient could not be looked up: %s" % type(exc).__name__)
    # Validated before anything downstream reads it:  a missing or malformed `users` payload must
    # be a refusal, never a silent empty inventory that moves the failure into resolve_recipient.
    if not isinstance(raw_members, Mapping) or not isinstance(raw_members.get("members"), list):
        return _outcome("refused", "the Zulip users response is malformed")
    members = raw_members["members"]
    if any(not _member_ok(member) for member in members):
        return _outcome("refused", "a Zulip member record is malformed")
    recipient_id, why = resolve_recipient(members, recipient)
    if recipient_id is None:
        return _outcome("refused", "the recipient is not resolvable: %s" % why)
    if recipient_id == me.get("user_id"):
        return _outcome("refused", "the recipient is the Operator itself")

    hidden = list(secretscan.basic_forms(str(creds.email), str(creds.key)))
    flagged = secretscan.scan(body, hidden)
    if flagged:
        # The scan result itself can carry the suspected secret or its prefix, so it is never
        # echoed.  The caller gets a stable code and the field that was withheld, nothing more.
        return _outcome(
            "refused",
            "the candidate list was withheld: it matched the secret scan",
            delivery_status=DELIVERY_REFUSED,
            code="secret",
            field="candidate_list",
        )

    path = state_path(environment, seat_name)
    key = _fingerprint(recipient_id, body)
    allowed, since, stored = _reserve(path, key, at, window, recipient_id)
    if not allowed:
        state = str(stored.get("state") or "")
        if state == DELIVERY_OK:
            # A successful send is cached, so a retry returns the same answer with the same
            # message id instead of sending again.
            return _outcome(
                "sent",
                "the candidate list already went to this recipient %d seconds ago" % int(since),
                delivery_status=DELIVERY_OK,
                recipient=stored.get("recipient", recipient_id),
                message_id=stored.get("message_id"),
                body=body,
            )
        if state == DELIVERY_UNKNOWN:
            return _outcome(
                "refused",
                "an identical candidate list to this recipient has an unknown delivery outcome "
                "and is not replayed",
                delivery_status=DELIVERY_UNKNOWN,
                recipient=recipient_id,
                body=body,
            )
        return _outcome(
            "duplicate",
            "an identical candidate list already went to this recipient %d seconds ago"
            % int(since),
            delivery_status=DELIVERY_REFUSED,
            recipient=recipient_id,
            body=body,
        )

    try:
        result = client.post("messages", {"type": "direct", "to": [recipient_id], "content": body})
    except Z.NetworkError as exc:
        # A timeout or a post-send disconnect leaves the outcome unknown:  the message may already
        # exist, so the key is kept and replay is refused.  A failure that provably never reached
        # Zulip is retryable, so it gives the key back instead of blocking the dedupe window.
        if getattr(exc, "maybe_sent", False):
            _record(path, key, at, window, recipient_id, None, DELIVERY_UNKNOWN)
            return _outcome(
                "refused",
                "the candidate list delivery is unknown: %s" % exc,
                delivery_status=DELIVERY_UNKNOWN,
                recipient=recipient_id,
                body=body,
            )
        _release(path, key, at, window)
        return _outcome(
            "refused",
            "the candidate list was not sent and may be retried: %s" % exc,
            delivery_status=DELIVERY_REFUSED,
            retryable=True,
            recipient=recipient_id,
            body=body,
        )
    except Z.AgentSyncError as exc:
        # Zulip answered and refused:  the message definitely did not go out, so the key is
        # released and the refusal is terminal.  A rejected credential is named by a masked
        # preview only, never by any part of the key a caller could not otherwise see.
        status = getattr(exc, "status", getattr(exc, "status_code", None))
        _release(path, key, at, window)
        if status in (401, 403):
            return _outcome(
                "refused",
                "the Operator credential was rejected.  Preview: %s"
                % _masked(getattr(creds, "key", "")),
                delivery_status=DELIVERY_REFUSED,
                code="credential_rejected",
                recipient=recipient_id,
            )
        return _outcome(
            "refused",
            "the candidate list could not be sent: %s" % exc,
            delivery_status=DELIVERY_REFUSED,
            recipient=recipient_id,
        )
    except Exception as exc:
        _release(path, key, at, window)
        return _outcome(
            "refused",
            "the candidate list could not be sent: %s" % type(exc).__name__,
            delivery_status=DELIVERY_REFUSED,
            recipient=recipient_id,
        )
    message_id = result.get("id") if isinstance(result, Mapping) else None
    message_id = (
        int(message_id)
        if isinstance(message_id, int) and not isinstance(message_id, bool)
        else None
    )
    _record(path, key, at, window, recipient_id, message_id, DELIVERY_OK)
    return _outcome(
        "sent",
        "the candidate list went to the sender as the Operator bot",
        delivery_status=DELIVERY_OK,
        recipient=recipient_id,
        message_id=message_id,
        body=body,
    )
