"""Routing for the listener daemon:  classify one message for one seat and decide where it goes.

Pure functions over plain data, no I/O, so every rule is unit-tested directly.  The daemon owns
dedupe (cursor and ring), the files, the coalescing and the budgets.

Steps (design section 2), for each (seat, message) pair:

  1. Drop a muted topic unless the message is `direct` or a DM.
  2. Classify:  own, owner, eligible, direct, fleet, wildcard, dm, wake_tag, stale, reply_to.
     `fleet` is the fleet-wide wake:  a stream-wide wildcard (`@**all**`) in #agent-sync > `fleet`
     (owner 2026-10-09: there is no `fleet` user group and none will be made).  A wildcard anywhere
     else is only `wildcard`.
  3. Own posts go to other leases on the topic as passive sibling items.  Never inbox, never wake.
  4. A leased topic:  every live lease on it gets the item, interrupt or passive.  Done.
  5. A mention-class item in an unleased topic goes to the one live, wake-capable claude-code
     lease with `mentions` (latest owner prompt, then newest), and the seat-inbox copy is marked
     delivered_to.  No headless wake follows.
  6. Otherwise the seat inbox, if direct, dm, fleet, owner, wildcard or a followed topic.
  7. The wake prefilter, for seat-inbox items only.

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
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .identity import seat_tag_for
from .live import is_presence, topic_key

INTERRUPT = "interrupt"
PASSIVE = "passive"
WILDCARD_FLAGS = ("wildcard_mentioned", "stream_wildcard_mentioned", "topic_wildcard_mentioned")
# The one place a wildcard is the fleet-wide wake (owner 2026-10-09).
FLEET_WAKE_CHANNEL = "agent-sync"
FLEET_WAKE_TOPIC = "fleet"
FLEET_WAKE_KEY = topic_key(FLEET_WAKE_CHANNEL, FLEET_WAKE_TOPIC)
_FLEET_WAKE_RE = re.compile(r"(?<![\\\w])@\*\*all\*\*")
_WAKE_TAG_RE = re.compile(r"^\s*\[[A-Za-z0-9_-]+·wake\b")
_REPLY_TO_RE = re.compile(r"(?:^|\s)re=(\d+)\b")
_CODE_BLOCK_RE = re.compile(r"(?ms)^(```|~~~).*?(^\1\s*$|\Z)")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")


def fleet_wake_mention(content_outside_code: str, flags: Iterable[str]) -> bool:
    """True when the message carries a stream-wide wildcard:  Zulip's `stream_wildcard_mentioned` flag
    (or the legacy `wildcard_mentioned` without a topic-only flag), or a literal `@**all**` outside
    code and quotes.  `@**topic**` reaches only the topic's participants, so it never counts."""
    flag_set = set(flags)
    if "stream_wildcard_mentioned" in flag_set:
        return True
    if "wildcard_mentioned" in flag_set and "topic_wildcard_mentioned" not in flag_set:
        return True
    return bool(_FLEET_WAKE_RE.search(content_outside_code))


def outside_code(content: str) -> str:
    """Content without code blocks, code spans and quoted lines, for raw mention checks."""
    text = _CODE_BLOCK_RE.sub("", content)
    text = _CODE_SPAN_RE.sub("", text)
    return _QUOTE_LINE_RE.sub("", text)


GENERIC_BOT = 1  # Zulip bot_type:  2 incoming webhook, 3 outgoing webhook, 4 embedded


def fleet_bot_ids(users: Mapping[int, Mapping[str, Any]], tags: Iterable[str], owner_user_id: int) -> set[int]:
    """User ids of the fleet bots in a realm user list:  active generic bots that the owner owns
    (`bot_owner_id`) and whose email maps to one of `tags` (config.fleet_tags).  Webhook and embedded
    bots never count, whatever their email, and neither does a person.  The owner check is the trust
    anchor:  a bot someone else creates under a fleet-looking email is not a fleet bot, and with no
    owner pinned there are none."""
    wanted = set(tags)
    found: set[int] = set()
    if not isinstance(owner_user_id, int) or owner_user_id <= 0:
        return found
    for uid, user in users.items():
        if not isinstance(uid, int) or isinstance(uid, bool) or user.get("is_bot") is not True:
            continue
        if user.get("is_active", True) is False or user.get("bot_type", GENERIC_BOT) != GENERIC_BOT:
            continue
        if user.get("bot_owner_id") != owner_user_id:
            continue
        if seat_tag_for(user) in wanted:
            found.add(uid)
    return found


class SeatIdentity:
    def __init__(self, seat: str, user_id: int, full_name: str, email: str) -> None:
        self.seat = seat
        self.user_id = user_id
        self.full_name = full_name
        self.email = email


class Context:
    """What the router needs to know beyond the message.  `is_bot` maps user ids to the bot flag;
    an unknown sender counts as a bot."""

    def __init__(self, *, owner_user_id: int, owner_clients: Iterable[str], eligible_user_ids: Iterable[int],
                 is_bot: Mapping[int, bool], now: float, stale_after: float,
                 presence: list[tuple[str, str]], muted: Iterable[str] = (), followed: Iterable[str] = ()) -> None:
        self.owner_user_id = owner_user_id
        self.owner_clients = set(owner_clients)
        self.eligible_user_ids = set(eligible_user_ids)
        self.is_bot = is_bot
        self.now = now
        self.stale_after = stale_after
        self.presence = presence
        self.muted = set(muted)
        self.followed = set(followed)


