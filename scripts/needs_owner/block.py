"""The fixed block that makes an issue a needs-owner item.

It sits in a fenced code block, because Linear re-serializes Markdown (it escapes `[ ] ~` and turns
`-` bullets into `*`) but keeps code verbatim:

    ```needs-owner
    Format: needs-owner/1
    Uid: needs-owner:CLAUDE:3f2a9c1e
    Question: Revive PR 761 or close it?
    Option 1 (low-stakes): Revive it
    Option 2: Close it
    Recommendation: option 1
    Parent: none
    Filed by: CLAUDE
    Context: none
    ```

`Version`, `Again of` and `Mirror of` lines appear only when they apply.  Every value is one line
(`text.clean_text`), so a value can neither start a new field nor close the fence.  `render_block`
writes it;  `parse_block` is the strict reader (unknown or repeated keys, gaps in the option numbers,
a bad uid or a seat that is not a token are all errors).  An item with no valid block is "unadopted".
There is no "default if no answer" action anywhere in it:  nothing happens until Jay answers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import text as T

FORMAT = "needs-owner/1"
FENCE_OPEN = "```needs-owner"
FENCE_CLOSE = "```"
_FENCE_RE = re.compile(r"^```needs-owner[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*$", re.S | re.M)
_OPTION_KEY = re.compile(r"option (?P<n>[1-9])(?: \((?P<mark>low-stakes)\))?")
_SEAT_RE = re.compile(r"[A-Z][A-Z0-9-]{0,31}")
_PARENT_RE = re.compile(r"[A-Z]{1,8}-[0-9]{1,9}")
_AGAIN_RE = re.compile(r"[A-Z]{1,8}-[0-9]{1,9}(?: msg [0-9]{1,12})?")
SINGLE_KEYS = ("format", "uid", "question", "recommendation", "parent", "filed by", "context", "version", "again of",
               "mirror of")


class BlockError(ValueError):
    """Why a block is missing or not valid.  The text never repeats the item's own words."""


@dataclass
class Block:
    uid: str
    question: str
    options: list[str]
    low_stakes: list[int] = field(default_factory=list)  # 1-based option numbers a tap may answer
    recommend: int | None = None
    parent: str | None = None
    filed_by: str = ""
    context: str | None = None
    version: int = 1
    again_of: str | None = None  # "AFC-12 msg 456":  the earlier item this one re-asks
    mirror_of: str | None = None  # the Zulip topic link of a Grok Bot request this item mirrors

    @property
    def scope(self) -> str:
        """The middle part of the uid:  the parent's identifier, else the filing seat."""
        match = T.UID_RE.fullmatch(self.uid)
        return match.group(1) if match else (self.parent or self.filed_by)

    @property
    def for_seat(self) -> str | None:
        """`for GB-PLUMBER` at the start of the context marks an item CLAUDE mirrored for a persona."""
        match = re.match(r"for (GB-[A-Z0-9-]+)", self.context or "")
        return match.group(1) if match else None

    def mirror_message(self) -> int | None:
        if not self.mirror_of:
            return None
        match = re.search(r"/near/([0-9]+)", self.mirror_of)
        return int(match.group(1)) if match else None

    def hash_matches_uid(self) -> bool:
        """False when the question or options were edited after filing (the uid hash no longer fits)."""
        match = T.UID_RE.fullmatch(self.uid)
        return bool(match) and match.group(2) == T.uid_hash(self.question, self.options, version=self.version,
                                                            mirror_msg=self.mirror_message())

    def same_request(self, other: "Block") -> bool:
        """Every field a request can set, compared without case or spacing differences."""
        return (T.normalize(self.question) == T.normalize(other.question)
                and [T.normalize(o) for o in self.options] == [T.normalize(o) for o in other.options]
                and self.low_stakes == other.low_stakes and self.recommend == other.recommend
                and T.normalize(self.context or "") == T.normalize(other.context or "")
                and self.parent == other.parent)


def render_block(b: Block) -> str:
    lines = [FENCE_OPEN, "Format: " + FORMAT, "Uid: " + b.uid, "Question: " + b.question]
    for n, option in enumerate(b.options, 1):
        lines.append("Option %d%s: %s" % (n, " (low-stakes)" if n in b.low_stakes else "", option))
    lines.append("Recommendation: " + ("option %d" % b.recommend if b.recommend else "none"))
    lines.append("Parent: " + (b.parent or "none"))
    lines.append("Filed by: " + b.filed_by)
    lines.append("Context: " + (b.context or "none"))
    if b.version > 1:
        lines.append("Version: %d" % b.version)
    if b.again_of:
        lines.append("Again of: " + b.again_of)
    if b.mirror_of:
        lines.append("Mirror of: " + b.mirror_of)
    lines.append(FENCE_CLOSE)
    return "\n".join(lines)


