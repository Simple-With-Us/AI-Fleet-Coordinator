"""The DM rule and the Side-Chats route (owner decision, Wed, Oct 22 2026, hard cutover).

Jay's decision (d):  bot-to-bot conversation happens in Side-Chats topics, never in DMs, and the
only new DMs Operator may open are one clarifying question on a wake whose addressee it cannot
resolve and, since Oct 29 2026, its one-to-one status message to the sender.  A seat may never
open a DM on its own, on either MCP transport or the CLI.

The rule, in one place so all three surfaces enforce it the same way:

* **Blocked** — a seat opens a new DM (`zulip_dm_send`, or the CLI `dm` verb).  Refused with a
  message that names where the message belongs instead, and audited.
* **Allowed** — a reply inside a DM Operator already opened.  The conversation is Operator's; a seat
  answering in it is not opening anything.
* **Allowed** — Operator opens a DM, and only to ask the sender one clarifying question when the
  wake's addressee is ambiguous, at most one per message (`clarifying=True` below).  See clarify.py
  for the queue and the 30-minute timeout.
* **Allowed** — Operator opens a one-to-one DM to the sender to say what became of a wake (woken,
  alerted, queued, held, dropped or paused), owner ruling Thu, Oct 29 2026, about 3:35 AM (`status=True`
  below).  The text is deterministic and carries no project, topic or message content, so the
  owner is not in the conversation (the owner-in-the-conversation guard has one matching
  exemption, `ZulipClient.operator_status_dm`).  The Operator sends it in process, from the
  server instance:  no CLI, stdio MCP or hosted surface takes `status`.

Everything else routes to Side-Chats:  a seat-to-seat message becomes a post in stream 646987 under
one topic per discussion, mentioning only the participants;  a seat's ping to Jay goes to the
Pending-for-Jay topic there, never a new DM.  The stream is keyed on the **id** (646987), never the
name, because the live display name differs from Jay's spelling and has changed before.

The block is behind `AGENT_SYNC_DM_RULE` and defaults **on** (hard cutover).  Set it to `off`,
`0`, `false` or `no` to restore today's behaviour; nothing else turns it off.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from . import identity_log as AUDIT
from . import zulip as Z

# Jay spells the channel Side-Chats;  the live Zulip stream shows a lowercase display name, so the
# code keys on the id and never on the name.
SIDE_CHATS_STREAM_ID = 646987
SIDE_CHATS_NAME = "Side-Chats"
PENDING_FOR_JAY_TOPIC = "Pending-for-Jay"

OPERATOR_SEAT = Z.OPERATOR_SEAT

ENV_RULE = "AGENT_SYNC_DM_RULE"
ENV_TIMEOUT_MINUTES = "AGENT_SYNC_DM_CLARIFY_TIMEOUT_MINUTES"
DEFAULT_TIMEOUT_MINUTES = 30.0  # Jay confirmed 30 minutes, Oct 22 2026 (Q11)

OFF = frozenset({"off", "0", "false", "no", "none", "disabled"})

GUIDANCE_OWNER = (
    "Jay is reached through the Pending-for-Jay post in #%s (stream %d) or a topic there, not a new "
    "direct message." % (SIDE_CHATS_NAME, SIDE_CHATS_STREAM_ID)
)
GUIDANCE_SEAT = (
    "Seat-to-seat work happens in a #%s topic (stream %d):  one topic per discussion, mentioning only "
    "the participants, never a direct message." % (SIDE_CHATS_NAME, SIDE_CHATS_STREAM_ID)
)
GUIDANCE_PEOPLE = (
    "Direct messages are Operator's to open.  Put this in a #%s topic (stream %d) instead, or let "
    "Operator ask one clarifying question if a wake could not be routed."
    % (SIDE_CHATS_NAME, SIDE_CHATS_STREAM_ID)
)


class DmRefused(Z.AgentSyncError):
    """A seat tried to open a direct message on its own.  Carries the audit `reason`.

    An AgentSyncError, so the CLI exits 3 (a refusal) and not 2 (a malformed command):  a caller can
    tell "the rule said no" from "you typed it wrong"."""

    code = "dm_not_allowed"
    exit_code = 3

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class MissingOperatorId(DmRefused):
    """The runtime could not tell who the Operator bot is.

    Its own code and reason, because a missing deployment setting is not the DM rule saying no.
    A seat that answers inside a DM Operator opened was told "you may not open a direct message",
    which is both untrue (it is answering, not opening) and unactionable (the seat cannot fix a
    host that is missing a variable).  The message names the variable the host has to set, and
    never a value."""

    code = "missing_operator_user_id"


def rule_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True unless AGENT_SYNC_DM_RULE explicitly turns the block off.  The default is on."""
    raw = (env or {}).get(ENV_RULE)
    if raw is None:
        return True
    return raw.strip().casefold() not in OFF


