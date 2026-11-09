"""Caller text made safe to store, the uid, the topic name and the answer emoji.

Everything the filer types (question, options, context) is cleaned once, here, before it is checked
for secrets or stored:  control and invisible characters are dropped, every kind of whitespace
(NBSP included) collapses to one space, and a backtick becomes an apostrophe, so no text can close
the code fence that holds the item's block or break out of a Zulip code span.  Case and wording are
kept as typed.  `normalize` is only for comparing and hashing.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from . import f3
from .errors import Refusal

QUESTION_MAX = 200  # the question is the issue title, and the board cuts a title at 250 characters
OPTION_MAX = 200
CONTEXT_MAX = 600
MAX_OPTIONS = 5
TITLE_PREFIX = "OWNER DECISION:  "
TOPIC_MAX = 60  # a Zulip topic name
SUBJECT_MAX = 40

IDENT_RE = re.compile(r"[A-Za-z]{1,8}-[0-9]{1,9}")
_DROPPED = frozenset({"Cc", "Cf", "Cs", "Co"})

# 1️⃣ to 9️⃣:  the digit, the emoji variation selector and the combining keycap.
KEYCAPS = tuple("%d️⃣" % n for n in range(1, 10))


def option_emoji(n: int) -> str:
    """The keycap for option number `n` (1 to 5), by the option's own place, so a low-stakes option 3
    is always 3️⃣ whatever else is low-stakes."""
    return KEYCAPS[n - 1]


def clean_text(raw: object, field: str, cap: int, *, required: bool = True) -> str:
    """`raw` as one clean line, or a Refusal that names the field and never repeats the text."""
    text = "" if raw is None else str(raw)
    kept = []
    for char in text:
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char) not in _DROPPED:
            kept.append(char)
    cleaned = " ".join("".join(kept).split()).replace("`", "'")
    if not cleaned:
        if required:
            raise Refusal("%s is empty" % field)
        return ""
    if len(cleaned) > cap:
        raise Refusal("%s is %d characters;  the most is %d.  Say it shorter, and put the rest in --context or in the Linear "
                      "issue" % (field, len(cleaned), cap))
    return cleaned


def normalize(text: str) -> str:
    """Lower-case with all whitespace (NBSP included) collapsed:  the form that is compared and hashed."""
    return " ".join(str(text).casefold().split())


def parse_ident(raw: str, field: str) -> str:
    text = str(raw).strip()
    if not IDENT_RE.fullmatch(text):
        raise Refusal("%s %r is not a Linear identifier such as AFC-123" % (field, text[:40]))
    return text.upper()


def uid_hash(question: str, options: list[str], *, version: int = 1, mirror_msg: int | None = None) -> str:
    """The first 8 hex characters of the SHA-256 of the normalized question and options.  A mirrored item
    also folds in the Zulip message it mirrors (two personas asking one question are two items), and a
    version above 1 (`--again`) is folded in too, so the uid stays three parts."""
    parts = ["q:" + normalize(question)]
    parts += ["o%d:%s" % (i, normalize(o)) for i, o in enumerate(options, 1)]
    if mirror_msg:
        parts.append("mirror:%d" % mirror_msg)
    if version > 1:
        parts.append("v:%d" % version)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:8]


def make_uid(scope: str, question: str, options: list[str], *, version: int = 1, mirror_msg: int | None = None) -> str:
    """needs-owner:<parent identifier or SEAT>:<sha8>"""
    return "needs-owner:%s:%s" % (scope, uid_hash(question, options, version=version, mirror_msg=mirror_msg))


UID_RE = re.compile(r"needs-owner:([A-Za-z0-9-]+):([0-9a-f]{8})")


def title_for(question: str) -> str:
    return TITLE_PREFIX + question


def topic_for(identifier: str, question: str) -> str:
    """`OWNER <ID> <subject>`:  the Zulip topic of an item, at most 60 characters.  The subject is the
    question made inert (`render_untrusted`, no code span) and cut at 40 characters."""
    head = "OWNER %s " % identifier
    room = max(1, min(SUBJECT_MAX, TOPIC_MAX - len(head)))
    return head + f3.textsafe.render_untrusted(question, room, code_span=False)


def parse_index_list(values: list[str] | None, count: int, flag: str) -> list[int]:
    """`--low-stakes 1 --low-stakes 3` or `--low-stakes 1,3`, as sorted 1-based option numbers."""
    found: set[int] = set()
    for raw in values or []:
        for piece in str(raw).replace(" ", ",").split(","):
            if not piece:
                continue
            if not piece.isdigit() or not 1 <= int(piece) <= count:
                raise Refusal("%s %r is not an option number from 1 to %d" % (flag, piece[:20], count))
            found.add(int(piece))
    return sorted(found)
