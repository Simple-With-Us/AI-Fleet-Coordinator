"""Outbound text fix-ups for what agent-sync sends to Zulip.

`sentence_gap` is a safety net, not a substitute for writing the gap:  authors still write two
spaces after a sentence, and Zulip's Markdown renderer then collapses them to one.  A real
U+00A0 plus an ASCII space survives, so the client swaps the one for the other on the way out.
It is applied to message content that this process is sending and never to anything it reads.

The rules, all of them conservative (a missed conversion leaves the text as written, a wrong one
would corrupt it):

  * After `.`, `!` or `?`, optionally followed by closing characters  " ' ” ’ ) ] * _
    two or more ASCII spaces that precede a non-space character on the same line become exactly
    U+00A0 plus one ASCII space.
  * Single spaces stay (`e.g. x`, `v1.2.3`), and so do trailing spaces at the end of a line (a
    Markdown hard break).
  * Fenced code blocks (``` and ~~~, indented or inside a list or quote, unclosed ones running to
    the end), inline code spans (a run of N backticks closes on a run of exactly N), Zulip math
    (`$$...$$`) and @-mentions are left alone.  So are the contents of ```quote and ```spoiler
    fences:  a quote block holds someone else's words.
  * A list marker such as `1.  item` is left alone:  U+00A0 after the marker would end the list.
  * Idempotent, and length-preserving for exactly two spaces (two characters become two
    characters; a longer run only gets shorter).  The 10,000-character cap and the secret scanner
    therefore reach the same verdict on the text before and after.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import re
from datetime import UTC
from typing import Any

__all__ = ["GAP", "NBSP", "fix_outbound", "fleet_dates", "sentence_gap", "single_gaps"]

NBSP = " "
GAP = NBSP + " "

# A sentence terminator, closing characters, then a run of ASCII spaces.  `[ ]` and not `\s`:  NBSP is
# whitespace to `\s`, and matching it would stop an existing gap from being left exactly as it is.
_GAP_RE = re.compile(r"(?P<end>[.!?][\"'”’)\]*_]*)(?P<spaces>[ ]{2,})(?=[^\s])")
_LIST_MARKER_PREFIX_RE = re.compile(r"[ \t>]*\d{1,9}")

# An opening fence may sit behind indentation, quote markers and list markers ("  - ```", "1. ```").
_FENCE_OPEN_RE = re.compile(
    r"(?P<prefix>(?:[ \t]*(?:>|[-+*]|\d{1,9}[.)]))*[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)"
)
_FENCE_CLOSE_RE = re.compile(r"[ \t]*(?:>[ \t]*)*(?P<fence>`{3,}|~{3,})[ \t\r]*")

# Inline spans, found left to right so the earlier opener wins.  None of them may run across a blank
# line, and an opener with no closer is literal text that protects nothing.
_INLINE_RE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=ticks)(?!`))"
    r"|(?P<math>\$\$(?:(?!\n[ \t]*\n).)+?\$\$)"
    r"|(?P<mention>@_?\*{1,2}[^*\n]+\*{1,2})",
    re.DOTALL,
)

_MASK = ""


def _split_fences(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(fenced, open) line-aligned ranges of `text`.  `fenced` covers every fenced block from its
    opening fence line through its closing fence line (or the end of the text when unclosed);
    `open` is the complement, the runs that can hold prose."""
    fenced: list[tuple[int, int]] = []
    prose: list[tuple[int, int]] = []
    pos = 0
    prose_start = 0
    fence_start = 0
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        end = pos + len(line) + 1  # past the newline (one past the end for the last line)
        if fence_char:
            closer = _FENCE_CLOSE_RE.fullmatch(line)
            if (
                closer
                and closer.group("fence")[0] == fence_char
                and len(closer.group("fence")) >= fence_len
            ):
                fenced.append((fence_start, min(end, len(text))))
                fence_char = ""
                prose_start = min(end, len(text))
        else:
            opener = _FENCE_OPEN_RE.fullmatch(line)
            if opener and not (opener.group("fence")[0] == "`" and "`" in opener.group("info")):
                if prose_start < pos:
                    prose.append((prose_start, pos))
                fence_start = pos
                fence_char = opener.group("fence")[0]
                fence_len = len(opener.group("fence"))
        pos = end
    if fence_char:
        fenced.append((fence_start, len(text)))
    elif prose_start < len(text):
        prose.append((prose_start, len(text)))
    return fenced, prose


def _protected(text: str) -> list[tuple[int, int]]:
    """Every range `sentence_gap` must not look inside."""
    fenced, prose = _split_fences(text)
    ranges = list(fenced)
    for start, end in prose:
        chunk = text[start:end]
        for match in _INLINE_RE.finditer(chunk):
            ranges.append((start + match.start(), start + match.end()))
    return ranges