def clarifying_exempt(identity: str, *, clarifying: bool) -> bool:
    """True when the rule permits Operator's own clarifying question.

    The one message the rule allows without knowing who the Operator is:  the Operator is the
    sender, so nothing has to be proven.  A reply inside a conversation Operator opened still needs
    the id, because only the id proves who opened it.

    The surfaces ask this question before they resolve the Operator's id, so a host whose Operator
    id cannot be read can still send the one question the rule permits (Kody review on AFC-473)."""
    return identity == OPERATOR_SEAT and clarifying


def status_exempt(identity: str, *, status: bool) -> bool:
    """True when the rule permits Operator's one-to-one status message to the sender.

    Like the clarifying question it needs no Operator id:  the Operator is the sender.  Nothing else
    qualifies, and a seat that asks for `status` is still a seat opening a DM."""
    return identity == OPERATOR_SEAT and status


def clarify_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """How long Operator waits for an answer to its clarifying question before dropping the wake.
    AGENT_SYNC_DM_CLARIFY_TIMEOUT_MINUTES, default 30."""
    raw = (env or {}).get(ENV_TIMEOUT_MINUTES)
    if raw is None or not str(raw).strip():
        return DEFAULT_TIMEOUT_MINUTES * 60.0
    try:
        minutes = float(str(raw).strip())
    except ValueError:
        return DEFAULT_TIMEOUT_MINUTES * 60.0
    return max(0.0, minutes) * 60.0


def classify(recipients: Sequence[Mapping[str, Any]], *, owner_user_id: int | None = None) -> str:
    """Which rule the recipients fall under:  `owner`, `seat` (any bot), or `people`.

    `owner_user_id` is the owner pinned in listener.toml;  it is optional because the hosted server
    has no listener config and treats any non-bot human recipient as `people`."""
    bots = [r for r in recipients if r.get("is_bot")]
    if bots:
        return "seat"
    if owner_user_id is not None and any(r.get("user_id") == owner_user_id for r in recipients):
        return "owner"
    return "people"


def guidance_for(kind: str) -> str:
    return {"owner": GUIDANCE_OWNER, "seat": GUIDANCE_SEAT}.get(kind, GUIDANCE_PEOPLE)


