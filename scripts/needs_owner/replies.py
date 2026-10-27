"""What counts as an answer, as pure functions:  a tap, a control word, a Grok Bot request, the notifier's own words.

The notifier (notifier.py) does the reading and writing;  everything it has to decide from what it read is here, so
each rule has a test of its own.

  taps      A reaction counts only when it is the pinned owner's (`user_id`), a standard emoji (`reaction_type`
            unicode_emoji, because a realm can add a custom emoji that reuses a name) whose `emoji_code` is a keycap
            from 1️⃣ to 5️⃣ that names an option of the item.  Exactly one such option is a tap;  two different ones
            mean "ask again"; a tap on an option that is not low-stakes is not an answer ("this one needs a typed
            reply").
  control   A message that is only the word `pause` or `resume` controls the notifier and is never an answer.
  request   A Grok Bot persona's fenced `needs-owner-request` JSON block, validated strictly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import f3
from . import safety
from . import text as T
from .errors import Refusal

# Zulip's emoji_code for the keycaps 1️⃣ to 5️⃣ ("0031-20e3"); the variant with the emoji selector is accepted too.
KEYCAP_CODES = {}
for _n in range(1, 6):
    KEYCAP_CODES["003%d-20e3" % _n] = _n
    KEYCAP_CODES["003%d-fe0f-20e3" % _n] = _n
STANDARD_EMOJI = "unicode_emoji"


def option_of_reaction(reaction: object) -> int | None:
    """The option number (1 to 5) a reaction stands for, else None.  Only a standard emoji with a keycap code."""
    if not isinstance(reaction, dict) or reaction.get("reaction_type") != STANDARD_EMOJI:
        return None
    code = reaction.get("emoji_code")
    return KEYCAP_CODES.get(code) if isinstance(code, str) else None


def owner_taps(reactions: object, owner_id: int | None, option_count: int) -> list[int]:
    """The distinct options the owner tapped, sorted.  Reactions of anyone else, custom emoji, other emoji and
    keycaps beyond the item's options are not taps."""
    if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id <= 0 or not isinstance(reactions, list):
        return []
    found = set()
    for reaction in reactions:
        if not isinstance(reaction, dict) or reaction.get("user_id") != owner_id:
            continue
        option = option_of_reaction(reaction)
        if option is not None and option <= option_count:
            found.add(option)
    return sorted(found)


@dataclass
class TapVerdict:
    kind: str  # none | answer | conflict | risky
    options: list[int]  # the options tapped


def judge_taps(taps: list[int], low_stakes: list[int]) -> TapVerdict:
    if not taps:
        return TapVerdict("none", [])
    if len(taps) > 1:
        return TapVerdict("conflict", taps)  # two different options:  ask again, count neither
    return TapVerdict("answer" if taps[0] in low_stakes else "risky", taps)


_MENTION = re.compile(r"@_?\*\*[^*\n]{1,80}\*\*")


def control_word(content: object) -> str | None:
    """`pause` or `resume` when the whole message is that word (case, a mention, spaces and . or ! around it are
    ignored), else None.  A sentence that holds the word is an answer, not a control."""
    text = _MENTION.sub(" ", str(content or ""))
    text = " ".join(text.split()).strip(" .!—-").casefold()
    return text if text in ("pause", "resume") else None


# ---- the notifier's own words --------------------------------------------------------------------------

def keycaps(options: list[int]) -> str:
    return " and ".join(T.option_emoji(n) for n in options)


def ask_again_text(options: list[int]) -> str:
    return ("I saw two different reactions on the question (%s).  I counted neither.  Leave exactly one reaction, or reply here "
            "in words." % keycaps(options))


def typed_reply_text(option: int) -> str:
    return ("I saw a reaction on option %d, which needs a typed reply.  I did not count it.  Please reply here in words." % option)


def note_text(identifier: str, url: str, kind: str, ident: str) -> str:
    where = {"msg": "message %s in this topic", "reaction": "a reaction on message %s in this topic", "comment": "Linear comment %s"}[kind]
    return ("Jay replied on %s:  %s  (%s).  Verify it before you act:  `simple-chat verify-owner-msg` for a message, the owner's "
            "reaction (`simple-chat read --json` `reactions` and `whoami --json` `owner_user_id`) for a tap, the comment's author "
            "for Linear.  The item stays open;  close it yourself when you have acted." % (identifier, url, where % ident))


def pending_header(for_seat: str, filed_by: str | None) -> str:
    return "**For %s** (filed by %s, not checked by CLAUDE)." % (for_seat, filed_by or "an unknown seat")


