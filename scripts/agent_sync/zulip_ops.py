"""The Zulip operations beyond chat (AFC-330):  one message, edits, deletes, edit history, topic moves,
resolve and unresolve, reactions, read and star flags, search, channels, users, presence, status,
user groups, custom emoji, uploads, scheduled messages, drafts and typing.

The CLI commands and the stdio MCP tools both call these functions, so the two surfaces share one
implementation;  the hosted Worker ports the same rules (scripts/agent-sync-mcp/src/zulip-full.js).
Every function takes the CLI's `Agent` (identity already pinned, every request after users/me
gated on the bot check) and returns plain data.  Nothing here prints.

Safety rules that hold on every path:
  - Only this bot's own messages are edited (`own_message`).  A delete adds no rule of its own:
    Zulip's realm delete permissions decide (owner, Oct 29 2026).
  - Outbound text (edits, scheduled messages, drafts, status text, uploaded text) is secret-scanned
    by the caller before any request:  the CLI with `_read_text`, the MCP layer with its own scan.
  - The generic escape hatch (`simple-chat api`) reaches only `API_ALLOWLIST`, a fixed list of
    non-admin endpoints, and scans every string it sends.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import zulip as Z
from .identity import seat_tag_for
from .zulip import UsageError

RESOLVED_PREFIX = "✔ "
PROPAGATE_MODES = ("change_all", "change_later", "change_one")
VISIBILITY = {"mute": 1, "unmute": 0, "follow": 3, "unfollow": 0}
SEARCH_HAS = ("link", "attachment", "image", "reaction")
SEARCH_IS = ("starred", "unread", "mentioned", "resolved", "unresolved", "alerted", "followed")
FLAG_IDS_MAX = 20
FLAG_NARROW_MAX = 1000
STATUS_TEXT_MAX = 60
UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # Zulip Cloud's default max_file_upload_size_mib
PRESENCE_ACTIVE_S = (
    140  # Zulip's own thresholds:  active within 140 seconds, idle within 15 minutes
)
PRESENCE_IDLE_S = 15 * 60
# Zulip's own text is left out (it can quote member-authored names);  the hosted Worker says the same.
DELETE_REFUSED = (
    "Zulip refused to delete message %d:  this bot has no permission to delete it, or it is past"
    " the realm's delete time limit.  The owner sets both in the realm's settings."
)
_EMOJI_RE = re.compile(r"^[a-z0-9_+-]{1,60}$")

# The generic escape hatch (`simple-chat api`, CLI only).  Non-admin endpoints a member bot may call
# for itself; anything realm-wide, any other user's settings, bot management, invitations, exports
# and channel administration are left out on purpose.  The path is matched in full.
API_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "GET": (
        r"messages",
        r"messages/\d+",
        r"messages/\d+/history",
        r"messages/\d+/read_receipts",
        r"messages/matches_narrow",
        r"streams",
        r"streams/\d+",
        r"streams/\d+/members",
        r"streams/\d+/email_address",
        r"get_stream_id",
        r"users",
        r"users/\d+",
        r"users/me",
        r"users/me/subscriptions",
        r"users/me/subscriptions/\d+",
        r"users/me/\d+/topics",
        r"users/\d+/presence",
        r"users/\d+/status",
        r"realm/presence",
        r"users/me/alert_words",
        r"user_groups",
        r"user_groups/\d+/members",
        r"user_groups/\d+/members/\d+",
        r"realm/emoji",
        r"realm/linkifiers",
        r"scheduled_messages",
        r"drafts",
        r"attachments",
        r"server_settings",
    ),
    "POST": (
        r"messages",
        r"messages/\d+/reactions",
        r"messages/flags",
        r"messages/flags/narrow",
        r"messages/render",
        r"mark_stream_as_read",
        r"mark_topic_as_read",
        r"user_topics",
        r"users/me/status",
        r"users/me/subscriptions",
        r"users/me/subscriptions/properties",
        r"users/me/alert_words",
        r"scheduled_messages",
        r"drafts",
        r"typing",
    ),
    "PATCH": (r"messages/\d+", r"scheduled_messages/\d+", r"drafts/\d+"),
    "DELETE": (
        r"messages/\d+",
        r"messages/\d+/reactions",
        r"users/me/subscriptions",
        r"users/me/alert_words",
        r"scheduled_messages/\d+",
        r"drafts/\d+",
        r"attachments/\d+",
    ),
}


def api_allowed(method: str, path: str) -> bool:
    """Is `method path` on the escape hatch's allowlist?  The path is the part after /api/v1/."""
    clean = path.strip().strip("/")
    if not clean or ".." in clean or "?" in clean or "#" in clean or "//" in clean:
        return False
    return any(re.fullmatch(pattern, clean) for pattern in API_ALLOWLIST.get(method.upper(), ()))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------------