class LeaseView:
    """A live lease as the router sees it (the daemon decides liveness and wake capability)."""

    def __init__(self, lease_id: str, *, platform: str, topics: Iterable[str], mentions: bool,
                 wake_capable: bool, posted: Iterable[int], prefix: str | None, last_prompt: float = 0.0,
                 created: float = 0.0, room: Mapping[str, Any] | None = None) -> None:
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
        return bool(self.prefix) and any(content.startswith(self.prefix + end) for end in ("]", "→", "->"))


class Classes:
    def __init__(self) -> None:
        self.own = self.owner = self.owner_api = self.eligible = self.direct = False
        self.fleet = self.wildcard = self.dm = self.wake_tag = self.stale = self.sender_is_bot = False
        self.reply_to: int | None = None
        self.channel = ""
        self.topic = ""
        self.key = ""

    def labels(self) -> list[str]:
        return [name for name in ("own", "owner", "owner_api", "eligible", "direct", "fleet", "wildcard", "dm",
                                  "wake_tag", "stale") if getattr(self, name)]


def classify(message: Mapping[str, Any], me: SeatIdentity, ctx: Context) -> Classes:
    c = Classes()
    sender_id = message.get("sender_id")
    content = str(message.get("content") or "")
    flags = list(message.get("flags") or [])
    c.dm = message.get("type") == "private"
    if c.dm:
        others = sorted(r.get("id") for r in message.get("display_recipient") or [] if isinstance(r, dict))
        c.channel, c.topic = "DM", ",".join(str(i) for i in others)
    else:
        recipient = message.get("display_recipient")
        c.channel = recipient if isinstance(recipient, str) else ""
        c.topic = str(message.get("subject") if message.get("subject") is not None else message.get("topic") or "")
    c.key = topic_key(c.channel, c.topic)
    c.own = sender_id == me.user_id
    from_owner_account = ctx.owner_user_id > 0 and sender_id == ctx.owner_user_id
    c.owner = from_owner_account and str(message.get("client") or "") in ctx.owner_clients and not c.own
    c.owner_api = from_owner_account and not c.owner
    c.sender_is_bot = bool(ctx.is_bot.get(sender_id, True)) if isinstance(sender_id, int) else True
    c.eligible = not c.own and (c.owner or (isinstance(sender_id, int) and sender_id in ctx.eligible_user_ids
                                            and not from_owner_account))
    visible = outside_code(content)
    names = ("@**%s**" % me.full_name, "@**%s|%d**" % (me.full_name, me.user_id))
    c.direct = "mentioned" in flags and any(n in visible for n in names)
    c.fleet = c.key == FLEET_WAKE_KEY and fleet_wake_mention(visible, flags)
    c.wildcard = any(f in flags for f in WILDCARD_FLAGS)
    c.wake_tag = bool(_WAKE_TAG_RE.match(content))
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
        return {"drop": self.drop_reason, "leases": [[l, c, k] for l, c, k in self.lease_items],
                "inbox": self.seat_inbox, "delivered_to": self.delivered_to, "wake": self.wake,
                "wake_block": self.wake_block}


def prefilter(c: Classes) -> tuple[str | None, str | None]:
    """(wake kind, reason not).  Wake kind is 'owner' or 'peer'."""
    if c.own:
        return None, "own"
    if not c.eligible:
        return None, "owner_api" if c.owner_api else "not_eligible"
    if c.wake_tag:
        return None, "wake_tag"
    if c.owner and (c.direct or c.dm or c.fleet):
        return "owner", None
    if c.direct or c.dm:
        # An eligible bot's DM wakes like a direct mention (owner 2026-10-09).
        return (None, "stale") if c.stale else ("peer", None)
    if c.fleet or c.wildcard:
        return None, "group_or_wildcard"
    return None, "not_a_mention"


def route(message: Mapping[str, Any], me: SeatIdentity, ctx: Context, leases: list[LeaseView]) -> tuple[Classes, Decision]:
    c = classify(message, me, ctx)
    d = Decision()
    kind, block = prefilter(c)
    d.wake_worthy = kind is not None
    d.wake_owner = kind == "owner"
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
            wants = c.eligible and (c.owner or c.direct or (c.reply_to is not None and c.reply_to in lease.posted))
            klass = PASSIVE
            if wants and not presence:
                reason = lease.has_room(c.key, c.owner)
                if reason is None:
                    klass = INTERRUPT
                else:
                    d.throttled.append((lease.lease_id, reason))
            d.lease_items.append((lease.lease_id, klass, "message"))
        return c, d
    mention_class = c.eligible and (c.direct or c.dm or (c.owner and c.fleet))
    if mention_class:
        candidates = [l for l in leases if l.platform == "claude-code" and l.mentions and l.wake_capable
                      and l.has_room(c.key, c.owner) is None]
        if candidates:
            chosen = max(candidates, key=lambda l: (l.last_prompt, l.created))
            d.lease_items.append((chosen.lease_id, INTERRUPT, "message"))
            d.seat_inbox = True
            d.delivered_to = chosen.lease_id
            d.wake_block = "delivered_to_lease"
            return c, d
    if c.direct or c.dm or c.fleet or c.owner or c.wildcard or c.key in ctx.followed:
        d.seat_inbox = True
        d.wake, d.wake_block = kind, block
        return c, d
    d.drop_reason = "chatter"
    return c, d