def check_new_dm(
    env: Mapping[str, str] | None,
    *,
    identity: str,
    recipients: Sequence[Mapping[str, Any]],
    operator_opened: bool | None = False,
    clarifying: bool = False,
    owner_user_id: int | None = None,
    status: bool = False,
) -> None:
    """Raise DmRefused unless this DM may be opened.  Nothing here writes or sends anything.

    `operator_opened` is what `operator_opened_dm` could prove:  True when the conversation is
    provably Operator's, False when it is provably not, and None when a conversation exists and
    the Operator's id is not configured, so nobody can tell.

    **Only Operator's own clarifying question is id-free.**  A reply inside a conversation Operator
    opened needs the id too:  `operator_opened_dm` has to resolve the id to prove the conversation
    is Operator's, and without it `missing_operator_user_id` applies.  A maintainer must not read
    the id as optional here, because deleting the fail-closed branch would reopen the direct
    messages a seat was told not to open (Kody review on AFC-473)."""
    if not rule_enabled(env):
        return
    # A reply inside a conversation Operator opened is a seat answering, not a seat opening one.
    if operator_opened is True:
        return
    if identity == OPERATOR_SEAT:
        # Operator may open a DM for exactly two reasons, and the caller says which.
        if clarifying_exempt(identity, clarifying=clarifying) or status_exempt(
            identity, status=status
        ):
            return
        raise DmRefused(
            "%s may open a direct message only to ask the sender one clarifying question when a wake "
            "could not be routed.  Bot-to-bot work goes to a #%s topic (stream %d)."
            % (OPERATOR_SEAT, SIDE_CHATS_NAME, SIDE_CHATS_STREAM_ID),
            reason="operator_not_clarifying",
        )
    kind = classify(recipients, owner_user_id=owner_user_id)
    if operator_opened is None and kind == "seat":
        # The exemptions above did not decide it and the attribution is unknown, so the host is
        # missing the variable the rule needs.  Say what is actually wrong:  a missing deployment
        # setting, which the DM rule cannot fix and the seat cannot set either.  Failing closed
        # here is right;  failing closed *before* the exemptions ran was not.
        #
        # This is the answer only when the attribution can change the verdict:  a bot is in the
        # conversation, so it may be the Operator, and setting the id may permit the message.  A
        # conversation with only people in it cannot be Operator's, so the rule's own reason is the
        # actionable answer there, and telling the host to set a variable could never permit the
        # message (Kody review on AFC-473).
        raise MissingOperatorId(
            "the Operator's Zulip user id is not set, so a direct message Operator opened cannot "
            "be told apart from one a seat opened:  set %s in the seat host's environment"
            % Z.ENV_OPERATOR_USER_ID,
            reason="missing_operator_user_id",
        )
    raise DmRefused(
        "this seat may not open a new direct message (%s).  %s" % (kind, guidance_for(kind)),
        reason=kind,
    )


def needs_clarification(addressees: Sequence[str]) -> bool:
    """True when a wake's addressee cannot be resolved to exactly one seat.

    One candidate routes cleanly and Operator says nothing.  Zero or several is ambiguous, and only
    then may Operator open one clarifying DM to the sender (one per message, never more)."""
    seats = {(a or "").strip().upper() for a in addressees if (a or "").strip()}
    return len(seats) != 1


def clarifying_question(addressees: Sequence[str]) -> str:
    """The one question Operator asks:  which seat the wake is for.  Nothing else."""
    return (
        "Operator could not tell which seat this is for (candidates: %s).  Reply with the seat tag, or "
        "nothing happens and the wake is dropped when the timeout passes.  One question per message."
        % (
            ", ".join(sorted({(a or "").strip().upper() for a in addressees if (a or "").strip()}))
            or "none"
        )
    )


def side_chats_topic(participants: Sequence[str]) -> str:
    """One topic per discussion, named for the participants and nobody else.

    Jay's rule is that a Side-Chats topic mentions only the people in it, so the topic is built
    from the seat tags of the participants with no free text from the caller."""
    names = sorted({(p or "").strip().upper() for p in participants if (p or "").strip()})
    if not names:
        return SIDE_CHATS_NAME
    return " <-> ".join(names)


def pending_for_jay_topic() -> str:
    """The one Pending-for-Jay topic in Side-Chats;  a seat's ping to Jay is one updated post here."""
    return PENDING_FOR_JAY_TOPIC


def side_chats_post(
    client: Any,
    *,
    topic: str,
    content: str,
    stream_id: int = SIDE_CHATS_STREAM_ID,
) -> int:
    """Post to Side-Chats and return the message id.  The stream is named by id, never by name."""
    result = client.post(
        "messages",
        {"type": "stream", "to": str(stream_id), "topic": topic, "content": content},
    )
    return int(result["id"])


def audit_refusal(
    env: Mapping[str, str] | None,
    home: Any,
    *,
    transport: str,
    identity: str | None,
    recipients: Sequence[Mapping[str, Any]],
    reason: str,
    message: str,
    authenticated_seat: str | None = None,
) -> bool:
    """Append the refusal to the identity audit log.  Ids and the reason only, never a body.

    `authenticated_seat` is the seat the grant was issued to and `identity` the bot tag it acted
    as.  Both are persisted, because a grant for one seat may act as another:  an admin reading a
    refusal row needs both to know which grant was used and what it claimed to be.
    """
    return AUDIT.append(
        env or {},
        home,
        transport=transport,
        default_identity=None,
        identity=identity,
        tool="zulip_dm_send",
        outcome="dm_refused:%s" % reason,
        client={"note": message[:160]},
        seat=authenticated_seat,
    )


