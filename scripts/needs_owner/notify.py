"""Announcing an item:  the filing seat's bot opens the OWNER topic that mentions Jay.

`notify` runs right after an item is filed (or adopted), as the filing seat:

    agent-sync post --as SEAT --session needsown --json --topic "OWNER <ID> <subject>" --to-owner -- -

with the message on standard input.  The mention comes from the owner id pinned in listener.toml
(`--to-owner`), never from a name.  On success a comment `[needs-owner] notified msg <id>` records the
message.  When the post fails or the seat has no bot credential the item gets `[needs-owner]
notify_pending` and CLAUDE's fallback notifier (C6-2) sends it.  A mirrored item (`--mirror-of`) has no
post of its own:  the Grok Bot persona's topic is the notice, so its message id is recorded and the
Linear link is replied in that topic.

A post that may or may not have gone through (agent-sync exit 5 or 6, or a timeout) is never retried
blind:  the message carries a reference line (the item's uid), and the topic is read for it first.  Only
`find_notice` reads it, and the C6-2 notifier imports that one function:  it takes a message only from the
bot of the filing seat (or CLAUDE, whose fallback notifier posts for any seat), only when the reference is
a line of its own that is not quoted, and then the oldest such message, so a person's quote-and-reply or
a peer who copies the (public) uid can never become the notice.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import f3
from . import links as linkmod
from . import records
from . import text as T
from .errors import Refusal
from .items import Item
from .proc import Result, Runner

SESSION = "needsown"  # agent-sync cuts a session label to 8 characters
CHANNEL = "agent-sync"
READ_BACK = 30  # messages read when looking for a post that may have landed
MAYBE_SENT_CODES = (5, 6)
FALLBACK_SEAT = "CLAUDE"  # CLAUDE's fallback notifier (C6-2) posts a pending notice for any seat
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass
class Notice:
    state: str  # notified | notify_pending
    msg_id: int | None = None
    topic: str | None = None
    reason: str | None = None
    recorded: bool = False  # the Linear comment that records it
    already: bool = False  # it was already announced
    linkback: str | None = None  # mirror items:  ok | failed

    def to_json(self) -> dict:
        return {"state": self.state, "notified_msg_id": self.msg_id, "topic": self.topic, "reason": self.reason,
                "recorded": self.recorded, "already_notified": self.already, "linkback": self.linkback}


def _quote(text: str, cap: int) -> str:
    return f3.textsafe.render_untrusted(text, cap)


def _again_phrase(again_of: str) -> str:
    """'AFC-12 msg 456' as 'AFC-12 (message 456)'."""
    ident, _, message = again_of.partition(" msg ")
    return "%s (message %s)" % (ident, message) if message else ident


def compose_body(item: Item) -> str:
    """The OWNER topic's first message.  Every word that came from the filer goes through `render_untrusted`."""
    b = item.block
    if b is None:
        raise Refusal("%s has no readable needs-owner block" % item.identifier)
    urgent = item.severity == "P1"
    paragraphs = ["**%s** from %s:  %s" % ("Urgent decision needed" if urgent else "Decision needed", b.filed_by,
                                          _quote(b.question, 260))]
    if b.again_of:
        paragraphs.append("Asking again.  The earlier question was %s." % _again_phrase(b.again_of))
    lines = []
    for n, option in enumerate(b.options, 1):
        if n in b.low_stakes:
            lines.append("%s Option %d:  %s  (Tap to answer)" % (T.option_emoji(n), n, _quote(option, 220)))
        else:
            lines.append("Option %d:  %s  (reply here to choose)" % (n, _quote(option, 220)))
    paragraphs.append("\n".join(lines))
    if b.recommend:
        paragraphs.append("Recommendation:  option %d." % b.recommend)
    paragraphs.append("Linear:  %s" % item.url)
    paragraphs.append("Answer here or as a comment on the Linear issue.")
    paragraphs.append("Ref:  `%s`" % b.uid)
    return "\n\n".join(paragraphs) + "\n"


def _message_id(result: Result) -> int | None:
    for line in reversed(result.out.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("id"), int):
            return value["id"]
    return None