def sentence_gap(text: str) -> str:
    """`text` with each ASCII-space sentence gap turned into U+00A0 plus one space.  See the module
    docstring for what is left alone."""
    if "  " not in text:
        return text
    masked = list(text)
    for start, end in _protected(text):
        for index in range(start, end):
            if masked[index] not in "\n\r":
                masked[index] = _MASK
    pieces: list[str] = []
    last = 0
    for match in _GAP_RE.finditer("".join(masked)):
        if match.group("end") == ".":
            line_start = text.rfind("\n", 0, match.start()) + 1
            if _LIST_MARKER_PREFIX_RE.fullmatch(text[line_start : match.start()]):
                continue
        pieces.append(text[last : match.start("spaces")])
        pieces.append(GAP)
        last = match.end("spaces")
    if not pieces:
        return text
    pieces.append(text[last:])
    return "".join(pieces)


# ---- Owner 2026-10-19:  enforce the gap and the fleet date format, and say what changed ------------------------------
#
# sentence_gap only widens a gap the author typed as two spaces.  Agents often type one (and some tools collapse two
# into one before the text reaches this client), so single_gaps also widens ONE space after a sentence when a capital
# letter, digit, quote, `*`, `#`, `(`, code or a mention follows.  It is a heuristic:  a list marker (`1. Item`) is
# skipped, and so are known abbreviations, dotted words and single-letter initials (`Dr. Smith`, `U.S. Army`,
# `J. Doe`).  A missed gap is the old behaviour;  a wrong one is a slightly wider space, never changed words.
#
# fleet_dates rewrites an ISO date in prose (`2026-10-19`, `2026-10-20T02:03:00Z`) into the fleet form
# (`Mon, Oct 19`, `Mon, Oct 19, 9:03pm`), in Central, with the year only when it is not this year.  A date inside a
# name or a path (`backup-2026-10-19.tar`, `/2026-10-19/`) is left alone.
#
# Both skip code, math and mentions (the same protected ranges as sentence_gap), and both report what they changed,
# so the CLI can tell the agent and it learns to write it that way.

# "no" is absent on purpose:  "I said no.  Then we stop." ends a sentence, and "No. 5" cannot be told apart
# from it here, so the same call as check-routine-prompts.py makes.  A wrong widening is a slightly wider space.
_ABBREVIATIONS = frozenset(
    [
        "mr",
        "mrs",
        "ms",
        "dr",
        "st",
        "jr",
        "sr",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "eg",
        "ie",
        "cf",
        "al",
        "approx",
        "dept",
        "est",
        "fig",
        "nos",
        "vol",
        "ca",
        "mt",
        "ft",
        "sq",
    ]
)
_SINGLE_GAP_RE = re.compile(r"(?P<end>[.!?][\"'”’)\]*_]*)(?P<space>[ ])(?=[A-Z0-9\"“(*#\[])")
_WORD_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")

# The same rules as the Apple Notes script's date rewrite.  A date is a token of its own:  a name or a query string
# that merely contains one keeps it, and a dot after the date ends a sentence unless a word character follows.  On
# the space form a -HH:MM is the end of a time range (2026-10-19 10:30-11:00), so it is tried first;  it is read as
# a UTC offset only when the start carried seconds and the end did not (2026-10-19 10:30:00-05:00), because a
# range that shows seconds shows them on both ends (2026-10-19 10:30:00-11:00:00).  A +HH:MM is never a range.
_ISO_RE = re.compile(
    r"(?<![\w/.:=?&-])(\d{4})-(\d{2})-(\d{2})"
    r"(?:T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"
    r"| (\d{2}):(\d{2})(:\d{2}(?:\.\d+)?)?(?:-(\d{2}):(\d{2})(:\d{2}(?:\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?)?"
    r"(?![\w/-]|\.[A-Za-z0-9])"
)


def _masked(text: str) -> str:
    masked = list(text)
    for start, end in _protected(text):
        for index in range(start, end):
            if masked[index] not in "\n\r":
                masked[index] = _MASK
    return "".join(masked)


def single_gaps(text: str) -> tuple[str, int]:
    """(`text` with each one-space sentence gap widened to U+00A0 plus a space, how many)."""
    if not re.search(r"[.!?]\S* ", text):
        return text, 0
    masked = _masked(text)
    pieces: list[str] = []
    last = 0
    count = 0
    for match in _SINGLE_GAP_RE.finditer(masked):
        before = _WORD_BEFORE_RE.search(masked[max(0, match.start() - 40) : match.start()])
        word = before.group(1).casefold().rstrip(".") if before else ""
        if (
            match.group("end").startswith(".")
            and before
            and (word in _ABBREVIATIONS or len(word) == 1 or "." in word)
        ):
            continue
        if match.group("end") == ".":
            line_start = text.rfind("\n", 0, match.start()) + 1
            if _LIST_MARKER_PREFIX_RE.fullmatch(text[line_start : match.start()]):
                continue  # `1. Item`:  U+00A0 after the marker would end the list
        pieces.append(text[last : match.start("space")])
        pieces.append(GAP)
        last = match.end("space")
        count += 1
    if not count:
        return text, 0
    pieces.append(text[last:])
    return "".join(pieces), count


