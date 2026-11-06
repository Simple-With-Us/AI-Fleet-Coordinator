"""Routing for the listener daemon:  classify one message for one seat and decide where it goes.

Pure functions over plain data, no I/O, so every rule is unit-tested directly.  The daemon owns
dedupe (cursor and ring), the files, the coalescing and the budgets.

Steps (design section 2), for each (seat, message) pair:

  1. Drop a muted topic unless the message is `direct` or a DM.
  2. Classify:  own, owner, eligible, direct, fleet, wildcard, dm, wake_tag, stale, reply_to.
     `fleet` is the fleet-wide wake:  a stream-wide wildcard (`@**all**`) in #work-in-progress > `fleet`
     (owner 2026-10-09: there is no `fleet` user group and none will be made).  A wildcard anywhere
     else is only `wildcard`.
  3. Own posts go to other leases on the topic as passive sibling items.  Never inbox, never wake.
  4. A leased topic:  every live lease on it gets the item, interrupt or passive.  Done.
  5. A mention-class item in an unleased topic goes to the one live, wake-capable claude-code
     lease with `mentions` (latest owner prompt, then newest), and the seat-inbox copy is marked
     delivered_to.  No headless wake follows.
  6. Otherwise the seat inbox, if direct, dm, fleet, owner, wildcard or a followed topic.
  7. The wake prefilter, for seat-inbox items only.

OWNER topics (open-loops C6, owner 2026-10-11):  a topic whose name starts `OWNER ` (a resolved check mark in
front does not hide it) is where a decision waits on the owner.  Nothing a peer posts there wakes anyone:
a bot's direct mention, wildcard or `re=` reply is not eligible, so it reaches no lease as an interrupt and
no seat as a wake, and a seat that reads its inbox still sees it.  The one carve-out is the needs-owner
notifier's directed note ("Jay replied"):  a message from CLAUDE's bot whose tag starts `[CLAUDE\u00b7needsown\u2192`.
It is an ordinary mention and wakes the filer, under the same peer budgets, loop guard and stale rule.  The
owner's own message there wakes only the seats it mentions (a human client, as everywhere).

Eligible senders (owner 2026-10-09:  "everyone should be able to DM to wake anyone else or tag to
wake anyone else"):  the owner, plus every fleet bot, which is an active generic bot in the realm
user list, owned by the owner (bot_owner_id), whose email maps (seat_tag_for) to a fleet tag:  a seat in the partition file (Mac and
cloud seats, GB personas, BF role bots, GROK-BUILD) or config.FLEET_SEATS.  The daemon computes the
set at runtime (fleet_bot_ids) and adds the optional `eligible_user_ids` pins, so a new seat needs
no re-init.  Integration and webhook bots (Sentry, PagerDuty, Linear), unknown senders, API posts
from the owner's account and a seat's own posts are never eligible.  An eligible bot's DM wakes the
seat like a direct mention:  peer budgets, the loop guard and the stale rule all apply.  A peer wake
is still peer data, never owner authority.

Owner (decision 5, confirmed 2026-10-07):  the sender is `owner_user_id` AND the message's
`client` is a human Zulip app.  A post with the owner's account from any other client (an API
key) is not the owner, not eligible, and flagged `owner_api`.  The `client` is what the sending
request reports, so this catches honest API use but cannot stop someone holding the owner's key
from claiming a human client:  owner priority is a routing hint, never authority for a side
effect.

The Oct 27 2026 rule (owner):  `@all` is mostly Jay's.  A bot uses it when Jay asks or explicitly
approves, or on its own judgment in an extremely serious situation (an ongoing targeted attack, a
fleet-wide outage of about twenty minutes).  A bot that uses it without explicit approval DMs Jay
why.  A fleet bot's `@all` therefore wakes like the owner's, under peer budgets, the loop guard
and the stale rule:  `peer_broadcast` is the class for it, and the daemon treats it like the
owner's broadcast for the fanout and the inbox copy.  The listener keeps the book:  when a bot's
`@all` is not explained to Jay within ten minutes, the held-alert seat DMs Jay a reminder
(`broadcast_audit.py`).

The broadcast tokens (owner, Oct 27 2026):  `@all`, `@channel` and `@stream` all count exactly
like `@all`; `@everyone` on its own is still inert.  `@topic` is not a broadcast token and never
was.  A silent `@_**all**` (an escaped mention) is not a token either:  the leading underscore
keeps it out of the wildcard set, and a message that carries no token is ordinary text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .identity import seat_tag_for
from .live import is_presence, topic_key
from .zulip import DEFAULT_CHANNEL

INTERRUPT = "interrupt"
PASSIVE = "passive"
WILDCARD_FLAGS = ("wildcard_mentioned", "stream_wildcard_mentioned", "topic_wildcard_mentioned")
# The one place a wildcard is the fleet-wide wake (owner 2026-10-09).  The realm
# renamed the channel on Oct 22, 2026, and the `fleet` topic moved with it.  It
# reads the one canonical default rather than repeating the literal, so the next
# rename cannot leave the wake guard naming a channel the default no longer posts to.
FLEET_WAKE_CHANNEL = DEFAULT_CHANNEL
FLEET_WAKE_TOPIC = "fleet"
FLEET_WAKE_KEY = topic_key(FLEET_WAKE_CHANNEL, FLEET_WAKE_TOPIC)
_BROADCAST_TOKEN_RE = re.compile(
    r"(?<![\\\w*])@(?:\*\*(all|everyone|channel|stream)\*\*|(all|everyone|channel|stream)\b(?![-\w*]))",
    re.IGNORECASE,
)
#: The tokens that mean "wake the fleet" (owner, Oct 27 2026):  `@all`, `@channel` and `@stream`
#: count exactly like `@all`.  `@everyone` is NOT one of them:  on its own it lands unread in
#: every bot's inbox and wakes nobody.
BROADCAST_TOKENS = frozenset({"all", "channel", "stream"})
_WAKE_TAG_RE = re.compile(r"^\s*\[[A-Za-z0-9_-]+·wake\b")
# A route post (`[CLAUDE·route→CODEX] re=N`):  a wake seat paged another seat on the owner's behalf.  It
# counts on the loop guard like a wake reply, but it never blocks the wake of the seat it mentions.
_ROUTE_TAG_RE = re.compile(r"^\s*\[[A-Za-z0-9_-]+·route\b")
_REPLY_TO_RE = re.compile(r"(?:^|\s)re=(\d+)\b")
_CODE_BLOCK_RE = re.compile(r"(?ms)^(```|~~~).*?(^\1\s*$|\Z)")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
# The OWNER-topic rule.  Zulip groups topic names without case, and its resolved check mark sits in front of the name.
OWNER_TOPIC_PREFIX = "owner "
_RESOLVED_MARKS = ("\u2714\ufe0f", "\u2714")
# The notifier's directed note (`simple-chat --as CLAUDE --session needsown post --to SEAT`) is tagged `[CLAUDE\u00b7needsown\u2192SEAT]`.
NOTE_SENDER_SEAT = "CLAUDE"
_NOTE_TAG_RE = re.compile(r"^\s*\[CLAUDE\u00b7needsown(?:\u2192|->)")

#: The no-wake marker (owner ask, relayed by Jet on 2026-10-21, Zulip 633790364):  a leading
#: eyes emoji on a message means "never wake anyone".  The message stays readable in Zulip, and
#: native human Zulip notifications are a separate system that this does not touch.  Both
#: spellings are accepted, because Zulip's own emoji picker writes the shortcode and a pasted
#: character arrives as itself.
NO_WAKE_MARKER = ":eyes:"
NO_WAKE_MARKERS = (":eyes:", "\U0001f440")
_NO_WAKE_RE = re.compile(r"^\s*(?:%s)" % "|".join(re.escape(m) for m in NO_WAKE_MARKERS))
#: One leading bracket seat tag (`[MM\u00b7session8] `, `[CLAUDE\u00b7needsown\u2192SEAT] `).  The
#: marker may follow it, because `simple-chat post` prepends that tag to whatever the agent wrote.
#: The session part is optional:  a seat with no session tag writes `[MM] `, and a labelled post
#: writes `[MM\u2192CODEX] `, so the tag is the seat name, then optionally a `\u00b7` session, then
#: optionally a `\u2192` label list, then the closing bracket.
_SEAT_TAG_PREFIX_RE = re.compile(
    r"^\s*\[(?P<seat>[A-Z0-9][A-Z0-9_-]*)(?P<session>(?:\u00b7[^\]]*)?)(?:\u2192[^\]]*)?\]\s*"
)


def _sender_seat_tag(message: Mapping[str, Any]) -> str | None:
    """The seat tag the sender signs its posts with (`MM`, `CLAUDE`), or None when the sender is
    not a seat bot and so has no tag of its own."""
    email = message.get("sender_email")
    if not email:
        return None
    return seat_tag_for({"is_bot": True, "email": email})


def strip_no_wake(content: str, seat: str | None = None) -> tuple[str, bool]:
    """(the content without a leading no-wake marker, whether the marker was there).

    The marker is the first non-space character of the content, or the first non-space
    character after one leading bracket seat tag.  A marker inside a quote line, a code span, a
    code block or a sentence is not a marker:  only a leading one is.  The rest of the content
    comes back unchanged, so the anchored tag regexes still match where they did.

    `seat` is the sender's own seat tag (`MM`, `CLAUDE`).  A bracket after which the marker is
    read must be a seat tag:  either it carries the `\u00b7` session separator, which no ordinary
    word has, or its seat name is the sender's own.  So `[TODO] :eyes: hi` stays a normal
    message, while `[MM] :eyes: hi` from MM is the signal.
    """
    text = str(content or "")
    lead = len(text) - len(text.lstrip())
    rest = text[lead:]
    match = _NO_WAKE_RE.match(rest)
    if match:
        return text[:lead] + rest[match.end() :], True
    tag = _SEAT_TAG_PREFIX_RE.match(rest)
    if tag and (tag.group("session") or (seat and tag.group("seat") == seat)):
        after = rest[tag.end() :]
        match = _NO_WAKE_RE.match(after)
        if match:
            return text[:lead] + rest[: tag.end()] + after[match.end() :], True
    return text, False


def _broadcast_tokens_visible(visible: str) -> frozenset[str]:
    return frozenset(
        (match.group(1) or match.group(2)).casefold()
        for match in _BROADCAST_TOKEN_RE.finditer(visible)
    )


def broadcast_tokens(content: str) -> frozenset[str]:
    """Raw `@all` / `@everyone` / `@channel` / `@stream` tokens outside code, quotes and escapes.

    Zulip sets the same wildcard flags for every one of those tokens, so wake routing must read
    the raw token, not `wildcard_mentioned` or `stream_wildcard_mentioned`.  `@topic` is out of
    scope here and always was."""
    return _broadcast_tokens_visible(outside_code(content))


def fleet_wake_mention(content_outside_code: str, flags: Iterable[str]) -> bool:
    """True when the visible content carries a raw `@all`, `@channel` or `@stream` token.

    Wildcard flags are ignored:  they cannot distinguish `@all` from `@everyone`."""
    return bool(_broadcast_tokens_visible(content_outside_code) & BROADCAST_TOKENS)


def tags_seat(content: str, me: SeatIdentity) -> bool:
    """Does this text tag the seat:  its own `@**Name**` (or `@**Name|id**`), or a fleet-wide `@all`,
    `@channel` or `@stream`, outside code and quotes?  A silenced `@_**Name**` never counts.

    An edit wakes a seat only when the new text tags it and the old text did not (owner, Oct 29
    2026), so a mention that was already there never wakes it a second time."""
    visible = outside_code(str(content or ""))
    names = ("@**%s**" % me.full_name, "@**%s|%d**" % (me.full_name, me.user_id))
    return any(n in visible for n in names) or fleet_wake_mention(visible, ())


def is_owner_topic(topic: str) -> bool:
    """True for a topic whose name starts `OWNER ` (any case), with or without Zulip's resolved check mark."""
    name = str(topic or "").strip()
    for mark in _RESOLVED_MARKS:
        if name.startswith(mark):
            name = name[len(mark) :].lstrip()
            break
    return name.casefold().startswith(OWNER_TOPIC_PREFIX)


