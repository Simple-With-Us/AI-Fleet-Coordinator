"""CLI commands for the rest of Zulip (AFC-330):  show, edit, delete, history, move, unresolve,
unreact, mark-read, mark-unread, star, unstar, search, channel-info, members, unsubscribe, users,
presence, set-status, groups, emoji, upload, scheduled, drafts, typing, dm-list and the guarded
`api` escape hatch.  The work is in zulip_ops.py, which the stdio MCP tools call too.

Every command acts as this seat's own bot (the CLI's Agent), every outbound string is secret-scanned
before any request, and Zulip-authored text is shown with control characters escaped, as `read` does.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import router as ROUTER
from . import secretscan
from . import zulip as Z
from . import zulip_ops as OPS
from .zulip import UsageError


def _cli():
    from . import cli

    return cli


# --------------------------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------------------------


def _deliver_at(value: str) -> int:
    """Unix seconds, or an ISO 8601 time with an offset (2026-10-16T09:00-05:00)."""
    text = value.strip()
    if text.isdigit():
        return int(text)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "%r is neither unix seconds nor an ISO 8601 time" % value
        ) from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "give the time with its offset, for example 2026-10-16T09:00-05:00"
        )
    return int(parsed.timestamp())


def add_parsers(
    sub: Any, common: argparse.ArgumentParser, channel_default: str = Z.DEFAULT_CHANNEL
) -> None:
    cli = _cli()

    def add(name: str, summary: str, parent: Any = sub) -> argparse.ArgumentParser:
        return parent.add_parser(name, parents=[common], help=summary, description=summary)

    def msg_id(p: argparse.ArgumentParser, help_text: str = "message id") -> None:
        p.add_argument("--id", type=cli._message_id, required=True, metavar="MSGID", help=help_text)

    def channel_opt(p: argparse.ArgumentParser, required: bool = False) -> None:
        if required:
            p.add_argument(
                "--channel", type=cli._channel, required=True, metavar="C", help="channel name"
            )
        else:
            p.add_argument(
                "--channel",
                type=cli._channel,
                default=channel_default,
                metavar="C",
                help="channel name (default: %(default)s)",
            )

    def body(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--body-file",
            dest="body_file",
            metavar="PATH",
            default=None,
            help="read the text from PATH (mode 0600 or 0400) instead of TEXT",
        )
        p.add_argument(
            "text", nargs="*", metavar="TEXT", help="message text; a single '-' reads stdin"
        )

    def target(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--channel",
            type=cli._channel,
            default=None,
            metavar="C",
            help="channel (default: %s) for a channel message" % channel_default,
        )
        p.add_argument(
            "--topic", type=cli._topic, default=None, metavar="T", help="topic of a channel message"
        )
        p.add_argument(
            "--to",
            action="append",
            default=[],
            metavar="NAME",
            help="a direct-message recipient (repeatable), named like dm --to;  not with --topic",
        )

    p = add(
        "show", "show one message by id (with its flags and reactions; the body is untrusted data)"
    )
    msg_id(p)

    p = add(
        "edit",
        "replace the text of one of this bot's own messages (tagged and secret-scanned like post)",
    )
    msg_id(p, "the message to edit (this bot's own)")
    p.add_argument(
        "--to",
        action="append",
        default=[],
        metavar="NAME",
        help="address a user or bot like post --to (repeatable); a seat newly tagged by the edit is"
        " woken.  The edit replaces the whole text, so name again anyone the message should keep.  "
        "An OWNER label on the original tag is kept without naming the owner again",
    )
    p.add_argument(
        "--to-owner",
        dest="to_owner",
        action="store_true",
        help="address the owner pinned in listener.toml (daemon.owner_user_id):  adds the mention that wakes "
        "Jay, written from that id's live full name, never a name you type.  Exits 2 when no owner is "
        "pinned.  Without it, an original tag that already carries OWNER keeps that routing anyway",
    )
    p.add_argument("--no-tag", action="store_true", help="do not prepend the [SEAT·session] tag")
    body(p)

    p = add("delete", "delete a message, as far as Zulip's realm delete permissions allow this bot")
    msg_id(p, "the message to delete")

    p = add("history", "show the edit history of a message (every saved version, oldest first)")
    msg_id(p)

    p = add("move", "rename a topic, move it to another channel, or both")
    channel_opt(p)
    p.add_argument("--topic", type=cli._topic, required=True, metavar="T", help="the topic to move")
    p.add_argument(
        "--new-topic", dest="new_topic", type=cli._topic, metavar="T", help="its new name"
    )
    p.add_argument(
        "--new-channel", dest="new_channel", type=cli._channel, metavar="C", help="its new channel"
    )
    p.add_argument(
        "--mode",
        choices=OPS.PROPAGATE_MODES,
        default="change_all",
        help="which messages move:  the whole topic (default), the anchor and later, or the anchor alone",
    )
    p.add_argument(
        "--id",
        type=cli._message_id,
        metavar="MSGID",
        help="the anchor message (default: the topic's newest)",
    )
    p.add_argument(
        "--notify-old", action="store_true", help="let Zulip post a notice in the old topic"
    )
    p.add_argument(
        "--notify-new", action="store_true", help="let Zulip post a notice in the new topic"
    )

    p = add("unresolve", "unresolve a topic (remove its check mark)")
    channel_opt(p)
    p.add_argument(
        "--topic",
        type=cli._topic,
        required=True,
        metavar="T",
        help="the resolved topic, with or without its mark",
    )

    p = add("unreact", "remove this bot's emoji reaction from a message")
    msg_id(p)
    p.add_argument("emoji", metavar="EMOJI", help="emoji name, for example eyes")

    for name, summary in (
        ("mark-read", "mark messages read:  by id, or a whole channel or topic"),
        ("mark-unread", "mark messages unread:  by id, or a whole channel or topic"),
    ):
        p = add(name, summary)
        p.add_argument(
            "--id",
            type=cli._message_id,
            action="append",
            default=[],
            metavar="MSGID",
            help="a message id (repeatable, at most %d)" % OPS.FLAG_IDS_MAX,
        )
        p.add_argument(
            "--channel",
            type=cli._channel,
            default=None,
            metavar="C",
            help="every message in a channel",
        )
        p.add_argument(
            "--topic",
            type=cli._topic,
            default=None,
            metavar="T",
            help="only this topic of --channel",
        )

    for name, summary in (("star", "star messages"), ("unstar", "unstar messages")):
        p = add(name, summary)
        p.add_argument(
            "--id",
            type=cli._message_id,
            action="append",
            required=True,
            metavar="MSGID",
            help="a message id (repeatable, at most %d)" % OPS.FLAG_IDS_MAX,
        )

    p = add("search", "search messages:  words, channel, topic, sender, has, is, a DM conversation")
    p.add_argument(
        "words", nargs="*", metavar="WORDS", help="search words (Zulip full-text search)"
    )
    p.add_argument("--channel", type=cli._channel, default=None, metavar="C")
    p.add_argument("--topic", type=cli._topic, default=None, metavar="T", help="needs --channel")
    p.add_argument("--sender", metavar="NAME", help="a sender, named like post --to")
    p.add_argument("--has", action="append", default=[], choices=OPS.SEARCH_HAS, help="repeatable")
    p.add_argument("--is", dest="is_", choices=OPS.SEARCH_IS)
    p.add_argument(
        "--dm-with",
        dest="dm_with",
        action="append",
        default=[],
        metavar="NAME",
        help="a direct-message conversation (repeatable for a group)",
    )
    where = p.add_mutually_exclusive_group()
    where.add_argument("--since", type=cli._message_id, metavar="ID", help="matches after this id")
    where.add_argument("--near", type=cli._message_id, metavar="ID", help="matches around this id")
    p.add_argument(
        "--limit",
        type=cli._positive_int,
        default=20,
        metavar="N",
        help="at most N (default: %(default)s)",
    )

    p = add("channel-info", "show one channel:  id, description, privacy, subscriber count")
    channel_opt(p, required=True)
    p = add("members", "list the subscribers of a channel")
    channel_opt(p, required=True)
    p = add("unsubscribe", "unsubscribe this bot from a channel")
    channel_opt(p, required=True)

    p = add("users", "look users up by name, email, seat tag or id")
    p.add_argument(
        "query", nargs="?", metavar="QUERY", help="part of a name, an email or a seat tag"
    )
    p.add_argument("--id", dest="user_id", type=cli._positive_int, metavar="N")
    p.add_argument("--email", metavar="E")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--bots", action="store_const", dest="kind", const="bots")
    kind.add_argument("--humans", action="store_const", dest="kind", const="humans")
    p.add_argument("--inactive", action="store_true", help="include deactivated users")
    p.add_argument("--limit", type=cli._positive_int, default=50, metavar="N")

    p = add("presence", "show whether a user is active, idle or offline")
    p.add_argument("name", metavar="NAME", help="a user, named like post --to")

    p = add("set-status", "set this bot's status text, emoji or away flag")
    p.add_argument(
        "--text",
        metavar="T",
        help="status text (at most %d characters;  '' clears it)" % OPS.STATUS_TEXT_MAX,
    )
    p.add_argument("--emoji", metavar="E", help="status emoji name ('' clears it)")
    away = p.add_mutually_exclusive_group()
    away.add_argument("--away", action="store_const", dest="away", const=True)
    away.add_argument("--back", action="store_const", dest="away", const=False)

    p = add("groups", "list user groups, or the members of one")
    p.add_argument(
        "--id", dest="group_id", type=cli._positive_int, metavar="N", help="one group's members"
    )

    p = add("emoji", "list the realm's custom emoji")
    p.add_argument("--all", action="store_true", help="include deactivated emoji")

    p = add("upload", "upload a file as this bot and print the link to put in a message")
    p.add_argument(
        "path",
        metavar="PATH",
        help="a regular file, at most %d MB" % (OPS.UPLOAD_MAX_BYTES // (1024 * 1024)),
    )
    p.add_argument(
        "--name", metavar="NAME", help="the file name Zulip shows (default: the file's own)"
    )

    p = add("scheduled", "list, create, edit or delete scheduled messages")
    ssub = p.add_subparsers(dest="scheduled_action", metavar="ACTION", required=True)
    add("list", "list this bot's scheduled messages", ssub)
    q = add("create", "schedule a message (tagged and secret-scanned like post)", ssub)
    target(q)
    q.add_argument(
        "--at",
        dest="deliver_at",
        type=_deliver_at,
        required=True,
        metavar="WHEN",
        help="unix seconds, or ISO 8601 with an offset",
    )
    q.add_argument("--no-tag", action="store_true")
    body(q)
    q = add("edit", "change a scheduled message's text, topic or time", ssub)
    q.add_argument("--id", type=cli._positive_int, required=True, metavar="N")
    q.add_argument("--topic", type=cli._topic, default=None, metavar="T")
    q.add_argument("--at", dest="deliver_at", type=_deliver_at, default=None, metavar="WHEN")
    q.add_argument("--no-tag", action="store_true")
    body(q)
    q = add("delete", "delete a scheduled message", ssub)
    q.add_argument("--id", type=cli._positive_int, required=True, metavar="N")

    p = add("drafts", "list, create, edit or delete this bot's drafts (never sent)")
    dsub = p.add_subparsers(dest="drafts_action", metavar="ACTION", required=True)
    add("list", "list drafts", dsub)
    q = add("create", "save a draft", dsub)
    target(q)
    body(q)
    q = add("edit", "replace a draft", dsub)
    q.add_argument("--id", type=cli._positive_int, required=True, metavar="N")
    target(q)
    body(q)
    q = add("delete", "delete a draft", dsub)
    q.add_argument("--id", type=cli._positive_int, required=True, metavar="N")

    p = add("typing", "send a typing notification to a topic or a DM conversation")
    p.add_argument("op", choices=("start", "stop"))
    target(p)

    p = add("dm-list", "list this bot's recent direct-message conversations")
    p.add_argument("--limit", type=cli._positive_int, default=20, metavar="N")

    p = add("api", "call one allowlisted, non-admin Zulip endpoint (an escape hatch; see docs)")
    p.add_argument(
        "method", choices=("GET", "POST", "PATCH", "DELETE"), type=str.upper, metavar="METHOD"
    )
    p.add_argument(
        "path", metavar="PATH", help="the path after /api/v1/, for example messages/123/history"
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="a string parameter (repeatable)",
    )
    p.add_argument(
        "--json-param",
        dest="json_param",
        action="append",
        default=[],
        metavar="K=JSON",
        help="a JSON parameter such as narrow or subscriptions (repeatable)",
    )


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _scan(agent: Any, **fields: str | None) -> None:
    known = secretscan.basic_forms(agent.creds.email, agent.creds.key)
    for field, value in fields.items():
        if value:
            found = secretscan.scan(value, known)
            if found:
                raise UsageError(
                    "refusing to send: the %s looks like it contains %s" % (field, found)
                )


def _text(agent: Any, args: argparse.Namespace) -> str:
    return _cli()._read_text(agent, args.text, getattr(args, "body_file", None))


def _tagged(agent: Any, text: str, no_tag: bool) -> str:
    return _cli()._compose(agent, text, labels=[], mentions=[], no_tag=no_tag)


def _show(agent: Any, text: str, inline: bool = True) -> str:
    return _cli()._visible(str(text if text is not None else ""), inline=inline)


def _rows(
    agent: Any,
    rows: Sequence[Mapping[str, Any]],
    line: Callable[[Mapping[str, Any]], str],
    empty: str,
) -> int:
    if agent.json:
        for row in rows:
            agent.emit_json(dict(row))
        return 0
    for row in rows:
        agent.out(line(row) + "\n")
    if not rows:
        agent.err(empty)
    return 0


def _result(agent: Any, result: Mapping[str, Any], text: str) -> int:
    if agent.json:
        agent.emit_json(dict(result))
    else:
        agent.out(text + "\n")
    return 0


def _one_user(agent: Any, name: str) -> Mapping[str, Any]:
    cli = _cli()
    return cli._lookup_peer(agent, [u for u in agent.users() if u.get("full_name")], name)


def _target(agent: Any, args: argparse.Namespace) -> dict[str, Any]:
    cli = _cli()
    ids: list[int] = []
    if args.to:
        everyone = list(agent.users())
        ids = sorted({int(cli._dm_user(agent, everyone, raw)["user_id"]) for raw in args.to})
        if args.channel is not None:
            raise UsageError(
                "a direct message has no channel:  give --to or --channel and --topic, not both"
            )
    return OPS.target_params(agent, channel=args.channel, topic=args.topic, user_ids=ids or None)


def _tag_carries_owner(agent: Any, content: Any) -> bool:
    """Whether a message's own leading seat tag carries the OWNER label.  The tag is read as
    `_compose` writes it (`[SEAT·session→OWNER,...]`, with `->` accepted as elsewhere), and only
    that leading bracket counts.  A `→OWNER` quoted later in the text is not routing."""
    text, _ = ROUTER.strip_no_wake(str(content or ""), getattr(agent, "seat", None))
    text = text.lstrip()
    if not text.startswith("["):
        return False
    close = text.find("]")
    if close < 0:
        return False
    tag = text[1:close]
    if "→" in tag:
        head, _, labels = tag.partition("→")
    elif "->" in tag:
        head, _, labels = tag.partition("->")
    else:
        return False
    seat, session_mark, _session = head.partition("·")
    #  The same seat-tag rule strip_no_wake uses:  a bracket counts when it carries a session
    #  part, or when it names this seat.  Any other leading bracket is ordinary text.
    if not seat or (not session_mark and seat != getattr(agent, "seat", None)):
        return False
    return "OWNER" in {label.strip() for label in labels.split(",")}


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def cmd_show(agent: Any, args: argparse.Namespace) -> int:
    agent.emit(OPS.get_message(agent, args.id))
    return 0


def cmd_edit(agent: Any, args: argparse.Namespace) -> int:
    cli = _cli()
    text = _text(agent, args)
    to_owner = bool(getattr(args, "to_owner", False))
    if to_owner:
        #  The same pre-check reply makes in cli.py:  an unpinned owner exits 2 before the
        #  original message is even fetched.  Post and reply have no separate owner-queue
        #  write.  Their routing is the OWNER label plus the pinned mention that
        #  `_resolve_targets` adds, so reusing that pair here is the whole consistent path.
        cli._owner_user_id(agent)
    #  A plain text fix must not silently drop owner routing.  Read this bot's own original
    #  first, and keep the owner when its tag already carried OWNER.  The kept case cannot
    #  pre-check the pinned owner before that fetch, because the fetch is what tells us the
    #  owner is needed.  `_resolve_targets` still fails closed before any edit is sent when
    #  no owner is pinned.
    original = OPS.own_message(agent, args.id)
    to_owner = to_owner or _tag_carries_owner(agent, original.get("content"))
    mentions, labels = cli._resolve_targets(agent, getattr(args, "to", []) or [], to_owner)
    #  --no-tag still wins for the bracket, exactly as `_compose` gives post.  The mentions
    #  above stay, so a seat or the owner named by the edit is still woken.
    body = cli._compose(
        agent, text, labels=labels, mentions=mentions, no_tag=bool(getattr(args, "no_tag", False))
    )
    result = OPS.edit_message(agent, args.id, body, checked=original)
    return _result(agent, result, "edited id %d" % args.id)


def cmd_delete(agent: Any, args: argparse.Namespace) -> int:
    return _result(agent, OPS.delete_message(agent, args.id), "deleted id %d" % args.id)


def cmd_history(agent: Any, args: argparse.Namespace) -> int:
    rows = OPS.message_history(agent, args.id)
    fmt = _cli().format_time

    def line(row: Mapping[str, Any]) -> str:
        head = "%s  user %s  topic %s" % (
            fmt(float(row.get("timestamp") or 0)),
            row.get("user_id"),
            _show(agent, row.get("topic")),
        )
        return "%s\n%s\n" % (head, _show(agent, row.get("content"), inline=False))

    return _rows(agent, rows, line, "no history")


def cmd_move(agent: Any, args: argparse.Namespace) -> int:
    _scan(agent, **{"new topic": args.new_topic})
    result = OPS.move_topic(
        agent,
        args.channel,
        args.topic,
        new_topic=args.new_topic,
        new_channel=args.new_channel,
        mode=args.mode,
        message_id=args.id,
        notify_old=args.notify_old,
        notify_new=args.notify_new,
    )
    return _result(
        agent,
        result,
        "moved #%s › %s (%s, anchor %d)"
        % (_show(agent, args.channel), _show(agent, args.topic), args.mode, result["id"]),
    )


def cmd_unresolve(agent: Any, args: argparse.Namespace) -> int:
    result = OPS.set_resolved(agent, args.channel, args.topic, False)
    return _result(
        agent,
        result,
        "unresolved: #%s › %s" % (_show(agent, args.channel), _show(agent, result["topic"])),
    )


def cmd_unreact(agent: Any, args: argparse.Namespace) -> int:
    result = OPS.unreact(agent, args.id, args.emoji)
    return _result(agent, result, "removed :%s: from %d" % (result["emoji"], args.id))


def _flags(flag: str, add: bool) -> Callable[[Any, argparse.Namespace], int]:
    def run(agent: Any, args: argparse.Namespace) -> int:
        ids = list(getattr(args, "id", None) or [])
        channel = getattr(args, "channel", None)
        topic = getattr(args, "topic", None)
        if ids and channel:
            raise UsageError("give message ids or a channel, not both")
        if topic and not channel:
            raise UsageError("--topic needs --channel")
        narrow = None
        if channel:
            narrow = [{"operator": "channel", "operand": channel}]
            if topic:
                narrow.append({"operator": "topic", "operand": topic})
        result = OPS.flag_messages(agent, flag, add, message_ids=ids or None, narrow=narrow)
        verb = {
            ("read", True): "marked read",
            ("read", False): "marked unread",
            ("starred", True): "starred",
            ("starred", False): "unstarred",
        }[(flag, add)]
        return _result(
            agent,
            result,
            "%s: %d message%s" % (verb, result["updated"], "" if result["updated"] == 1 else "s"),
        )

    return run


def cmd_search(agent: Any, args: argparse.Namespace) -> int:
    cli = _cli()
    query = " ".join(args.words).strip() or None
    _scan(agent, query=query)
    sender = None
    if args.sender:
        sender = str(_one_user(agent, args.sender).get("email") or "")
    dm_with = cli._dm_with_ids(agent, args.dm_with) if args.dm_with else None
    narrow = OPS.search_narrow(
        query=query,
        channel=args.channel,
        topic=args.topic,
        sender=sender,
        has=args.has,
        is_=args.is_,
        dm_with=dm_with,
    )
    messages = OPS.search(agent, narrow, limit=args.limit, since_id=args.since, near=args.near)
    for message in messages:
        agent.emit(message)
    if not messages and not agent.json:
        agent.err("no matches")
    return 0


def cmd_channel_info(agent: Any, args: argparse.Namespace) -> int:
    info = OPS.channel_info(agent, args.channel)
    return _result(
        agent,
        info,
        "#%s  id %s  %s  %d subscribers\n%s"
        % (
            _show(agent, info["name"]),
            info["stream_id"],
            "public" if info["public"] else "private",
            info["subscriber_count"],
            _show(agent, info.get("description") or ""),
        ),
    )


def _user_line(agent: Any) -> Callable[[Mapping[str, Any]], str]:
    def line(row: Mapping[str, Any]) -> str:
        kind = "bot" if row.get("is_bot") else "human"
        seat = (" seat %s" % row["seat"]) if row.get("seat") else ""
        gone = "" if row.get("is_active", True) else " (deactivated)"
        return "%10s  %s <%s> [%s]%s%s" % (
            row.get("user_id"),
            _show(agent, row.get("full_name")),
            _show(agent, row.get("email")),
            kind,
            seat,
            gone,
        )

    return line


def cmd_members(agent: Any, args: argparse.Namespace) -> int:
    return _rows(
        agent, OPS.channel_members(agent, args.channel), _user_line(agent), "no subscribers"
    )


def cmd_unsubscribe(agent: Any, args: argparse.Namespace) -> int:
    result = OPS.unsubscribe(agent, args.channel)
    text = ("unsubscribed from #%s" if result["removed"] else "was not subscribed to #%s") % _show(
        agent, args.channel
    )
    return _result(agent, result, text)


def cmd_users(agent: Any, args: argparse.Namespace) -> int:
    rows = OPS.find_users(
        agent,
        query=args.query,
        user_id=args.user_id,
        email=args.email,
        include_inactive=args.inactive,
        kind=args.kind,
        limit=args.limit,
    )
    return _rows(agent, rows, _user_line(agent), "no matching users")


def cmd_presence(agent: Any, args: argparse.Namespace) -> int:
    user = _one_user(agent, args.name)
    result = OPS.presence(agent, int(user["user_id"]))
    when = _cli().format_time(float(result["timestamp"])) if result["timestamp"] else "never"
    return _result(
        agent,
        result,
        "%s is %s (last seen %s)" % (_show(agent, user.get("full_name")), result["status"], when),
    )


def cmd_set_status(agent: Any, args: argparse.Namespace) -> int:
    _scan(agent, **{"status text": args.text})
    return _result(
        agent,
        OPS.set_status(agent, status_text=args.text, emoji_name=args.emoji, away=args.away),
        "status set",
    )


def cmd_groups(agent: Any, args: argparse.Namespace) -> int:
    groups = OPS.user_groups(agent)
    if args.group_id is not None:
        group = next((g for g in groups if g["id"] == args.group_id), None)
        if group is None:
            raise UsageError("no user group with id %d" % args.group_id)
        by_id = {u.get("user_id"): u for u in agent.users()}
        rows = [OPS.user_row(by_id.get(i) or {"user_id": i}) for i in group["members"]]
        return _rows(agent, rows, _user_line(agent), "the group has no members")
    return _rows(
        agent,
        groups,
        lambda g: (
            "%8s  %s  (%d members)%s"
            % (
                g["id"],
                _show(agent, g["name"]),
                len(g["members"]),
                " system" if g["system"] else "",
            )
        ),
        "no user groups",
    )


def cmd_emoji(agent: Any, args: argparse.Namespace) -> int:
    rows = OPS.custom_emoji(agent, include_deactivated=args.all)
    return _rows(
        agent,
        rows,
        lambda e: (
            ":%s:%s" % (_show(agent, e["name"]), " (deactivated)" if e["deactivated"] else "")
        ),
        "no custom emoji",
    )


def cmd_upload(agent: Any, args: argparse.Namespace) -> int:
    path = os.path.expanduser(args.path)
    try:
        info = os.stat(path)
    except OSError as exc:
        raise UsageError("cannot read %s: %s" % (args.path, exc.strerror)) from None
    if not stat.S_ISREG(info.st_mode):
        raise UsageError("%s is not a regular file" % args.path)
    if info.st_size > OPS.UPLOAD_MAX_BYTES:
        raise UsageError(
            "%s is %d bytes; uploads are at most %d"
            % (args.path, info.st_size, OPS.UPLOAD_MAX_BYTES)
        )
    with open(path, "rb") as fh:
        data = fh.read(OPS.UPLOAD_MAX_BYTES + 1)
    name = args.name or os.path.basename(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    _scan(agent, file=text, **{"file name": name})
    kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
    result = OPS.upload(agent, name, data, kind)
    return _result(agent, result, "%s\n%s" % (result["url"], result["markdown"]))


def _target_line(agent: Any, row: Mapping[str, Any]) -> str:
    if row.get("type") in ("stream",) and row.get("topic") is not None:
        return "channel %s › %s" % (row.get("to"), _show(agent, row.get("topic")))
    return "DM %s" % row.get("to")


def cmd_scheduled(agent: Any, args: argparse.Namespace) -> int:
    action = args.scheduled_action
    fmt = _cli().format_time
    if action == "list":
        return _rows(
            agent,
            OPS.scheduled_list(agent),
            lambda r: (
                "%8s  %s  %s%s\n%s"
                % (
                    r["id"],
                    fmt(float(r.get("deliver_at") or 0)),
                    _target_line(agent, r),
                    "  FAILED" if r["failed"] else "",
                    _show(agent, r.get("content"), inline=False),
                )
            ),
            "no scheduled messages",
        )
    if action == "delete":
        return _result(
            agent, OPS.scheduled_delete(agent, args.id), "deleted scheduled message %d" % args.id
        )
    if action == "create":
        text = _text(agent, args)
        target = _target(agent, args)
        _scan(agent, topic=target.get("topic"))
        result = OPS.scheduled_create(
            agent, target, _tagged(agent, text, args.no_tag), args.deliver_at
        )
        return _result(
            agent,
            result,
            "scheduled id %d for %s" % (result["id"], fmt(float(result["deliver_at"]))),
        )
    text = _text(agent, args) if (args.text or args.body_file) else None
    _scan(agent, topic=args.topic)
    result = OPS.scheduled_edit(
        agent,
        args.id,
        body=_tagged(agent, text, args.no_tag) if text else None,
        topic=args.topic,
        deliver_at=args.deliver_at,
    )
    return _result(agent, result, "updated scheduled message %d" % args.id)


def cmd_drafts(agent: Any, args: argparse.Namespace) -> int:
    action = args.drafts_action
    if action == "list":
        return _rows(
            agent,
            OPS.drafts_list(agent),
            lambda r: (
                "%8s  %s\n%s"
                % (
                    r["id"],
                    _target_line(
                        agent,
                        {
                            **r,
                            "to": r.get("to"),
                            "type": "stream" if r.get("type") == "stream" else "direct",
                        },
                    ),
                    _show(agent, r.get("content"), inline=False),
                )
            ),
            "no drafts",
        )
    if action == "delete":
        return _result(agent, OPS.draft_delete(agent, args.id), "deleted draft %d" % args.id)
    text = _text(agent, args)
    target = _target(agent, args)
    _scan(agent, topic=target.get("topic"))
    if action == "create":
        result = OPS.draft_create(agent, target, text)
        return _result(agent, result, "saved draft %d" % result["id"])
    return _result(
        agent, OPS.draft_edit(agent, args.id, target, text), "updated draft %d" % args.id
    )


def cmd_typing(agent: Any, args: argparse.Namespace) -> int:
    if not args.to and args.topic is None:
        raise UsageError("typing needs --channel and --topic, or --to")
    return _result(agent, OPS.typing(agent, args.op, _target(agent, args)), "typing %s" % args.op)


def dm_conversations(agent: Any, limit: int, scan: int = 200) -> list[dict[str, Any]]:
    """The bot's recent DM conversations, newest first (the hosted zulip_dm_list rule):  the newest
    `scan` direct messages grouped by participant set."""
    me = agent.me().get("user_id")
    messages = agent.fetch([{"operator": "is", "operand": "dm"}], anchor="newest", num_before=scan)
    convos: dict[tuple[int, ...], dict[str, Any]] = {}
    for m in messages:
        people = [
            p
            for p in (m.get("display_recipient") or [])
            if isinstance(p, Mapping) and isinstance(p.get("id"), int)
        ]
        if m.get("type") != "private" or not people:
            continue
        others = [p for p in people if p["id"] != me] or people
        key = tuple(sorted(p["id"] for p in others))
        row = convos.setdefault(
            key,
            {
                "user_ids": list(key),
                "participants": [{"user_id": p["id"], "name": p.get("full_name")} for p in others],
                "last": m,
                "unread": 0,
            },
        )
        if m["id"] > row["last"]["id"]:
            row["last"] = m
        mine = m.get("sender_id") == me
        if not mine and "read" not in (m.get("flags") or []):
            row["unread"] += 1
    rows = sorted(convos.values(), key=lambda c: -c["last"]["id"])[:limit]
    return [
        {
            "user_ids": c["user_ids"],
            "participants": c["participants"],
            "group": len(c["user_ids"]) > 1,
            "last_message_id": c["last"]["id"],
            "last_timestamp": c["last"].get("timestamp"),
            "last_from_self": c["last"].get("sender_id") == me,
            "unread": c["unread"],
        }
        for c in rows
    ]


def cmd_dm_list(agent: Any, args: argparse.Namespace) -> int:
    fmt = _cli().format_time
    rows = dm_conversations(agent, args.limit)
    return _rows(
        agent,
        rows,
        lambda c: (
            "%s  %s  last %s%s%s"
            % (
                ",".join(str(i) for i in c["user_ids"]),
                ", ".join(_show(agent, p.get("name")) for p in c["participants"]),
                fmt(float(c["last_timestamp"] or 0)),
                " (you)" if c["last_from_self"] else "",
                "  %d unread" % c["unread"] if c["unread"] else "",
            )
        ),
        "no direct messages",
    )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _pairs(raw: Sequence[str], flag: str) -> list[tuple[str, str]]:
    out = []
    for item in raw:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise UsageError("%s takes KEY=VALUE, got %r" % (flag, item.split("=", 1)[0]))
        out.append((key.strip(), value))
    return out


def cmd_api(agent: Any, args: argparse.Namespace) -> int:
    """The escape hatch:  one allowlisted non-admin endpoint, every string sent secret-scanned."""
    path = args.path.strip().strip("/")
    path = path.removeprefix("api/v1/")
    if not OPS.api_allowed(args.method, path):
        raise UsageError(
            "%s %s is not on the agent-sync api allowlist (non-admin endpoints only; see "
            'docs/protocols/zulip-fleet-guide.md, "The agent-sync CLI")' % (args.method, path)
        )
    params = OPS.encode_json_params(
        _pairs(args.param, "--param"), _pairs(args.json_param, "--json-param")
    )
    if args.method != "GET":
        _scan(agent, **{"parameter %d" % i: s for i, s in enumerate(_strings(params))})
    result = agent.client.request(args.method, path, params or None)
    shown = {k: v for k, v in result.items() if k not in ("result", "msg")}
    agent.out(json.dumps(shown, ensure_ascii=False, indent=None if agent.json else 2) + "\n")
    return 0


COMMANDS: dict[str, Callable[[Any, argparse.Namespace], int]] = {
    "show": cmd_show,
    "edit": cmd_edit,
    "delete": cmd_delete,
    "history": cmd_history,
    "move": cmd_move,
    "unresolve": cmd_unresolve,
    "unreact": cmd_unreact,
    "mark-read": _flags("read", True),
    "mark-unread": _flags("read", False),
    "star": _flags("starred", True),
    "unstar": _flags("starred", False),
    "search": cmd_search,
    "channel-info": cmd_channel_info,
    "members": cmd_members,
    "unsubscribe": cmd_unsubscribe,
    "users": cmd_users,
    "presence": cmd_presence,
    "set-status": cmd_set_status,
    "groups": cmd_groups,
    "emoji": cmd_emoji,
    "upload": cmd_upload,
    "scheduled": cmd_scheduled,
    "drafts": cmd_drafts,
    "typing": cmd_typing,
    "dm-list": cmd_dm_list,
    "api": cmd_api,
}
