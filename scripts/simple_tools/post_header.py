"""The shared outbound fleet header spacing, with legacy input compatibility (OPS-1086).

Only the first header changes.  Message prose, quoted later headers and spaces inside recipient
names stay intact.  The counts below are numeric owner requirements, never visual guesses.
"""

from __future__ import annotations

import re

HAIR = "\u200a"
AFTER_COLON = HAIR * 3
BEFORE_FOLDER = HAIR * 2
AFTER_FOLDER = HAIR
REPO_SEPARATOR = "," + HAIR
RECIPIENT_SEPARATOR = "," + HAIR * 2
ARROW = " → "

_HEADER = re.compile(r"^(?P<lead>\s*(?:(?:👀|:eyes:)\s*)?)\[(?P<inner>[^\r\n\[\]]+)\]")
_SENDER = re.compile(
    r"(?P<seat>[A-Z0-9][A-Z0-9_-]*)(?P<transport>·[^\s:→\]]+)?"
    r"(?:\s*:\s*(?P<thread>[A-Za-z0-9][A-Za-z0-9.-]*))?"
)
_OPERATOR_THREAD = re.compile(r"[A-Z]{1,2}[.-]\d+[.-][MSC]")
_BODY_HEADER = re.compile(r"^(?P<lead>(?:(?:👀|:eyes:)[ \t]*)?)\[(?P<inner>[^\r\n\[\]]+)\]")
_PR_FIRST_HEADER = re.compile(
    r"^(?P<lead> {0,3}(?:(?:👀|:eyes:)[ \t]*)?)\[(?P<inner>[^\r\n\[\]]+)\]"
)


def _pr_body_prefix(text: str) -> int | None:
    """Bound flat criteria to 32 lines/8 KiB before a blank-separated, column-zero envelope."""
    cursor = size = 0
    saw_bullet = separated = False
    for _ in range(32):
        chunk = text[cursor : cursor + 8193]
        newline = chunk.find("\n")
        line = (chunk if newline < 0 else chunk[:newline]).removesuffix("\r")
        bullet = re.fullmatch(r"(?:[-*+]|[0-9]{1,9}[.)]) ([^\r\n]+)", line)
        if bullet:
            payload = re.sub(r"^\[[ xX]\][ \t]+", "", bullet.group(1).lstrip(" \t"))
            if newline < 0 or not payload or re.match(r"(?:>|<|`{3}|~{3})", payload):
                return None
            saw_bullet, separated = True, False
        elif line.strip(" \t"):
            return None if saw_bullet and not separated else cursor
        elif newline < 0:
            return cursor
        else:
            separated = True
        consumed = chunk[: newline + 1]
        try:
            size += len(consumed.encode("utf-8"))
        except UnicodeError:
            return None
        if size > 8192:
            return None
        cursor += len(consumed)
    return None if saw_bullet and not separated else cursor


def normalize_header(text: str, *, seat: str | None = None, context: str = "message") -> str:
    """Normalize one complete leading header, optionally only this seat's own.

    Repo commas take one hair space; recipient/platform commas take two.  A real Operator
    letter identifies the thread, so its sender never carries a runtime session suffix.
    Legacy transport-only tags remain readable; message context keeps their correlation fragment.  PR
    contexts omit repositories and private transport, preserving the literal Kody command line.  Only PR
    body context permits a bounded initial list of readiness criteria before the envelope.
    PR prose refuses code indentation; title context keeps its established prefix recognition."""
    if context not in ("message", "github-pr", "github-pr-body", "github-pr-title"):
        raise ValueError("unknown header context")
    is_pr = context != "message"
    command = re.match(r"^@kody start-review\r?\n", text) if is_pr else None
    prefix = command.group(0) if command else ""
    criteria = ""
    if context == "github-pr-body":
        description = text[len(prefix) :]
        end = _pr_body_prefix(description)
        if end is None:
            return text
        criteria = description[:end]
        prefix += criteria
    content = text[len(prefix) :]
    pattern = _HEADER
    if context in ("github-pr", "github-pr-body"):
        pattern = _BODY_HEADER if criteria.strip(" \t\r\n") else _PR_FIRST_HEADER
    match = pattern.match(content)
    if not match:
        return text
    parts = re.split(r"\s*(?:→|->)\s*", match.group("inner"), maxsplit=1)
    if len(parts) > 1 and re.search(r"→|->", parts[1]):
        return text
    front = parts[0]
    peers = [part.strip() for part in parts[1].split(",")] if len(parts) > 1 else []
    folder = front.split("📁", 1)
    if not is_pr and len(folder) > 1 and "📁" in folder[1]:
        return text
    sender = _SENDER.fullmatch(folder[0].strip())
    if not sender or (seat is not None and sender.group("seat") != seat):
        return text
    repos = [part.strip() for part in folder[1].split(",")] if len(folder) > 1 else []
    if (not is_pr and len(folder) > 1 and not all(repos)) or (len(parts) > 1 and not all(peers)):
        return text
    thread = sender.group("thread") or ""
    transport = sender.group("transport") or ""
    if is_pr or _OPERATOR_THREAD.fullmatch(thread):
        transport = ""
    inner = sender.group("seat") + transport
    if thread:
        inner += ":" + AFTER_COLON + thread
    if repos and not is_pr:
        inner += BEFORE_FOLDER + "📁" + AFTER_FOLDER + REPO_SEPARATOR.join(repos)
    if peers:
        inner += ARROW + RECIPIENT_SEPARATOR.join(peers)
    return prefix + match.group("lead") + "[" + inner + "]" + content[match.end() :]