def _central(value: Any) -> Any:
    """`value` (an aware datetime) as Central wall-clock time.  Uses zoneinfo;  if the machine has no tz
    database, applies the US rule (second Sunday of March to first Sunday of November) directly, the same
    fallback the CLI and open_loops use."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    utc = value.astimezone(UTC)
    try:
        return utc.astimezone(ZoneInfo("America/Chicago"))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass

    def nth_sunday(year: int, month: int, n: int) -> int:
        first = datetime(year, month, 1)
        return 1 + (6 - first.weekday()) % 7 + 7 * (n - 1)

    start = datetime(utc.year, 3, nth_sunday(utc.year, 3, 2), 8, tzinfo=UTC)
    end = datetime(utc.year, 11, nth_sunday(utc.year, 11, 1), 7, tzinfo=UTC)
    return utc.astimezone(timezone(timedelta(hours=-5 if start <= utc < end else -6)))


def _central_now() -> Any:
    from datetime import datetime

    return _central(datetime.now(UTC))


def _fleet_day(when: Any, this_year: int) -> str:
    out = when.strftime("%a, %b ") + str(when.day)
    return out + (", " + str(when.year) if when.year != this_year else "")


def _fleet_date(match: re.Match[str], this_year: int) -> str:
    from datetime import datetime

    (
        year,
        month,
        day,
        t_hour,
        t_minute,
        t_zone,
        hour,
        minute,
        s_secs,
        r_hour,
        r_minute,
        r_secs,
        zone,
    ) = match.groups()
    if t_hour is not None:
        hour, minute, zone = t_hour, t_minute, t_zone
    elif r_hour is not None and s_secs is not None and r_secs is None:
        # Seconds on the start of a range but not on its end:  a UTC offset, not a range (see _ISO_RE).
        zone = "-" + r_hour + ":" + r_minute
        r_hour = r_minute = None

    def clock(when: Any) -> str:
        return str(when.hour % 12 or 12) + when.strftime(":%M") + ("am" if when.hour < 12 else "pm")

    try:
        if hour is None:
            return _fleet_day(datetime(int(year), int(month), int(day)), this_year)
        when = datetime(int(year), int(month), int(day), int(hour), int(minute))
        offset = ""
        if zone:
            offset = (
                "+00:00" if zone == "Z" else (zone if ":" in zone else zone[:3] + ":" + zone[3:])
            )
            when = _central(datetime.fromisoformat(when.isoformat() + offset))
        out = _fleet_day(when, this_year) + ", " + clock(when)
        if r_hour is not None:
            # The end of a range carries the same zone as its start, so it converts the same way.
            end = datetime(int(year), int(month), int(day), int(r_hour), int(r_minute))
            if offset:
                end = _central(datetime.fromisoformat(end.isoformat() + offset))
            out += "-" + clock(end)
        return out
    except (ValueError, OverflowError):
        # OverflowError:  a zone conversion off a year-1 stamp runs out of range;  leave the date alone.
        return match.group(0)


def fleet_dates(text: str, now: Any = None) -> tuple[str, list[tuple[str, str]]]:
    """(`text` with ISO dates in prose in the fleet form, [(old, new), ...])."""
    if not re.search(r"\d{4}-\d{2}-\d{2}", text):
        return text, []
    this_year = (now or _central_now()).year
    masked = _masked(text)
    pieces: list[str] = []
    changes: list[tuple[str, str]] = []
    last = 0
    for match in _ISO_RE.finditer(masked):
        new = _fleet_date(match, this_year)
        if new == match.group(0):
            continue
        pieces.append(text[last : match.start()])
        pieces.append(new)
        changes.append((match.group(0), new))
        last = match.end()
    if not changes:
        return text, []
    pieces.append(text[last:])
    return "".join(pieces), changes


def fix_outbound(text: str, limit: int | None = None, now: Any = None) -> tuple[str, list[str]]:
    """Every outbound fix, and a short report of what changed for the author.  With `limit`, a result longer than it
    keeps only the length-safe double-space conversion (a single gap and a date each add a character).

    Idempotent, because the hosted transport applies this after the body layer already applied it:  the date rewrite
    runs before the gap widening, so a stamp that ends in a single letter (`...T02:03:00Z.`) is decided on the same
    text both passes see, and a second pass widens nothing new."""
    gapped = sentence_gap(text)
    dated, dates = fleet_dates(gapped, now)
    fixed, singles = single_gaps(dated)
    if limit is not None and len(fixed) > limit:
        return gapped, []
    report: list[str] = []
    if dates:
        shown = ", ".join("%s to %s" % pair for pair in dates[:5]) + (
            " and more" if len(dates) > 5 else ""
        )
        report.append("dates rewritten in the fleet format (%s);  write dates that way" % shown)
    if singles:
        report.append(
            "%d sentence gap%s had one space and %s widened;  put two spaces after a sentence"
            % (singles, "" if singles == 1 else "s", "was" if singles == 1 else "were")
        )
    return fixed, report