def operator_opened_dm(
    people: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    *,
    operator_user_id: int | None = None,
    self_id: int | None = None,
) -> bool | None:
    """True when this exact conversation already exists and Operator opened it, False when it
    provably did not, and None when nobody can tell.

    A conversation is Operator's when its oldest message came from the Operator bot, so a seat may
    answer inside it.  An empty conversation (no messages yet) is not: that would be a new DM.

    The match is the **exact** 1:1 pair, never a superset:  a group conversation that merely contains
    the same people is not the conversation this call would open, and answering in it is not the
    reply-inside-an-Operator-DM the rule allows.  `self_id` is this seat's own bot id when the caller
    knows it;  it is the other half of the pair and is never treated as an extra participant.

    **None means unknown, not no.**  The Operator's id is the only thing that proves Operator opened
    the conversation, so without it the runtime does not guess and does not decide here either:  the
    id is a deployment setting, and the rule's own exemptions (a reply inside a conversation Operator
    opened, Operator's one clarifying question) have to run before a missing variable becomes the
    answer.  `check_new_dm` raises `MissingOperatorId` once those exemptions have passed."""
    wanted = {int(p["user_id"]) for p in people if isinstance(p.get("user_id"), int)}
    if not wanted:
        return False
    pair = wanted | ({int(self_id)} if isinstance(self_id, int) else set())
    oldest: Mapping[str, Any] | None = None
    for message in messages:
        if message.get("type") != "private":
            continue
        ids = {
            int(p["id"])
            for p in message.get("display_recipient") or []
            if isinstance(p, Mapping) and isinstance(p.get("id"), int)
        }
        if ids != pair:
            continue
        if oldest is None or int(message.get("id") or 0) < int(oldest.get("id") or 0):
            oldest = message
    if oldest is None:
        return False
    sender = oldest.get("sender_id")
    if sender is None:
        return False
    operator_id = operator_user_id if operator_user_id is not None else Z.operator_user_id()
    if operator_id is None:
        # The Operator's id is not configured, so no conversation is provably Operator-opened and
        # the answer would be a guess.  Report "unknown" instead of deciding here:  a permitted DM
        # (a reply inside a conversation Operator opened, or Operator's own clarifying question)
        # does not need the id, so the exemptions run first and only then does the missing
        # deployment setting become the answer (Kody review on AFC-473).
        return None
    return int(sender) == int(operator_id)


def operator_user_id(env: Mapping[str, str] | None = None, pinned: Any = None) -> int | None:
    """The Operator bot's Zulip user id:  OPERATOR_ZULIP_USER_ID, else the id the host pinned,
    else None when neither is set.

    No id is committed, so a deployment that has not set one gets None rather than a guess, and
    the caller fails closed.  The environment comes first, so a host that pins the id in its own
    environment overrides the local config;  `pinned` is the id the host's own listener.toml
    carries, the same place the owner's id is pinned.  Only an id, never a key;  it is safe to
    log.

    `pinned` may be a callable that resolves it, so the host's config is read only when the
    environment has no id:  a host that exports the id is never broken by a config file it does
    not consult, and a damaged one fails closed by name instead of looking like an unpinned host
    (Kody review on AFC-473)."""
    found = Z.operator_user_id(env)
    if found is not None:
        return found
    if callable(pinned):
        pinned = pinned()
    if isinstance(pinned, int) and not isinstance(pinned, bool) and pinned > 0:
        return pinned
    return None


def env_or_root_default(env: Mapping[str, str] | None, name: str, fallback: str) -> str:
    value = (env or {}).get(name)
    return value if value else fallback


def state_file(root: str) -> str:
    return os.path.join(root, "listener", "clarify.json")