def get_message(agent: Any, message_id: int) -> dict[str, Any]:
    """One message, raw markdown, flags included.  Zulip answers only for messages this bot can see."""
    message = (
        agent.client.get("messages/%d" % message_id, {"apply_markdown": False}).get("message") or {}
    )
    if _int(message.get("id")) is None:
        message = dict(message, id=message_id)
    return message


def is_own(agent: Any, message: Mapping[str, Any]) -> bool:
    me = agent.me()
    if _int(message.get("sender_id")) is not None and _int(me.get("user_id")) is not None:
        return message.get("sender_id") == me.get("user_id")
    return agent.sent_by_this_bot(message)


def own_message(agent: Any, message_id: int) -> dict[str, Any]:
    """The message, when this bot sent it;  else a usage error (an edit touches only our own)."""
    message = get_message(agent, message_id)
    if not is_own(agent, message):
        raise UsageError(
            "message %d was not sent by this bot; only its own messages can be edited" % message_id
        )
    return message


def edit_message(
    agent: Any, message_id: int, body: str, *, checked: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Replace this bot's own message.  `checked` is the message a caller already read with
    `own_message`, so the ownership check is not fetched a second time."""
    if checked is None:
        own_message(agent, message_id)
    agent.client.patch("messages/%d" % message_id, {"content": body})
    return {"id": message_id, "edited": True}


def delete_message(agent: Any, message_id: int) -> dict[str, Any]:
    """Delete any message this bot can see.  The tools add no rule of their own (owner, Oct 29 2026):
    Zulip's delete permissions decide, so another sender's message, or one past the realm's delete
    time limit, is refused by Zulip until the owner grants this bot that right."""
    get_message(agent, message_id)
    try:
        agent.client.delete("messages/%d" % message_id)
    except Z.ApiError as exc:
        if exc.status != 400:
            raise
        # Zulip answers 400 for a refusal and for a message that is gone.  Only a message that is still
        # there was refused (permission or the time limit);  any other 400 is Zulip's own error.
        try:
            get_message(agent, message_id)
        except Z.ApiError:
            raise exc from None
        raise UsageError(DELETE_REFUSED % message_id) from None
    return {"id": message_id, "deleted": True}


def message_history(agent: Any, message_id: int) -> list[dict[str, Any]]:
    """Every saved version of a message, oldest first:  who, when, topic, content (raw markdown)."""
    rows = agent.client.get("messages/%d/history" % message_id).get("message_history") or []
    out = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        out.append(
            {
                "user_id": _int(row.get("user_id")),
                "timestamp": row.get("timestamp"),
                "topic": row.get("topic"),
                "prev_topic": row.get("prev_topic"),
                "content": row.get("content"),
                "prev_content": row.get("prev_content"),
                "stream": _int(row.get("stream")),
                "prev_stream": _int(row.get("prev_stream")),
            }
        )
    return out


def resolved_channel(agent: Any, channel: str) -> str:
    """The live channel a name refers to, resolved with the runtime's own environment.

    The narrow names the channel and so does the guard that checks an anchor against it,
    so both halves of a command have to resolve the same name.
    """
    return Z.resolve_channel(channel, getattr(getattr(agent, "rt", None), "env", None))


def newest_in_topic(agent: Any, channel: str, topic: str) -> dict[str, Any]:
    # The narrow names the channel, so a retired name has to be resolved before it
    # becomes an operand:  otherwise the lookup reads the tombstone stream.
    channel = resolved_channel(agent, channel)
    newest = agent.fetch(
        [{"operator": "channel", "operand": channel}, {"operator": "topic", "operand": topic}],
        anchor="newest",
        num_before=1,
    )
    if not newest:
        raise UsageError("no messages found in #%s › %s" % (channel, topic))
    return max(newest, key=lambda m: m["id"])


def check_topic(topic: str, field: str = "topic") -> str:
    topic = topic.strip()
    if not topic:
        raise UsageError("the %s must not be empty" % field)
    if len(topic) > Z.MAX_TOPIC_LENGTH:
        raise UsageError(
            "the %s is %d characters; Zulip allows at most %d"
            % (field, len(topic), Z.MAX_TOPIC_LENGTH)
        )
    return topic


def move_topic(
    agent: Any,
    channel: str,
    topic: str,
    *,
    new_topic: str | None = None,
    new_channel: str | None = None,
    mode: str = "change_all",
    message_id: int | None = None,
    notify_old: bool = False,
    notify_new: bool = False,
) -> dict[str, Any]:
    """Rename a topic, move it to another channel, or both.  `mode` picks which messages move:  the
    whole topic, the anchor and later, or the anchor alone.  The anchor is `message_id` (it must be
    in that topic) or the topic's newest message."""
    if mode not in PROPAGATE_MODES:
        raise UsageError("the propagate mode must be one of %s" % ", ".join(PROPAGATE_MODES))
    if new_topic is None and new_channel is None:
        raise UsageError("nothing to move:  give a new topic, a new channel or both")
    if new_topic is not None:
        new_topic = check_topic(new_topic, "new topic")
    # The anchor guard below and the lookup it falls back to have to name the same
    # channel, so the name is resolved once, here.
    channel = resolved_channel(agent, channel)
    if message_id is not None:
        anchor = get_message(agent, message_id)
        if (
            anchor.get("type") != "stream"
            or Z.message_channel(anchor).casefold() != channel.casefold()
            or Z.message_topic(anchor).casefold() != topic.casefold()
        ):
            raise UsageError("message %d is not in #%s › %s" % (message_id, channel, topic))
    else:
        anchor = newest_in_topic(agent, channel, topic)
    params: dict[str, Any] = {
        "propagate_mode": mode,
        "send_notification_to_old_thread": notify_old,
        "send_notification_to_new_thread": notify_new,
    }
    if new_topic is not None:
        params["topic"] = new_topic
    if new_channel is not None:
        params["stream_id"] = agent.stream_id(new_channel)
    agent.client.patch("messages/%d" % anchor["id"], params)
    return {
        "id": anchor["id"],
        "channel_id": params.get("stream_id") or anchor.get("stream_id"),
        "propagate_mode": mode,
    }


def resolved_name(topic: str, resolve: bool) -> str:
    marked = topic.startswith(RESOLVED_PREFIX.strip())
    if resolve:
        if marked:
            raise UsageError("topic %r is already resolved" % topic)
        new_topic = RESOLVED_PREFIX + topic
        if len(new_topic) > Z.MAX_TOPIC_LENGTH:
            raise UsageError(
                "the resolved topic would be %d characters; Zulip allows at most %d"
                % (len(new_topic), Z.MAX_TOPIC_LENGTH)
            )
        return new_topic
    if not marked:
        raise UsageError("topic %r is not resolved" % topic)
    new_topic = topic[len(RESOLVED_PREFIX.strip()) :].lstrip()
    if not new_topic:
        raise UsageError("topic %r has no name once the check mark is removed" % topic)
    return new_topic


def set_resolved(agent: Any, channel: str, topic: str, resolve: bool) -> dict[str, Any]:
    """Resolve (prefix the check mark) or unresolve (drop it) a whole topic, by its real spelling."""
    channel = resolved_channel(agent, channel)
    if resolve:
        resolved_name(topic, True)  # refuse the obvious cases before any request
        latest = newest_in_topic(agent, channel, topic)
    elif topic.startswith(RESOLVED_PREFIX.strip()):
        latest = newest_in_topic(agent, channel, topic)
    else:
        # Unresolving by the plain name means the topic as resolve left it:  the
        # check-marked spelling.  Fall back to the name as given.
        try:
            latest = newest_in_topic(agent, channel, RESOLVED_PREFIX + topic)
        except UsageError:
            latest = newest_in_topic(agent, channel, topic)
    new_topic = resolved_name(Z.message_topic(latest), resolve)
    agent.client.patch(
        "messages/%d" % latest["id"], {"topic": new_topic, "propagate_mode": "change_all"}
    )
    return {"id": latest["id"], "channel_id": latest.get("stream_id"), "topic": new_topic}


def check_emoji(emoji: str) -> str:
    name = emoji.strip().strip(":")
    if not _EMOJI_RE.fullmatch(name):
        raise UsageError("the emoji name may use a-z, 0-9, _, + and - only")
    return name


def unreact(agent: Any, message_id: int, emoji: str) -> dict[str, Any]:
    """Remove this bot's reaction.  A reaction that is not there is a success, so a retry is safe."""
    name = check_emoji(emoji)
    try:
        agent.client.delete("messages/%d/reactions" % message_id, {"emoji_name": name})
    except Z.ApiError as exc:
        if exc.code != "REACTION_DOES_NOT_EXIST":
            raise
    return {"id": message_id, "emoji": name}


def flag_messages(
    agent: Any,
    flag: str,
    add: bool,
    *,
    message_ids: Sequence[int] | None = None,
    narrow: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add or remove `flag` (read or starred) on message ids, or on every message of a narrow (a
    channel, or a channel and topic), at most FLAG_NARROW_MAX at a time.  Returns {updated}."""
    op = "add" if add else "remove"
    if message_ids:
        ids = sorted(set(int(i) for i in message_ids))
        if len(ids) > FLAG_IDS_MAX:
            raise UsageError("at most %d message ids at a time" % FLAG_IDS_MAX)
        result = agent.client.post("messages/flags", {"messages": ids, "op": op, "flag": flag})
        return {"updated": len(result.get("messages") or [])}
    if not narrow:
        raise UsageError("give message ids, or a channel (and optionally a topic)")
    result = agent.client.post(
        "messages/flags/narrow",
        {
            "narrow": narrow,
            "anchor": "oldest",
            "include_anchor": True,
            "num_before": 0,
            "num_after": FLAG_NARROW_MAX,
            "op": op,
            "flag": flag,
        },
    )
    return {
        "updated": _int(result.get("updated_count")) or 0,
        "complete": bool(result.get("found_newest", True)),
    }


# --------------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------------


def search_narrow(
    *,
    query: str | None = None,
    channel: Any = None,
    topic: str | None = None,
    sender: Any = None,
    has: Iterable[str] = (),
    is_: str | None = None,
    dm_with: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """A Zulip narrow from search fields.  `channel` is a name or a stream id;  `dm_with` the other
    people's user ids.  A channel and dm_with together are refused."""
    narrow: list[dict[str, Any]] = []
    if channel is not None and dm_with:
        raise UsageError("a search names a channel or a direct-message conversation, not both")
    if topic is not None and channel is None:
        raise UsageError("a topic search needs its channel")
    if channel is not None:
        narrow.append({"operator": "channel", "operand": channel})
    if topic is not None:
        narrow.append({"operator": "topic", "operand": topic})
    if dm_with:
        narrow.append({"operator": "dm", "operand": sorted(set(int(i) for i in dm_with))})
    if sender is not None:
        narrow.append({"operator": "sender", "operand": sender})
    for item in has:
        if item not in SEARCH_HAS:
            raise UsageError("has must be one of %s" % ", ".join(SEARCH_HAS))
        narrow.append({"operator": "has", "operand": item})
    if is_ is not None:
        if is_ not in SEARCH_IS:
            raise UsageError("is must be one of %s" % ", ".join(SEARCH_IS))
        if is_ == "unresolved":
            narrow.append({"operator": "is", "operand": "resolved", "negated": True})
        else:
            narrow.append({"operator": "is", "operand": is_})
    if query:
        narrow.append({"operator": "search", "operand": query})
    if not narrow:
        raise UsageError(
            "a search needs at least one term:  words, a channel, a sender, has, is or dm-with"
        )
    return narrow


def search(
    agent: Any,
    narrow: list[dict[str, Any]],
    *,
    limit: int,
    since_id: int | None = None,
    near: int | None = None,
) -> list[dict[str, Any]]:
    """Messages matching `narrow`, oldest first:  the newest `limit`, or `limit` after since_id, or
    about `limit` around `near`."""
    if since_id is not None:
        return agent.fetch(narrow, anchor=since_id, num_after=limit, include_anchor=False)
    if near is not None:
        half = max(1, limit // 2)
        return agent.fetch(
            narrow, anchor=near, num_before=half, num_after=limit - half, include_anchor=True
        )
    return agent.fetch(narrow, anchor="newest", num_before=limit)


# --------------------------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------------------------


def channels(agent: Any, *, include_all: bool = False) -> list[dict[str, Any]]:
    """Subscribed channels, or (include_all) every channel this bot can see, public ones included.
    Each row:  stream_id, name, description, public, subscribed."""
    subs = agent.client.get("users/me/subscriptions").get("subscriptions") or []
    subscribed = {_int(s.get("stream_id")) for s in subs if isinstance(s, Mapping)}
    rows = subs
    if include_all:
        rows = (
            agent.client.get("streams", {"include_public": True, "include_subscribed": True}).get(
                "streams"
            )
            or []
        )
    out = []
    for s in rows:
        if (
            not isinstance(s, Mapping)
            or _int(s.get("stream_id")) is None
            or s.get("is_archived") is True
        ):
            continue
        out.append(
            {
                "stream_id": s["stream_id"],
                "name": s.get("name"),
                "description": s.get("description"),
                "public": s.get("invite_only") is False,
                "subscribed": s["stream_id"] in subscribed,
            }
        )
    return sorted(out, key=lambda r: str(r.get("name") or "").casefold())


def channel_info(agent: Any, channel: str) -> dict[str, Any]:
    stream_id = agent.stream_id(channel)
    stream = agent.client.get("streams/%d" % stream_id).get("stream") or {}
    members = agent.client.get("streams/%d/members" % stream_id).get("subscribers") or []
    return {
        "stream_id": stream_id,
        "name": stream.get("name"),
        "description": stream.get("description"),
        "public": stream.get("invite_only") is False,
        "subscriber_count": len(members),
        "history_public_to_subscribers": stream.get("history_public_to_subscribers"),
        "date_created": stream.get("date_created"),
    }


def channel_members(agent: Any, channel: str) -> list[dict[str, Any]]:
    stream_id = agent.stream_id(channel)
    ids = [
        i
        for i in agent.client.get("streams/%d/members" % stream_id).get("subscribers") or []
        if _int(i) is not None
    ]
    by_id = {u.get("user_id"): u for u in agent.users()}
    return [user_row(by_id.get(i) or {"user_id": i}) for i in sorted(ids)]


def unsubscribe(agent: Any, channel: str) -> dict[str, Any]:
    stream_id = agent.stream_id(channel)
    result = agent.client.delete("users/me/subscriptions", {"subscriptions": [channel]})
    return {"channel_id": stream_id, "removed": bool(result.get("removed"))}


def topic_visibility(agent: Any, channel: str, topic: str, visibility: str) -> dict[str, Any]:
    if visibility not in VISIBILITY:
        raise UsageError("visibility must be one of %s" % ", ".join(VISIBILITY))
    stream_id = agent.stream_id(channel)
    policy = VISIBILITY[visibility]
    agent.client.post(
        "user_topics", {"stream_id": stream_id, "topic": topic, "visibility_policy": policy}
    )
    return {"channel_id": stream_id, "visibility_policy": policy}


# --------------------------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------------------------


def user_row(user: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        "user_id": user.get("user_id"),
        "full_name": user.get("full_name"),
        "email": user.get("email"),
        "is_bot": user.get("is_bot"),
        "is_active": user.get("is_active", True),
        "role": user.get("role"),
    }
    if user.get("is_bot") and user.get("email"):
        row["seat"] = seat_tag_for(user)
    return row


def find_users(
    agent: Any,
    *,
    query: str | None = None,
    user_id: int | None = None,
    email: str | None = None,
    include_inactive: bool = False,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Users by id, by email, or whose name, email or seat tag contains `query` (case-insensitive)."""
    found = []
    needle = (query or "").strip().casefold()
    for user in agent.users():
        if not include_inactive and not user.get("is_active", True):
            continue
        if kind == "bots" and not user.get("is_bot"):
            continue
        if kind == "humans" and user.get("is_bot"):
            continue
        if user_id is not None and user.get("user_id") != user_id:
            continue
        if email is not None and email.strip().casefold() not in (
            str(user.get("email") or "").casefold(),
            str(user.get("delivery_email") or "").casefold(),
        ):
            continue
        if needle:
            hay = [
                str(user.get("full_name") or "").casefold(),
                str(user.get("email") or "").casefold(),
            ]
            if user.get("is_bot") and user.get("email"):
                hay.append(seat_tag_for(user).casefold())
            if not any(needle in h for h in hay):
                continue
        found.append(user_row(user))
    found.sort(key=lambda r: str(r.get("full_name") or "").casefold())
    return found[:limit]


def presence(agent: Any, user_id: int, now: float | None = None) -> dict[str, Any]:
    """One user's presence:  active, idle or offline, from the newest client timestamp."""
    data = agent.client.get("users/%d/presence" % user_id).get("presence") or {}
    stamps = [float(v.get("timestamp") or 0) for v in data.values() if isinstance(v, Mapping)]
    newest = max(stamps, default=0.0)
    age = (time.time() if now is None else now) - newest
    status = (
        "active"
        if newest and age <= PRESENCE_ACTIVE_S
        else "idle"
        if newest and age <= PRESENCE_IDLE_S
        else "offline"
    )
    return {"user_id": user_id, "status": status, "timestamp": int(newest) if newest else None}


def set_status(
    agent: Any,
    *,
    status_text: str | None = None,
    emoji_name: str | None = None,
    away: bool | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if status_text is not None:
        if len(status_text) > STATUS_TEXT_MAX:
            raise UsageError(
                "the status text is %d characters; Zulip allows at most %d"
                % (len(status_text), STATUS_TEXT_MAX)
            )
        params["status_text"] = status_text
    if emoji_name is not None:
        params["emoji_name"] = check_emoji(emoji_name) if emoji_name else ""
    if away is not None:
        params["away"] = away
    if not params:
        raise UsageError("nothing to set:  give a status text, an emoji or away")
    agent.client.post("users/me/status", params)
    return {"updated": True}


def user_groups(agent: Any) -> list[dict[str, Any]]:
    rows = agent.client.get("user_groups").get("user_groups") or []
    return [
        {
            "id": g.get("id"),
            "name": g.get("name"),
            "description": g.get("description"),
            "members": [m for m in g.get("members") or [] if _int(m) is not None],
            "system": bool(g.get("is_system_group")),
        }
        for g in rows
        if isinstance(g, Mapping) and _int(g.get("id")) is not None
    ]


def custom_emoji(agent: Any, *, include_deactivated: bool = False) -> list[dict[str, Any]]:
    rows = (agent.client.get("realm/emoji").get("emoji") or {}).values()
    out = []
    for e in rows:
        if not isinstance(e, Mapping) or (e.get("deactivated") and not include_deactivated):
            continue
        try:
            emoji_id = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "id": emoji_id,
                "name": e.get("name"),
                "deactivated": bool(e.get("deactivated")),
                "author_id": _int(e.get("author_id")),
            }
        )
    return sorted(out, key=lambda r: str(r.get("name") or ""))


# --------------------------------------------------------------------------------------------
# Uploads, scheduled messages, drafts, typing
# --------------------------------------------------------------------------------------------


def upload(
    agent: Any, filename: str, data: bytes, content_type: str = "application/octet-stream"
) -> dict[str, Any]:
    if len(data) > UPLOAD_MAX_BYTES:
        raise UsageError(
            "the file is %d bytes; uploads are at most %d" % (len(data), UPLOAD_MAX_BYTES)
        )
    if not data:
        raise UsageError("the file is empty")
    result = agent.client.upload(filename, data, content_type)
    url = str(result.get("url") or result.get("uri") or "")
    return {
        "url": url,
        "filename": result.get("filename") or filename,
        "bytes": len(data),
        "markdown": "[%s](%s)" % (filename, url),
    }


def target_params(
    agent: Any, *, channel: str | None, topic: str | None, user_ids: Sequence[int] | None
) -> dict[str, Any]:
    """{type, to, topic} for a scheduled message or a draft:  a channel and topic, or a DM."""
    if user_ids:
        if topic is not None:
            raise UsageError("a direct message has no topic:  give user ids or a topic, not both")
        return {"type": "direct", "to": sorted(set(int(i) for i in user_ids))}
    if topic is None:
        raise UsageError("a channel message needs a topic")
    env = getattr(getattr(agent, "rt", None), "env", None)
    return {
        "type": "stream",
        "to": agent.stream_id(channel or Z.default_channel(env)),
        "topic": check_topic(topic),
    }


def scheduled_list(agent: Any) -> list[dict[str, Any]]:
    rows = agent.client.get("scheduled_messages").get("scheduled_messages") or []
    return [
        {
            "id": r.get("scheduled_message_id"),
            "type": r.get("type"),
            "to": r.get("to"),
            "topic": r.get("topic"),
            "content": r.get("content"),
            "deliver_at": r.get("scheduled_delivery_timestamp"),
            "failed": bool(r.get("failed")),
        }
        for r in rows
        if isinstance(r, Mapping) and _int(r.get("scheduled_message_id")) is not None
    ]


def check_deliver_at(deliver_at: int, now: float | None = None) -> int:
    if int(deliver_at) <= int(time.time() if now is None else now):
        raise UsageError("the delivery time must be in the future (unix seconds)")
    return int(deliver_at)


def scheduled_create(
    agent: Any, target: Mapping[str, Any], body: str, deliver_at: int
) -> dict[str, Any]:
    params = dict(target, content=body, scheduled_delivery_timestamp=check_deliver_at(deliver_at))
    result = agent.client.post("scheduled_messages", params)
    return {"id": int(result["scheduled_message_id"]), "deliver_at": int(deliver_at)}


def scheduled_edit(
    agent: Any,
    scheduled_id: int,
    *,
    body: str | None = None,
    topic: str | None = None,
    deliver_at: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if body is not None:
        params["content"] = body
    if topic is not None:
        params["topic"] = check_topic(topic)
    if deliver_at is not None:
        params["scheduled_delivery_timestamp"] = check_deliver_at(deliver_at)
    if not params:
        raise UsageError("nothing to change:  give new text, a topic or a delivery time")
    agent.client.patch("scheduled_messages/%d" % scheduled_id, params)
    return {"id": scheduled_id}


def scheduled_delete(agent: Any, scheduled_id: int) -> dict[str, Any]:
    agent.client.delete("scheduled_messages/%d" % scheduled_id)
    return {"id": scheduled_id, "deleted": True}


def drafts_list(agent: Any) -> list[dict[str, Any]]:
    rows = agent.client.get("drafts").get("drafts") or []
    return [
        {
            "id": r.get("id"),
            "type": r.get("type"),
            "to": r.get("to"),
            "topic": r.get("topic"),
            "content": r.get("content"),
            "timestamp": r.get("timestamp"),
        }
        for r in rows
        if isinstance(r, Mapping) and _int(r.get("id")) is not None
    ]


def _draft(target: Mapping[str, Any], body: str) -> dict[str, Any]:
    if target["type"] == "direct":
        return {
            "type": "private",
            "to": list(target["to"]),
            "topic": "",
            "content": body,
            "timestamp": int(time.time()),
        }
    return {
        "type": "stream",
        "to": [target["to"]],
        "topic": target["topic"],
        "content": body,
        "timestamp": int(time.time()),
    }


def draft_create(agent: Any, target: Mapping[str, Any], body: str) -> dict[str, Any]:
    result = agent.client.post("drafts", {"drafts": [_draft(target, body)]})
    ids = result.get("ids") or []
    return {"id": int(ids[0]) if ids else 0}


def draft_edit(agent: Any, draft_id: int, target: Mapping[str, Any], body: str) -> dict[str, Any]:
    agent.client.patch("drafts/%d" % draft_id, {"draft": _draft(target, body)})
    return {"id": draft_id}


def draft_delete(agent: Any, draft_id: int) -> dict[str, Any]:
    agent.client.delete("drafts/%d" % draft_id)
    return {"id": draft_id, "deleted": True}


def typing(agent: Any, op: str, target: Mapping[str, Any]) -> dict[str, Any]:
    if op not in ("start", "stop"):
        raise UsageError("op must be start or stop")
    if target["type"] == "direct":
        params: dict[str, Any] = {"type": "direct", "op": op, "to": list(target["to"])}
    else:
        params = {"type": "stream", "op": op, "stream_id": target["to"], "topic": target["topic"]}
    agent.client.post("typing", params)
    return {"op": op}


def encode_json_params(
    pairs: Iterable[tuple[str, str]], json_pairs: Iterable[tuple[str, str]]
) -> dict[str, Any]:
    """`--param k=v` (strings) and `--json k=JSON` (parsed) into one params dict."""
    params: dict[str, Any] = {}
    for key, value in pairs:
        params[key] = value
    for key, raw in json_pairs:
        try:
            params[key] = json.loads(raw)
        except ValueError:
            raise UsageError("--json %s is not valid JSON" % key) from None
    return params