def render_description(b: Block) -> str:
    """The issue description:  one plain sentence, the block, and where to answer."""
    return "\n".join([
        "A decision that needs Jay, filed by %s.  Nothing is decided or done until Jay answers." % b.filed_by,
        "",
        render_block(b),
        "",
        "Answer in the OWNER topic in Zulip (tap the emoji of a low-stakes option, or type a reply) or as a comment on this "
        "issue.  The filing seat closes this item after it acts on the answer.",
        "",
    ])


def find_fence(text: str) -> str | None:
    """The body of the first needs-owner fence in `text`, or None."""
    match = _FENCE_RE.search(text or "")
    return match.group("body") if match else None


def parse_block(text: str) -> Block:
    """The block in `text` (an issue description or a comment), strictly.  Raises BlockError."""
    body = find_fence(text)
    if body is None:
        raise BlockError("no needs-owner block")
    singles: dict[str, str] = {}
    options: dict[int, str] = {}
    low: list[int] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not sep:
            raise BlockError("a line without a field name")
        option = _OPTION_KEY.fullmatch(key)
        if option:
            n = int(option.group("n"))
            if n in options:
                raise BlockError("option %d appears twice" % n)
            if not value:
                raise BlockError("option %d is empty" % n)
            options[n] = value
            if option.group("mark"):
                low.append(n)
            continue
        if key not in SINGLE_KEYS:
            raise BlockError("unknown field %r" % key[:30])
        if key in singles:
            raise BlockError("%s appears twice" % key)
        singles[key] = value
    for needed in ("uid", "question", "filed by"):
        if not singles.get(needed):
            raise BlockError("%s is missing" % needed)
    if singles.get("format", FORMAT) != FORMAT:
        raise BlockError("an unknown block format")
    if not options or sorted(options) != list(range(1, len(options) + 1)) or len(options) > T.MAX_OPTIONS:
        raise BlockError("the options must be numbered 1 to n, with n from 1 to %d" % T.MAX_OPTIONS)
    if not T.UID_RE.fullmatch(singles["uid"]):
        raise BlockError("the uid is not needs-owner:<scope>:<sha8>")
    if not _SEAT_RE.fullmatch(singles["filed by"]):
        raise BlockError("'Filed by' is not a seat tag")
    if len(singles["question"]) > T.QUESTION_MAX or any(len(v) > T.OPTION_MAX for v in options.values()):
        raise BlockError("a field is longer than its cap")
    recommend = None
    rec = singles.get("recommendation", "none")
    if rec.lower() != "none":
        match = re.fullmatch(r"option ([1-9])", rec.lower())
        if not match or int(match.group(1)) not in options:
            raise BlockError("the recommendation does not name an option")
        recommend = int(match.group(1))
    parent = singles.get("parent", "none")
    if parent.lower() == "none":
        parent = None
    elif not _PARENT_RE.fullmatch(parent):
        raise BlockError("'Parent' is not a Linear identifier")
    context = singles.get("context", "none")
    if context.lower() == "none":
        context = None
    elif len(context) > T.CONTEXT_MAX:
        raise BlockError("the context is longer than its cap")
    version = 1
    if "version" in singles:
        if not singles["version"].isdigit() or not 2 <= int(singles["version"]) <= 99:
            raise BlockError("'Version' is not a number from 2 to 99")
        version = int(singles["version"])
    again = singles.get("again of")
    if again is not None and not _AGAIN_RE.fullmatch(again):
        raise BlockError("'Again of' is not an identifier with an optional message id")
    mirror = singles.get("mirror of")
    if mirror is not None and not (mirror.startswith("https://") and re.search(r"/near/[0-9]+$", mirror)):
        raise BlockError("'Mirror of' is not a Zulip message link")
    return Block(uid=singles["uid"], question=singles["question"], options=[options[n] for n in sorted(options)],
                 low_stakes=sorted(low), recommend=recommend, parent=parent, filed_by=singles["filed by"],
                 context=context, version=version, again_of=again, mirror_of=mirror)
