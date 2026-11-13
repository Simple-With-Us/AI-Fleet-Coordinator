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


def normalize_header(text: str, *, seat: str | None = None) -> str:
    """Normalize one complete leading header, optionally only this seat's own.

    Repo commas take one hair space; recipient/platform commas take two.  A real Operator
    letter identifies the thread, so its sender never carries a runtime session suffix.
    Legacy transport-only tags remain readable and keep their correlation fragment."""
    match = _HEADER.match(text)
    if not match:
        return text
    parts = re.split(r"\s*(?:→|->)\s*", match.group("inner"), maxsplit=1)
    if len(parts) > 1 and re.search(r"→|->", parts[1]):
        return text
    front = parts[0]
    peers = [part.strip() for part in parts[1].split(",")] if len(parts) > 1 else []
    folder = front.split("📁", 1)
    if len(folder) > 1 and "📁" in folder[1]:
        return text
    sender = _SENDER.fullmatch(folder[0].strip())
    if not sender or (seat is not None and sender.group("seat") != seat):
        return text
    repos = [part.strip() for part in folder[1].split(",")] if len(folder) > 1 else []
    if (len(folder) > 1 and not all(repos)) or (len(parts) > 1 and not all(peers)):
        return text
    thread = sender.group("thread") or ""
    transport = sender.group("transport") or ""
    if _OPERATOR_THREAD.fullmatch(thread):
        transport = ""
    inner = sender.group("seat") + transport
    if thread:
        inner += ":" + AFTER_COLON + thread
    if repos:
        inner += BEFORE_FOLDER + "📁" + AFTER_FOLDER + REPO_SEPARATOR.join(repos)
    if peers:
        inner += ARROW + RECIPIENT_SEPARATOR.join(peers)
    return match.group("lead") + "[" + inner + "]" + text[match.end() :]