def rejected_request_text(reason: str) -> str:
    return ("I could not file this request in Linear (%s).  Check the `needs-owner-request` block against the contract in "
            "docs/protocols/needs-owner.md and post it again as a new message." % reason)


# ---- a Grok Bot persona's request --------------------------------------------------------------------------

REQUEST_INFO = "needs-owner-request"
REQUEST_KEYS = frozenset({"question", "options", "seat", "low_stakes", "recommend", "context", "urgent", "app", "format"})
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_-]*)\s*$")
CONTEXT_ROOM = T.CONTEXT_MAX - 60  # the notifier puts "for GB-<ROLE>:  " in front


class RequestProblem(Exception):
    """Why a request block is not accepted.  `reason` is a word, never the persona's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Request:
    seat: str
    question: str
    options: list[str]
    low_stakes: list[int]
    recommend: int | None
    context: str | None
    urgent: bool


def find_request_blocks(content: object) -> list[str]:
    """The bodies of every fenced block in `content` whose info string is `needs-owner-request`."""
    lines = str(content or "").replace("\r\n", "\n").split("\n")
    bodies = []
    n = 0
    while n < len(lines):
        opening = _FENCE_OPEN.match(lines[n])
        if opening:
            fence, info = opening.group(1), opening.group(2)
            closing = re.compile(r"^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence)))
            end = next((k for k in range(n + 1, len(lines)) if closing.match(lines[k])), None)
            if end is None:
                return bodies  # never closed:  nothing after it is trusted
            if info == REQUEST_INFO:
                bodies.append("\n".join(lines[n + 1:end]))
            n = end + 1
            continue
        n += 1
    return bodies


def _int_list(raw: object, count: int, reason: str) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RequestProblem(reason)
    found = set()
    for value in raw:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= count:
            raise RequestProblem(reason)
        found.add(value)
    return sorted(found)


def parse_request(content: object, sender_seat: str) -> Request | None:
    """The request in a message, None when it holds no `needs-owner-request` block, or RequestProblem.

    Strict:  one block, a JSON object, only the known keys, a question of 1 to 200 characters, 1 to 5 different
    options, `seat` equal to the seat of the bot that sent the message (a persona files only for itself),
    `low_stakes` and `recommend` naming real options, no `urgent` with `low_stakes`, and the same secret and
    personal-data refusals as `needs-owner file`."""
    blocks = find_request_blocks(content)
    if not blocks:
        return None
    if len(blocks) > 1:
        raise RequestProblem("two_blocks")
    try:
        data = json.loads(blocks[0])
    except ValueError:
        raise RequestProblem("not_json") from None
    if not isinstance(data, dict):
        raise RequestProblem("not_an_object")
    if set(data) - REQUEST_KEYS:
        raise RequestProblem("unknown_key")
    if f3.identity.canonical_seat(data.get("seat")) != f3.identity.canonical_seat(sender_seat):
        raise RequestProblem("seat_is_not_the_sender")
    try:
        question = T.clean_text(data.get("question"), "question", T.QUESTION_MAX)
        raw_options = data.get("options")
        if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= T.MAX_OPTIONS:
            raise RequestProblem("options_count")
        options = [T.clean_text(o, "option %d" % n, T.OPTION_MAX) for n, o in enumerate(raw_options, 1)]
        if len({T.normalize(o) for o in options}) != len(options):
            raise RequestProblem("options_repeat")
        context = None
        if data.get("context") is not None:
            context = T.clean_text(data.get("context"), "context", CONTEXT_ROOM, required=False) or None
    except Refusal:
        raise RequestProblem("text_not_accepted") from None
    low = _int_list(data.get("low_stakes"), len(options), "low_stakes_option")
    recommend = data.get("recommend")
    if recommend is not None and (not isinstance(recommend, int) or isinstance(recommend, bool) or not 1 <= recommend <= len(options)):
        raise RequestProblem("recommend_option")
    urgent = data.get("urgent", False)
    if not isinstance(urgent, bool):
        raise RequestProblem("urgent_flag")
    if urgent and low:
        raise RequestProblem("urgent_with_low_stakes")
    try:
        safety.check_fields({"the question": question, "the context": context,
                             **{"option %d" % n: o for n, o in enumerate(options, 1)}})
    except Refusal:
        raise RequestProblem("secret_or_personal_data") from None
    return Request(f3.identity.canonical_seat(sender_seat), question, options, low, recommend, context, urgent)