def outside_code(content: str) -> str:
    """Content without code blocks, code spans and quoted lines, for raw mention checks."""
    text = _CODE_BLOCK_RE.sub("", content)
    text = _CODE_SPAN_RE.sub("", text)
    return _QUOTE_LINE_RE.sub("", text)


GENERIC_BOT = 1  # Zulip bot_type:  2 incoming webhook, 3 outgoing webhook, 4 embedded


def fleet_bots_by_seat(
    users: Mapping[int, Mapping[str, Any]],
    tags: Iterable[str],
    owner_user_id: int,
    *,
    outgoing_webhook_seats: Iterable[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """seat tag -> the fleet bots (as fleet_bot_ids decides) that sign as it, canonical email first:
    the bot whose email local part is the seat's own (`codex-bot@` for CODEX), then by user id.  One
    seat can have two bots (`grok-build-bot@` also signs as GROK)."""
    ids = fleet_bot_ids(users, tags, owner_user_id, outgoing_webhook_seats=outgoing_webhook_seats)
    found: dict[str, list[dict[str, Any]]] = {}
    for uid in sorted(ids):
        user = dict(users[uid], user_id=uid)
        found.setdefault(seat_tag_for(user), []).append(user)

    def canonical(seat: str, user: Mapping[str, Any]) -> tuple[int, int]:
        local = str(user.get("email") or "").split("@", 1)[0].lower()
        return (0 if local in (seat.lower() + "-bot", seat.lower()) else 1, int(user["user_id"]))

    for seat, bots in found.items():
        bots.sort(key=lambda u, seat=seat: canonical(seat, u))
    return found


def fleet_bot_ids(
    users: Mapping[int, Mapping[str, Any]],
    tags: Iterable[str],
    owner_user_id: int,
    *,
    outgoing_webhook_seats: Iterable[str] = (),
) -> set[int]:
    """User ids of the fleet bots in a realm user list:  active registered bots that the owner owns
    (`bot_owner_id`) and whose email maps to one of `tags` (config.fleet_tags).  Outgoing webhook bots count only for explicitly enabled seats.  Incoming webhook and embedded
    bots never count, whatever their email, and neither does a person.  The owner check is the trust
    anchor:  a bot someone else creates under a fleet-looking email is not a fleet bot, and with no
    owner pinned there are none."""
    wanted = set(tags)
    outgoing = set(outgoing_webhook_seats) & wanted
    found: set[int] = set()
    if not isinstance(owner_user_id, int) or owner_user_id <= 0:
        return found
    for uid, user in users.items():
        if not isinstance(uid, int) or isinstance(uid, bool) or user.get("is_bot") is not True:
            continue
        if user.get("is_active", True) is False:
            continue
        bot_type = user.get("bot_type", GENERIC_BOT)
        if bot_type != GENERIC_BOT and not (bot_type == 3 and seat_tag_for(user) in outgoing):
            continue
        if user.get("bot_owner_id") != owner_user_id:
            continue
        if seat_tag_for(user) in wanted:
            found.add(uid)
    return found


class SeatIdentity:
    def __init__(
        self,
        seat: str,
        user_id: int,
        full_name: str,
        email: str,
        bot_owner_id: int | None = None,
    ) -> None:
        self.seat = seat
        self.user_id = user_id
        self.full_name = full_name
        self.email = email
        self.bot_owner_id = bot_owner_id


class Context:
    """What the router needs to know beyond the message.  `is_bot` maps user ids to the bot flag;
    an unknown sender counts as a bot."""

    def __init__(
        self,
        *,
        owner_user_id: int,
        owner_clients: Iterable[str],
        eligible_user_ids: Iterable[int],
        is_bot: Mapping[int, bool],
        now: float,
        stale_after: float,
        presence: list[tuple[str, str]],
        muted: Iterable[str] = (),
        followed: Iterable[str] = (),
        operator_user_id: int = 0,
    ) -> None:
        self.owner_user_id = owner_user_id
        self.owner_clients = set(owner_clients)
        self.eligible_user_ids = set(eligible_user_ids)
        # The pinned Operator bot's Zulip user id (0 when the host pins none).  It is not an eligible
        # sender, but it posts a seat's route page (daemon.handle_route), and that page, and only that
        # page, must still reach the seat it pages: see `classify`.
        self.operator_user_id = operator_user_id
        self.is_bot = is_bot
        self.now = now
        self.stale_after = stale_after
        self.presence = presence
        self.muted = set(muted)
        self.followed = set(followed)


class LeaseView:
    """A live lease as the router sees it (the daemon decides liveness and wake capability)."""

    def __init__(
        self,
        lease_id: str,
        *,
        platform: str,
        topics: Iterable[str],
        mentions: bool,
        wake_capable: bool,
        posted: Iterable[int],
        prefix: str | None,
        last_prompt: float = 0.0,
        created: float = 0.0,
        room: Mapping[str, Any] | None = None,
    ) -> None:
        self.lease_id = lease_id
        self.platform = platform
        self.topics = set(topics)
        self.mentions = mentions
        self.wake_capable = wake_capable
        self.posted = set(posted)
        self.prefix = prefix  # "[SEAT·tag" of the session, for own posts
        self.last_prompt = last_prompt
        self.created = created
        self.room = room  # callable(topic_key, owner) -> reason or None; None means always room

    def has_room(self, key: str, owner: bool) -> str | None:
        return self.room(key, owner) if callable(self.room) else None

    def posted_this(self, message: Mapping[str, Any]) -> bool:
        if message.get("id") in self.posted:
            return True
        content = str(message.get("content") or "")
        return bool(self.prefix) and any(
            content.startswith(self.prefix + end) for end in ("]", "→", "->")
        )


class Classes:
    def __init__(self) -> None:
        self.own = self.owner = self.owner_api = self.eligible = self.direct = False
        self.broadcast_all = False
        self.everyone_inert = False
        self.peer_broadcast = False
        self.fleet = self.wildcard = self.dm = self.wake_tag = self.stale = self.sender_is_bot = (
            False
        )
        self.route_tag = False
        self.no_wake = False
        self.owner_topic = self.owner_topic_peer = self.owner_note = False
        self.reply_to: int | None = None
        self.channel = ""
        self.topic = ""
        self.key = ""

    def labels(self) -> list[str]:
        return [
            name
            for name in (
                "own",
                "owner",
                "owner_api",
                "eligible",
                "direct",
                "fleet",
                "broadcast_all",
                "everyone_inert",
                "peer_broadcast",
                "wildcard",
                "dm",
                "wake_tag",
                "route_tag",
                "no_wake",
                "stale",
                "owner_topic",
                "owner_topic_peer",
                "owner_note",
            )
            if getattr(self, name)
        ]


def classify(message: Mapping[str, Any], me: SeatIdentity, ctx: Context) -> Classes:
    c = Classes()
    sender_id = message.get("sender_id")
    content = str(message.get("content") or "")
    # The owner's per-message no-wake signal.  The marker comes off the content first, so the
    # anchored tag regexes below still match where they did before it was stripped.  The sender's
    # own seat tag is what makes a bare `[MM] ` prefix a seat tag rather than an ordinary word.
    content, c.no_wake = strip_no_wake(content, _sender_seat_tag(message))
    flags = list(message.get("flags") or [])
    c.dm = message.get("type") == "private"
    if c.dm:
        others = sorted(
            r.get("id") for r in message.get("display_recipient") or [] if isinstance(r, dict)
        )
        c.channel, c.topic = "DM", ",".join(str(i) for i in others)
    else:
        recipient = message.get("display_recipient")
        c.channel = recipient if isinstance(recipient, str) else ""
        c.topic = str(
            message.get("subject")
            if message.get("subject") is not None
            else message.get("topic") or ""
        )
    c.key = topic_key(c.channel, c.topic)
    c.own = sender_id == me.user_id
    from_owner_account = ctx.owner_user_id > 0 and sender_id == ctx.owner_user_id
    c.owner = (
        from_owner_account and str(message.get("client") or "") in ctx.owner_clients and not c.own
    )
    c.owner_api = from_owner_account and not c.owner
    c.sender_is_bot = bool(ctx.is_bot.get(sender_id, True)) if isinstance(sender_id, int) else True
    # The daemon's route page is posted by the OPERATOR now, not by the paging seat's bot (owner
    # ruling, Oct 30 2026), and the Operator is not an eligible sender, so without this the page it
    # relays would land in the target's inbox and wake no one.  The exception is exactly one shape: a
    # message from the pinned Operator bot (its user id, known to be a bot) whose first line is a
    # `·route` tag.  Anything else the Operator posts, its status messages and questions included,
    # stays ineligible, and a `·route` post is no wider than the one a peer bot could already write.
    from_operator_route = (
        isinstance(sender_id, int)
        and not isinstance(sender_id, bool)
        and ctx.operator_user_id > 0
        and sender_id == ctx.operator_user_id
        and ctx.is_bot.get(sender_id) is True
        and _ROUTE_TAG_RE.match(content) is not None
    )
    c.eligible = not c.own and (
        c.owner
        or (
            isinstance(sender_id, int)
            and sender_id in ctx.eligible_user_ids
            and not from_owner_account
        )
        or from_operator_route
    )
    c.owner_topic = not c.dm and is_owner_topic(c.topic)
    if c.owner_topic and c.eligible and not c.owner:
        # Only CLAUDE's bot may wake a seat here, and only with the notifier's tag:  the sender's email is the
        # server's, not the message's, so a peer cannot copy it, and the tag alone (which anyone can type) is not enough.
        c.owner_note = (
            c.sender_is_bot
            and _NOTE_TAG_RE.match(content) is not None
            and seat_tag_for({"is_bot": True, "email": message.get("sender_email")})
            == NOTE_SENDER_SEAT
        )
        if not c.owner_note:
            c.owner_topic_peer = True
            c.eligible = False
    visible = outside_code(content)
    names = ("@**%s**" % me.full_name, "@**%s|%d**" % (me.full_name, me.user_id))
    c.direct = "mentioned" in flags and any(n in visible for n in names)
    tokens = _broadcast_tokens_visible(visible)  # `visible` is already outside_code(content)
    # `@all`, `@channel` and `@stream` count exactly like `@all` (owner, Oct 27 2026).  `@everyone`
    # is inert on its own:  it lands unread in every bot's inbox and wakes nobody.
    c.broadcast_all = bool(tokens & BROADCAST_TOKENS)
    c.everyone_inert = "everyone" in tokens and not c.broadcast_all
    # A fleet bot's `@all` (owner, Oct 27 2026):  it wakes like the owner's, under peer budgets,
    # the loop guard and the stale rule.  The owner is not a peer broadcast, and a message in a DM
    # is not a broadcast at all.  An OWNER topic is excluded by the eligibility rule above:  a
    # peer's post there is never eligible, so it never becomes a peer broadcast either.
    c.peer_broadcast = bool(c.eligible and not c.owner and c.broadcast_all and not c.dm)
    c.fleet = c.key == FLEET_WAKE_KEY and c.broadcast_all
    c.wildcard = any(f in flags for f in WILDCARD_FLAGS)
    c.wake_tag = bool(_WAKE_TAG_RE.match(content))
    c.route_tag = bool(_ROUTE_TAG_RE.match(content))
    ts = message.get("timestamp")
    c.stale = isinstance(ts, (int, float)) and ctx.now - float(ts) > ctx.stale_after
    first_line = content.split("\n", 1)[0]
    match = _REPLY_TO_RE.search(first_line)
    c.reply_to = int(match.group(1)) if match else None
    return c


class Decision:
    def __init__(self) -> None:
        self.drop_reason: str | None = None
        self.lease_items: list[tuple[str, str, str]] = []  # (lease id, class, kind)
        self.seat_inbox = False
        self.delivered_to: str | None = None
        self.wake: str | None = None  # "owner" or "peer" when the prefilter says wake
        self.wake_block: str | None = None  # why a seat-inbox item does not wake
        self.wake_worthy = False  # the prefilter's verdict, whatever the route
        self.wake_owner = False
        self.throttled: list[tuple[str, str]] = []  # (lease id, reason) for interrupts made passive

    def describe(self) -> dict[str, Any]:
        return {
            "drop": self.drop_reason,
            "leases": [[l, c, k] for l, c, k in self.lease_items],
            "inbox": self.seat_inbox,
            "delivered_to": self.delivered_to,
            "wake": self.wake,
            "wake_block": self.wake_block,
        }


def prefilter(c: Classes) -> tuple[str | None, str | None]:
    """(wake kind, reason not).  Wake kind is 'owner' or 'peer'."""
    if c.own:
        return None, "own"
    if not c.eligible:
        return None, "owner_api" if c.owner_api else (
            "owner_topic" if c.owner_topic_peer else "not_eligible"
        )
    if c.wake_tag:
        return None, "wake_tag"
    owner_broadcast = (c.fleet or c.broadcast_all) and not c.owner_topic
    if c.owner and (c.direct or c.dm or owner_broadcast):
        return "owner", None
    if c.direct or c.dm:
        # An eligible bot's DM wakes like a direct mention (owner 2026-10-09).
        return (None, "stale") if c.stale else ("peer", None)
    if c.peer_broadcast and not c.owner_topic:
        # A fleet bot's `@all` wakes like the owner's (owner, Oct 27 2026):  the same stale rule,
        # the same loop guard, the peer budgets.  It is still peer data, never owner authority.
        return (None, "stale") if c.stale else ("peer", None)
    if c.fleet or c.wildcard:
        return None, "group_or_wildcard"
    return None, "not_a_mention"


def route(
    message: Mapping[str, Any], me: SeatIdentity, ctx: Context, leases: list[LeaseView]
) -> tuple[Classes, Decision]:
    c = classify(message, me, ctx)
    d = Decision()
    kind, block = prefilter(c)
    d.wake_worthy = kind is not None
    d.wake_owner = kind == "owner"
    if c.no_wake:
        # The owner's per-message signal:  the message stays readable in Zulip and no seat is
        # woken by it.  The gate is before the lease, the seat inbox and the wake branch, so a
        # marker message never reaches a seat.
        d.drop_reason = "no_wake"
        return c, d
    if not c.dm and c.key in ctx.muted and not c.direct:
        d.drop_reason = "muted"
        return c, d
    on_topic = [l for l in leases if not c.dm and c.key in l.topics]
    if c.own:
        for lease in on_topic:
            if not lease.posted_this(message):
                d.lease_items.append((lease.lease_id, PASSIVE, "sibling"))
        if not d.lease_items:
            d.drop_reason = "own"
        return c, d
    if on_topic:
        presence = is_presence(c.channel, c.topic, ctx.presence)
        for lease in on_topic:
            wants = c.eligible and (
                (c.owner and not c.everyone_inert)
                or c.direct
                or (not c.everyone_inert and c.reply_to is not None and c.reply_to in lease.posted)
                # A fleet bot's `@all` interrupts a leased topic like the owner's (owner,
                # Oct 27 2026):  the lease on the topic is woken, under the peer budgets.
                or (c.peer_broadcast and not c.owner_topic)
            )
            klass = PASSIVE
            if wants and not presence:
                reason = lease.has_room(c.key, c.owner)
                if reason is None:
                    klass = INTERRUPT
                else:
                    d.throttled.append((lease.lease_id, reason))
            d.lease_items.append((lease.lease_id, klass, "message"))
        return c, d
    mention_class = c.eligible and (
        c.direct
        or c.dm
        or (c.owner and c.fleet)
        # The owner's and a fleet bot's channel-wide mention (owner, Oct 27 2026):  both take a
        # claude-code lease with `mentions`, and the seat-inbox copy is marked delivered_to.
        or (c.eligible and c.broadcast_all and not c.owner_topic)
    )
    if mention_class:
        candidates = [
            l
            for l in leases
            if l.platform == "claude-code"
            and l.mentions
            and l.wake_capable
            and l.has_room(c.key, c.owner) is None
        ]
        if candidates:
            chosen = max(candidates, key=lambda l: (l.last_prompt, l.created))
            d.lease_items.append((chosen.lease_id, INTERRUPT, "message"))
            d.seat_inbox = True
            d.delivered_to = chosen.lease_id
            d.wake_block = "delivered_to_lease"
            return c, d
    if (
        c.direct
        or c.dm
        or c.fleet
        or c.broadcast_all
        or c.owner
        or c.wildcard
        or c.key in ctx.followed
    ):
        d.seat_inbox = True
        d.wake, d.wake_block = kind, block
        return c, d
    d.drop_reason = "chatter"
    return c, d
