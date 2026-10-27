"""The comments that record what happened to an item.

They are ordinary Linear comments (`board comment --text-file`), signed by whoever wrote them, and they
are the only state besides the block.  Readers accept any run of whitespace (two spaces and NBSP
included) where the writers put one, so a comment typed or reflowed by hand still reads.

  [needs-owner] block                    adopt:  the block of an issue filed before it had one
  [needs-owner] notified msg <id>        the filer's bot posted the OWNER topic (message id <id>)
  topic: <topic name>
  [needs-owner] notify_pending           the post failed or could not be sent;  the fallback notifier sends it
  reason: <word>
  owner replied:  msg <id>               written by the notifier (C6-2), never by the filer
  owner replied:  reaction <n> on msg <id>
  owner replied:  comment <id>

A reply record says that a reply exists.  It never holds the answer, and a consumer verifies before it
acts:  `simple-chat verify-owner-msg` for a Zulip message, the reaction's user id for a tap, and a
comment author equal to OWNER_LINEAR_USER_ID for Linear.
"""

from __future__ import annotations

import re

BLOCK_HEAD = "[needs-owner] block"
NOTIFIED_HEAD = "[needs-owner] notified msg"
PENDING_HEAD = "[needs-owner] notify_pending"
REPLY_HEAD = "owner replied:"

PENDING_REASONS = ("no_credential", "send_failed", "no_agent_sync", "unsent", "held")

_NOTIFIED = re.compile(
    r"\A\s*\[needs-owner\]\s+notified\s+msg\s+([0-9]{1,12})\b(.*)\Z", re.IGNORECASE | re.DOTALL
)
_PENDING = re.compile(r"\A\s*\[needs-owner\]\s+notify_pending\b(.*)\Z", re.IGNORECASE | re.DOTALL)
_BLOCK = re.compile(r"\A\s*\[needs-owner\]\s+block\b", re.IGNORECASE)
_REPLY = re.compile(r"\A\s*(?:\[needs-owner\]\s*)?owner\s+replied:\s*(?P<what>.+)\Z", re.IGNORECASE | re.DOTALL)
_REPLY_MSG = re.compile(r"msg\s+([0-9]{1,12})\b", re.IGNORECASE)
_REPLY_COMMENT = re.compile(r"comment\s+(\S+)", re.IGNORECASE)
_REPLY_REACTION = re.compile(r"reaction\s+(?:option\s+)?([1-9])\s+on\s+msg\s+([0-9]{1,12})\b", re.IGNORECASE)
_TOPIC_LINE = re.compile(r"^topic:[ \t]*(\S.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_REASON_LINE = re.compile(r"^reason:[ \t]*([a-z_]{1,30})[ \t]*$", re.IGNORECASE | re.MULTILINE)


def notified_text(msg_id: int, topic: str) -> str:
    return "%s %d\ntopic: %s" % (NOTIFIED_HEAD, msg_id, topic)


def pending_text(reason: str) -> str:
    return "%s\nreason: %s" % (PENDING_HEAD, reason)


def reply_text(kind: str, ident: object, option: int | None = None) -> str:
    """`owner replied:  msg 5`, `owner replied:  reaction 2 on msg 55` or `owner replied:  comment <id>`.  It says a
    reply exists and where, never what it said (a tap's option number is the tap itself).  `parse_reply` reads it."""
    if kind == "msg":
        return "%s  msg %d" % (REPLY_HEAD, int(ident))
    if kind == "reaction":
        if option is None or not 1 <= int(option) <= 9:
            raise ValueError("a reaction reply names an option from 1 to 9")
        return "%s  reaction %d on msg %d" % (REPLY_HEAD, int(option), int(ident))
    if kind == "comment":
        text = str(ident).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", text):
            raise ValueError("a comment id is letters, digits and hyphens")
        return "%s  comment %s" % (REPLY_HEAD, text)
    raise ValueError("unknown reply kind %r" % kind)


def block_comment_text(block_text: str) -> str:
    return "%s\n\n%s" % (BLOCK_HEAD, block_text)


def is_block_comment(text: str) -> bool:
    return bool(_BLOCK.match(text or ""))


def parse_notified(text: str) -> tuple[int, str | None] | None:
    """(message id, topic name or None) of a notified record, else None."""
    match = _NOTIFIED.match(text or "")
    if not match:
        return None
    topic = _TOPIC_LINE.search(match.group(2))
    return int(match.group(1)), (topic.group(1) if topic else None)


def parse_pending(text: str) -> str | None:
    """The reason of a pending record ('unsent' when none is given), else None."""
    match = _PENDING.match(text or "")
    if not match:
        return None
    reason = _REASON_LINE.search(match.group(1))
    return reason.group(1).lower() if reason else "unsent"


def parse_reply(text: str) -> dict | None:
    """{"kind": "msg"|"reaction"|"comment", "id": str, "option": int|None} of a reply record, else None."""
    match = _REPLY.match(text or "")
    if not match:
        return None
    what = match.group("what").strip().splitlines()[0].strip()
    m = _REPLY_REACTION.match(what)
    if m:
        return {"kind": "reaction", "id": m.group(2), "option": int(m.group(1))}
    m = _REPLY_MSG.match(what)
    if m:
        return {"kind": "msg", "id": m.group(1), "option": None}
    m = _REPLY_COMMENT.match(what)
    if m:
        return {"kind": "comment", "id": m.group(1), "option": None}
    return None