def own_lines(content: str) -> list[str]:
    """The lines of a message's raw Markdown that are its author's own words:  not a line that begins with `>`,
    and not a line inside a closed code fence (Zulip's quote-and-reply wraps the original in a ```quote fence
    with no `>` on its lines).  A fence that is never closed hides nothing."""
    lines = (content or "").splitlines()
    kept = []
    n = 0
    while n < len(lines):
        opening = _FENCE.match(lines[n])
        if opening:
            fence = opening.group(1)
            closing = re.compile(r"^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence)))
            end = next((k for k in range(n + 1, len(lines)) if closing.match(lines[k])), None)
            if end is not None:
                n = end + 1
                continue
        if not lines[n].lstrip().startswith(">"):
            kept.append(lines[n])
        n += 1
    return kept


def carries_ref(content: str, uid: str) -> bool:
    """True when `content` has the reference line `Ref:  `<uid>`` as a line of its own, not quoted."""
    want = re.compile(r"^ {0,3}Ref:\s+`%s`\s*$" % re.escape(uid))
    return any(want.match(line) for line in own_lines(content))


def notice_seats(seat: str) -> frozenset[str]:
    """The seats whose bot may have posted the notice of an item that `seat` filed."""
    return frozenset({f3.identity.canonical_seat(seat), FALLBACK_SEAT})


def find_notice(runner: Runner, topic: str, uid: str) -> int | None:
    """The id of the message that announced the item whose uid is `uid`, else None.

    It reads the newest `READ_BACK` messages of `topic` (`agent-sync read --json`, whose fields are the real
    `message_json`) and keeps a message only when all of this holds:  `is_bot` is true (never a person,
    whatever name they sign), its `seat` is the filer's or CLAUDE (a bot of another seat can copy the public
    uid), and `carries_ref` finds the reference as an unquoted line of its own (a quote-and-reply, from
    anyone, does not count).  The result is the lowest matching id, because the first post is the notice.
    A sender whose kind agent-sync could not look up has `is_bot` null and is left out:  the worst result
    is a repeated post, never an answer or a quote taken for the notice."""
    result = runner.agent_sync("read", "--as", runner.seat, "--session", SESSION, "--json", "--channel", CHANNEL,
                               "--topic", topic, "--include-self", "--limit", str(READ_BACK))
    if not result.ok:
        return None
    allowed = notice_seats(runner.seat)
    found = []
    for line in result.out.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or not isinstance(message.get("id"), int) or isinstance(message.get("id"), bool):
            continue
        if message.get("is_bot") is not True or f3.identity.canonical_seat(message.get("seat")) not in allowed:
            continue
        if carries_ref(str(message.get("content") or ""), uid):
            found.append(message["id"])
    return min(found) if found else None


def _record(runner: Runner, item: Item, text: str) -> bool:
    """One comment on the item, tried twice."""
    for _ in range(2):
        with runner.private_file(text) as path:
            if runner.board("comment", item.identifier, "--by", runner.seat, "--text-file", path).ok:
                return True
    return False


def _announce(runner: Runner, item: Item, msg_id: int, topic: str) -> Notice:
    return Notice("notified", msg_id, topic, None, _record(runner, item, records.notified_text(msg_id, topic)))


def _pending(runner: Runner, item: Item, topic: str, reason: str) -> Notice:
    recorded = True
    if not item.pending:  # one record is enough;  a rerun does not pile more on
        recorded = _record(runner, item, records.pending_text(reason))
    return Notice("notify_pending", None, topic, reason, recorded)


def notify(runner: Runner, item: Item, *, rescan: bool = False, environ: dict | None = None) -> Notice:
    """Announce `item` unless it already is.  `rescan` looks in the topic for an earlier post of this item
    first (a rerun after a crash between the post and its record)."""
    b = item.block
    if b is None:
        raise Refusal("%s has no readable needs-owner block;  adopt it first" % item.identifier)
    if item.notified:
        return Notice("notified", item.notified[0], item.notified[1] or item.topic, None, True, already=True)
    if b.mirror_of:
        return _mirror(runner, item, linkmod.parse_topic_link(b.mirror_of, environ))
    topic = T.topic_for(item.identifier, b.question)
    if rescan:
        earlier = find_notice(runner, topic, b.uid)
        if earlier is not None:
            return _announce(runner, item, earlier, topic)
    result = runner.agent_sync("post", "--as", runner.seat, "--session", SESSION, "--json", "--topic", topic,
                               "--to-owner", "--", "-", stdin=compose_body(item))
    if result.ok:
        msg_id = _message_id(result)
        if msg_id is not None:
            return _announce(runner, item, msg_id, topic)
        landed = find_notice(runner, topic, b.uid)  # posted, but the answer carried no id
        return _announce(runner, item, landed, topic) if landed is not None else _pending(runner, item, topic, "send_failed")
    if result.missing:
        return _pending(runner, item, topic, "no_agent_sync")
    if result.timed_out or result.code in MAYBE_SENT_CODES:
        landed = find_notice(runner, topic, b.uid)
        if landed is not None:
            return _announce(runner, item, landed, topic)
        return _pending(runner, item, topic, "send_failed")
    if result.code == 3:
        return _pending(runner, item, topic, "no_credential")
    return _pending(runner, item, topic, "send_failed")


def _mirror(runner: Runner, item: Item, link: linkmod.TopicLink) -> Notice:
    """A Grok Bot persona already opened the topic:  record its message as the notice, then reply there
    with the Linear link (the reply is a courtesy;  its failure is reported and does not undo the record)."""
    notice = _announce(runner, item, link.message_id, link.topic)
    reply = ("Filed in Linear as %s:  %s\n\nAnswer here or as a comment on the Linear issue.\n" % (item.identifier, item.url))
    result = runner.agent_sync("reply", "--as", runner.seat, "--session", SESSION, "--json", "--id", str(link.message_id),
                               "--", "-", stdin=reply)
    notice.linkback = "ok" if result.ok else "failed"
    return notice
