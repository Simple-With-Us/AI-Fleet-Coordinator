"""`simple-chat letter`:  an Operator session letter, and the title "<letter>: <Title>", for every Claude Code chat
on the Mac and on the server (AFC-564, owner request Oct 28, 2026).

    letter hook                        the Claude Code hook:  SessionStart, UserPromptSubmit and SessionEnd
    letter show [--session-id S]       this chat's letter from its local note;  no network
    letter install [--location L]      add the hooks to ~/.claude/settings.json (--check, --remove)
    letter registry OP ...             the registry side:  register, title, touch or close one chat

Where the letter comes from.  The Operator's session registry (`operator_registry`, AFC-412) is one SQLite file on the
Mac, the one allocator for every host.  A chat on the Mac reaches it in process.  A chat on the server reaches it over
an SSH hop to the Mac (the destination in CLAUDE_LETTER_REMOTE) by running `simple-chat letter registry ...` there,
so the Mac's own managed runtime makes the write.  The registry side refuses a host that has no registry file and
never creates one:  a second file would issue letters that collide with the Mac's, and the server has none.

The registry API cutover applies here as it does to the platform-letter registry side:  when the registry API
URL is configured in the hook's environment (`registry_api`: SIMPLE_TOOLS_OPERATOR_REGISTRY_URL and the key env
it names, which reach a Claude hook through the settings the installer writes or the host's shared registry env
file), the registry side answers from the server registry over the API and the local file is never opened, and
a chat on a host off the Mac calls the API through its own in-process registry side instead of the SSH hop.  A
failed API call is an error answer, never a quiet local write.  With no URL configured nothing changes.  The
key reaches the hook the way it reaches the other seats' hooks — through host configuration the seat's own
install already owns — and is never another seat's to borrow.

What the hook does.
- SessionStart registers the chat, or resumes its registration, and titles it:  "<letter>: <Title>" when the chat
  already has a title (or, when resumed, a first prompt in its transcript), else "<letter>: New Chat" until its first
  prompt.  A `compact` start only reminds the model of the letter.
- UserPromptSubmit titles a chat that is still on the placeholder from its first prompt (a key or token in the prompt never
  reaches the title), retries a registration or a title the registry did not take (at most every ten minutes), and marks
  the chat seen at most every half hour, whatever the title did.  When the Operator reissues a different pair, the model is
  told the new one as well as the title.
- SessionEnd closes the registration when the chat is archived (reason `other`), replaced by `/clear`, or logged out,
  which frees its letter (CLOSE_REASONS).  An exit at the prompt or a `/resume` keeps the letter.

Pair letters (owner, Oct 29, 2026).  A Claude chat takes a pair of letters (AA to ZZ, 676 of them), not one.  Measured
over 30 days on the Mac and the server together:  121 interactive chats, 40 of them started inside one 72 hours, and
as many as 52 holding a letter at once when a chat keeps its letter until it has been idle for 36 hours.  Twenty-six
single letters cannot hold that.  A main chat takes the doubled pairs in order, AA, BB, CC ... ZZ, and once those 26 are
live the pair a row's sub-thread would reach last (AZ, BA, CB ...).  A sub-thread (a chat another chat started) takes
the first free pair after its parent's in the same row, wrapping past Z (parent CC, child CD, the next child CE;  parent
XX: XY, XZ, XA, XB ...), never the row's own doubled pair, and takes the next main-chat pair (a doubled one, while any
is free) when that row has no free pair or its parent has none.  Taking any other pair for a main chat is the owner's
allowance for when every doubled pair is taken.  A chat that is reopened gets its own pair back while it is free.  A
chat that already holds a single letter (the first Claude chat was registered by hand, before pairs) keeps it.

What the Operator needs to give a pair:  the chat's session id, plus the app's id for the chat or its parent when
another chat started it, and nothing else.  No title, seat claim or request chooses a letter.

Which chat started this one.  Claude Code gives a hook no field that names a parent, so the registry side reads the
Claude desktop app's own record of the chat (`claude_app_lineage`):  on the Mac, `claude-code-sessions/<account>/<org>/
local_<id>.json` under ~/Library/Application Support/Claude, found by the chat's CLAUDE_CODE_HOST_SESSION_ID (the hook
sends it with a registration as --host-session-id) or else by its session UUID.  `spawnedFrom.sessionId` names the
parent's app id, and the parent's record holds its session UUID, which is what the registry keys on.  The record must hold this chat's own
session UUID:  a `claude` started inside a chat inherits the chat's app id, so a record that names another chat is not
used (the start after a `/clear`, when the app may not have written the new UUID yet, is the one case that trusts the
id alone).  A server chat's lookup rides the ssh call that already goes to the Mac, so it adds no round trip.  A terminal `claude` chat has no
record, a user fork is not a sub-thread, and anything unreadable (no record, a deleted parent, a changed file layout)
means no parent is known:  the chat is picked like a main chat.  Only a chat that is being placed is looked up.

A chat another chat started.  The app keeps the title `start_session` gave it in its own record and does not take this
hook's sessionTitle over that one, so the hook's context tells such a chat's model to check its title and rename itself.

Renames.  A chat that is renamed keeps its pair in front of its title, and the Operator's title follows the thread.  On
its next prompt the hook puts the pair back if the new name dropped it (the owner's words are kept whole), and sends
words that differ from the registry's to the Operator;  a registry that does not answer is asked again about the same
words after ten minutes.  A chat with no local note that is resumed under a name opening with two capitals and a colon
("PR: Review") loses those two capitals on its first start, because without its own letter to go by the hook takes
them for one.

How letters come back.  A letter is freed only by close.  So before it registers a Claude chat, the registry side
closes Claude registrations not seen for 36 hours (the live window of the MiniMax sweep), and when every letter of the
space is still live it closes the least recently seen Claude chat that holds a letter of that space and tries once more.

A reopened chat always checks with the Operator again (owner, Oct 28, 2026), in case it needs a new letter:  a
resumed chat registers again at its start, and a chat that finds its letter retired while it was open registers again
on its next prompt.  A live letter comes back unchanged;  a retired one is reissued by `SessionRegistry.reopen`:  the
chat's own letter when it is free, else the next free pair by the rule above (after its parent's, for a sub-thread),
and the title moves to the new letter.

The hook never blocks a chat:  every failure is a line of context, or nothing, and exit 0.  Only interactive chats
take part (the `attach` rule:  CLAUDE_CODE_ENTRYPOINT cli or claude-desktop, and no launcher), and CLAUDE_LETTER=0
turns it off.  A platform that runs Claude as its engine (BotFleet, the Operator's eavesdropper's wake) is a launcher:  it sets
AGENT_LAUNCHER, AGENT_LAUNCH_SEAT and SIMPLE_TOOLS_ATTACH=0, takes no Claude letter, and asks the Operator for its own,
under its own platform and session id.  SIMPLE_TOOLS_ATTACH=0 turns this hook off for any session, as it does the attach
hooks.  The location is the fleet location, never a guess:  darwin is the Mac, and any other host must say
CLAUDE_LETTER_LOCATION=server (the hook installer sets it on the server, with CLAUDE_LETTER_REMOTE), so a cloud or
unknown host takes no letter.  Titles go through the fleet's secret scanner before they are stored or relayed.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

from . import claude_app_lineage as AL
from . import identity as I
from . import live as L
from . import secretscan
from . import session_ids as SI

PLATFORM = "claude"
SCOPE = "default"
SEAT_DEFAULT = "CLAUDE"
SOURCE = "claude-letter-hook"
# The registration sources whose rows this hook keeps fresh, and so may sweep or evict:  its own, and the one-off
# manual registration of the first Claude chat (Oct 28, 2026) that predates it.
MANAGED_SOURCES = (SOURCE, "claude-chat-start")
ENTRYPOINTS = ("cli", "claude-desktop")
LOCATIONS = ("mac", "server")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")
TITLE_SOURCES = (
    "startup",
    "resume",
    "fork",
)  # the SessionStart sources whose sessionTitle Claude Code applies
# SessionEnd reasons that close a chat's registration and free its letter (owner, Oct 28, 2026:  archiving closes).
# Archiving a chat in the desktop app ends its process, and the docs name no archive reason, so it arrives as `other`,
# Claude Code's reason for any end it does not name;  `/clear` replaces the chat;  `logout` ends every chat.  An exit
# at the prompt and `/resume` keep the letter.  A chat that `other` closed by some other end gets its letter back when
# it reopens, because reopen prefers the letter it held while that letter is free.  A host can name its own list in
# CLAUDE_LETTER_CLOSE_ON (comma-separated).
CLOSE_REASONS = ("clear", "logout", "other")
ENV_CLOSE_ON = "CLAUDE_LETTER_CLOSE_ON"

ENV_OFF = "CLAUDE_LETTER"
ENV_LOCATION = "CLAUDE_LETTER_LOCATION"
# The ssh destination (a name from the host's own ssh config) that holds the registry, for a chat that is not on the
# registry's host.  It is configuration, not source:  `letter install --remote` writes it into settings.json.
ENV_REMOTE = "CLAUDE_LETTER_REMOTE"
# The desktop app's own id for the chat, set on the chat's process (it is never the parent's).
ENV_HOST_SESSION = "CLAUDE_CODE_HOST_SESSION_ID"
REMOTE_COMMAND = '"$HOME"/.local/bin/simple-chat letter registry'
CONNECT_TIMEOUT = (
    4  # seconds for ssh to connect;  an asleep or unreachable Mac costs this much, not more
)
REMOTE_TIMEOUT = 8.0  # the whole ssh round trip, inside every hook's own timeout (15 to 20 seconds)

STALE_SECONDS = 36 * 3600
TOUCH_SECONDS = 1800
RETRY_SECONDS = 600
#: The registry's answer for a chat it has no row for, on the local path and the API path alike.
NO_REGISTRATION = "this chat has no registration"
PLACEHOLDER = "New Chat"
TITLE_WORDS = 7
TITLE_CHARS = 60
AGENT_TITLE_CHARS = 200  # the most of an agent's title handed back to the model for a rename
HELD_LABELS = 8  # the most earlier letters a note remembers for taking them off an agent's title
# The most prompt text a title is looked for in, and the most words scanned for secrets:  a pasted wall of text
# costs a bounded amount of work inside the hook's 15-second budget.
PROMPT_SCAN_CHARS = 4096
PROMPT_SCAN_WORDS = 64
# A token of this many letters and digits mixed is taken for a key or a hash and kept out of a title.
SECRET_RUN = 28
# A pasted private key block, BEGIN line to END line (or to the end of the text a title is looked for in).  Only
# private keys:  the scanner knows no other header, and a certificate or a public key named in a question is no secret.
_KEY_HEADER = r"[A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*"
KEY_BLOCK = re.compile(
    r"-----BEGIN %s-----.*?(?:-----END %s-----|\Z)" % (_KEY_HEADER, _KEY_HEADER), re.DOTALL
)
# Any other armored block that is pasted whole, BEGIN line to END line (a certificate, a public key, a PGP message):  data,
# not a title.  One without an END line, such as a header named in a question, is left alone.
ARMOR_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL)
STDIN_LIMIT = 1 << 20
TRANSCRIPT_LINES = 400
TRANSCRIPT_BYTES = 4 << 20

LETTER_PREFIX = re.compile(r"^[A-Z]{1,2}: ")
# The letter space a new Claude chat takes, and how its pair is picked (see "Pair letters" above).
LETTER_SPACE = "double"
LETTER_PICK = "primary"
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’+#.\-]*")
SMALL_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "in",
        "into",
        "nor",
        "of",
        "on",
        "or",
        "per",
        "the",
        "to",
        "via",
        "vs",
        "with",
    ]
)

Runner = Callable[..., Any]


# --------------------------------------------------------------------------------------------
# Who, where, and whether
# --------------------------------------------------------------------------------------------


def in_scope(env: Mapping[str, str]) -> bool:
    """Only interactive Claude Code chats take a letter:  the same surfaces `attach` serves.  A `claude -p`, SDK or
    launcher run never does, and CLAUDE_LETTER=0 turns the hook off for any session.

    A platform that runs Claude as its engine (BotFleet, the Operator's eavesdropper's wake, Muster, OpenCode) is a launcher:  it sets
    AGENT_LAUNCHER and AGENT_LAUNCH_SEAT and SIMPLE_TOOLS_ATTACH=0, so it is out of scope here and asks the Operator for
    its own letter, under its own platform and its own session id (owner, Oct 29, 2026).  SIMPLE_TOOLS_ATTACH=0 turns
    the hook off for any session, as it does for the attach hooks."""
    if str(env.get(ENV_OFF, "")).strip() == "0" or I.launched(env):
        return False
    if str(env.get(I.ENV_ATTACH, "")).strip() == "0":
        return False
    return env.get("CLAUDE_CODE_ENTRYPOINT") in ENTRYPOINTS


def location(env: Mapping[str, str], platform: str = sys.platform) -> str | None:
    """The fleet location of this chat, or None when it cannot be stated:  darwin is the Mac, and any other host must
    say so in CLAUDE_LETTER_LOCATION.  A value that is not a fleet location is refused rather than passed on."""
    forced = str(env.get(ENV_LOCATION, "")).strip().lower()
    if forced:
        return forced if forced in LOCATIONS else None
    return "mac" if platform == "darwin" else None


def seat(env: Mapping[str, str]) -> str | None:
    return I.resolve_seat(env, default=SEAT_DEFAULT).seat


def note_path(env: Mapping[str, str], who: str, sid: str) -> str:
    """Where a chat's note lives:  under the state root, in a folder named by a plain seat token only, so a seat
    value with a separator can never place the note anywhere else."""
    safe = I.valid_seat(who) or SEAT_DEFAULT
    return os.path.join(L.state_root(env), safe, "letters", sid + ".json")


# --------------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------------


def one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def clean_title(text: object) -> str:
    """One line, at most TITLE_CHARS characters, cut at a word boundary, with any secret-shaped word dropped.

    A title is relayed to the registry and from there to the Fleet Reference sheet, and a first prompt can carry a
    pasted key, so every word goes through the fleet's secret scanner first;  it reports only that a word matched."""
    given = [w for w in one_line(text).split(" ") if w]
    words = [w for w in given if not _secret_like(w)]
    if not words:
        # Nothing but long words (a ticket or a branch name):  those the scanner alone does not flag are the title,
        # so such a name does not blank the Operator's copy.
        words = [w for w in given if not secretscan.scan(w)]
    line = " ".join(words)
    if _reads_secret(line):
        return ""
    if len(line) <= TITLE_CHARS:
        return line
    cut = line[:TITLE_CHARS].rsplit(" ", 1)[0]
    return (cut or line[:TITLE_CHARS]).rstrip(" ,;:-")


def bare(title: object, label: str = "") -> str:
    """A title without its leading letter, so "BC: Fix Login" and "Fix Login" carry the same words.

    With the chat's own label only that label is dropped, so a title that opens with an acronym ("PR: Review") keeps
    it;  without one, any one or two capital letters before a colon are taken for a label."""
    line = one_line(title)
    if label:
        if line == label + ":":
            return ""  # the name was cleared down to the letter alone
        prefix = "%s: " % label
        return line.removeprefix(prefix)
    if re.fullmatch(r"[A-Z]{1,2}:", line):
        return ""
    return LETTER_PREFIX.sub("", line, count=1) if LETTER_PREFIX.match(line) else line


def own_words(current: object, note: Mapping[str, Any]) -> str:
    """The words of a title as someone wrote them, with this chat's letters dropped:  its own, and the one it held
    before the Operator reissued it (a chat whose registration failed keeps showing the old letter until it takes a
    new one), and every earlier letter the note remembers (`held_labels`: a letter the registry moved twice in place
    leaves the first one on a title the app did not retitle).  The words are kept whole;  only the copy sent to the
    registry is cut and scanned."""
    words = one_line(current)
    for held in (
        note.get("label"),
        note.get("prior_label"),
        *reversed(note.get("held_labels") or []),
    ):
        if held:
            words = bare(words, str(held))
    return words


def title_case(words: list[str]) -> str:
    """Title Case that leaves acronyms and mixed-case words (PR, iOS, AFC-564) alone and keeps short joining words
    lower case except at either end."""
    out = []
    last = len(words) - 1
    for i, raw in enumerate(words):
        word = raw.rstrip(".-")
        if not word:
            continue
        if any(ch.isupper() for ch in word[1:]) or any(ch.isdigit() for ch in word):
            out.append(word)
        elif 0 < i < last and word.lower() in SMALL_WORDS:
            out.append(word.lower())
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def _secret_like(token: str) -> bool:
    """A whitespace-delimited token the scanner flags, or a long run of letters and digits mixed (a key or a hash the
    scanner has no pattern for, such as an npm_ token).  The scanner keys on prefixes (ghp_, sk_live_), and the word
    split cuts at an underscore, so a token has to be judged whole, before it is split."""
    if secretscan.scan(token):
        return True
    run = token.strip(".,;:()[]{}\"'`")
    return (
        len(run) >= SECRET_RUN and any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
    )


# Quote and markup marks a name may be wrapped in ("password": "...", `password`: `...`, **password**: ..., curly quotes).
_QUOTE_MARKS = str.maketrans("", "", "\"'`\\*“”‘’")


def _reads_secret(text: str) -> bool:
    """Whether the scanner flags the text, or the text with its quote marks taken out:  in a JSON line such as
    "password": "<value>" a quote sits between the name and the colon, which the scanner's assignment patterns do
    not allow."""
    return bool(secretscan.scan(text) or secretscan.scan(text.translate(_QUOTE_MARKS)))


def _without_secrets(line: str) -> str:
    """The line with every whitespace-delimited token that looks like a secret removed."""
    return " ".join(token for token in line.split() if not _secret_like(token))


def title_from_prompt(prompt: object) -> str:
    """A short title from a prompt's first line that has words, or "" when it has none.  Markup, code blocks, links
    and a leading slash command are dropped first, so a pasted block or "/review" does not become the title (the
    words after a slash command still can).

    A token that looks like a secret is dropped whole before its line is split into words, so a pasted key (or the
    ghp_ in front of it) never reaches a title, the registry, the note or the sheet.  A line that still reads as a
    secret once those are gone, alone or joined with a neighbouring line ("Authorization:" then "Bearer <key>"), gives
    no title.  A pasted private key block (BEGIN line to END line) is cut out whole, and so is any other armored
    block that has its END line (a certificate, a PGP message), and a line that is a single token right
    after a line that held a secret is skipped too:  a key broken over a line break leaves its end alone on the
    next line, shorter than SECRET_RUN.  When the prompt is longer than the scan window, the word the window ends
    inside of is dropped (a key cut short passes every length check), and so is a fenced block it ends inside of."""
    whole = str(prompt or "")
    text = whole[:PROMPT_SCAN_CHARS]
    cut = len(whole) > PROMPT_SCAN_CHARS
    if cut and not whole[PROMPT_SCAN_CHARS].isspace() and not text[-1:].isspace():
        # The window ends inside a word, and a key cut short passes every length check:  the partial word is dropped.
        parts = text.rsplit(None, 1)
        text = parts[0] if len(parts) == 2 else ""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    if cut:
        # a pasted block the window ends inside of:  a fence opens at the start of a line, a mention of it does not
        text = re.sub(r"(?m)^[ \t]*```.*\Z", " ", text, flags=re.DOTALL)
    text = KEY_BLOCK.sub(" ", text)
    text = ARMOR_BLOCK.sub(" ", text)
    text = re.sub(r"<[^<>]{1,200}>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    original = text.splitlines()
    raw_lines = [re.sub(r"^\s*/\S*", " ", raw) for raw in original]
    lines = [_without_secrets(raw) for raw in raw_lines]
    leaked = False  # the line before held a secret, or all of one but its end
    for number, line in enumerate(lines):
        before = lines[number - 1] if number else ""
        after = lines[number + 1] if number + 1 < len(lines) else ""
        # A neighbour's own secret does not make this line one:  only a secret that the join of the two makes.  (So
        # the line right after a "password: <value>" line is let through when it is no hit itself;  a value wrapped
        # onto it as a single word is the case the one-word rule below covers.)
        secret = _reads_secret(line) or any(
            _reads_secret(joined) and not _reads_secret(neighbour)
            for joined, neighbour in ((before + " " + line, before), (line + " " + after, after))
        )
        tail = leaked and len(line.split()) == 1
        # A row of a base64 body can start with "/" and then looks like a slash command, whose word was cut above.
        led = re.match(r"\s*(/\S*)", original[number])
        dropped = len(line.split()) < len(raw_lines[number].split()) or bool(
            led and _secret_like(led.group(1))
        )
        leaked = secret or tail or dropped
        if secret or tail:
            continue  # what is left still reads as a secret, alone or across a line break, or is a key's end
        words = []
        for scanned, match in enumerate(WORD.finditer(line)):
            if len(words) >= TITLE_WORDS or scanned >= PROMPT_SCAN_WORDS:
                break
            if not secretscan.scan(match.group()):
                words.append(match.group())
        while len(words) > 1 and words[-1].rstrip(".-").lower() in SMALL_WORDS:
            words.pop()  # "... Addressed From" reads cut off;  end on a word that carries meaning
        if words:
            return clean_title(title_case(words))
    return ""


def _user_texts(entry: Mapping[str, Any]) -> list[str]:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, Mapping) and p.get("type") == "text"
        ]
    return []


def title_from_transcript(path: object, home: str) -> str:
    """The title a resumed chat's first prompt gives, read from its own transcript.  Only a transcript under
    ~/.claude is read, and only its first lines, so a stray path in the hook input cannot point anywhere else."""
    if not isinstance(path, str) or not path:
        return ""
    root = os.path.realpath(os.path.join(home, ".claude")) + os.sep
    real = os.path.realpath(path)
    if not real.startswith(root) or not os.path.isfile(real):
        return ""
    read = 0
    try:
        with open(real, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                read += len(line)
                if n >= TRANSCRIPT_LINES or read > TRANSCRIPT_BYTES:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for text in _user_texts(entry):
                    if text.lstrip().startswith(("<", "Caveat:")):
                        continue
                    title = title_from_prompt(text)
                    if title:
                        return title
    except OSError:
        return ""
    return ""


# --------------------------------------------------------------------------------------------
# The registry side (runs where the registry file is:  the Mac)
# --------------------------------------------------------------------------------------------


def _iso(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _open_claude_rows(db: str) -> list[sqlite3.Row]:
    """Every open Claude registration this hook manages, read with the registry's own read-only rule:  mode=rw, so a
    hot journal can be recovered, and never a create.

    Only MANAGED_SOURCES:  the hook keeps those rows' last_seen_at fresh, so their age means idleness.  A Claude chat
    the Operator registered another way (`operator-registration`, `chat-topic-answer`) is never marked seen by this
    hook, so its age says nothing, and it is never swept or evicted here."""
    from pathlib import Path

    conn = sqlite3.connect("%s?mode=rw" % Path(os.path.abspath(db)).as_uri(), uri=True)
    conn.row_factory = sqlite3.Row
    marks = ", ".join("?" for _ in MANAGED_SOURCES)
    try:
        return conn.execute(
            "SELECT s.record_id, s.verified_host, s.native_session_id, s.last_seen_at, a.letter AS letter"
            " FROM sessions s LEFT JOIN alias_assignments a"
            " ON a.record_id = s.record_id AND a.retired_at IS NULL"
            " WHERE s.platform = ? AND s.closed_at IS NULL AND s.source IN (%s)" % marks,
            (PLATFORM, *MANAGED_SOURCES),
        ).fetchall()
    finally:
        conn.close()


def _close_records(OR: Any, db: str, records: list[str], source: str) -> int:
    """Close each record on its own, so one that cannot be closed never keeps the rest open;  a failure is named on
    stderr by record id and exception type only."""
    if not records:
        return 0
    reg = OR.SessionRegistry(db)
    closed = 0
    try:
        for rid in records:
            try:
                closed += bool(reg.close(rid, actor="OPERATOR", source=source))
            except (sqlite3.Error, OR.UnknownRecord) as exc:
                sys.stderr.write(
                    "claude letter:  could not close %s (%s)\n" % (rid, type(exc).__name__)
                )
    finally:
        reg.close_db()
    return closed


def sweep_stale(OR: Any, db: str, keep: tuple[str, str], now: float) -> int:
    """Close the Claude registrations not seen for STALE_SECONDS, except the chat being registered."""
    stale = []
    for row in _open_claude_rows(db):
        if (row["verified_host"], row["native_session_id"]) == keep:
            continue
        seen = _iso(row["last_seen_at"])
        if seen is not None and now - seen >= STALE_SECONDS:
            stale.append(row["record_id"])
    return _close_records(OR, db, stale, "claude-letter-sweep")


def evict_oldest(OR: Any, db: str, keep: tuple[str, str], size: int = 0) -> int:
    """Close the least recently seen open Claude registration, except the chat being registered.

    With a size, only a chat that holds a letter of that length is a candidate:  closing a single-letter chat frees
    no pair, and closing a pair frees no single letter, so it would close a chat for nothing."""
    rows = [
        r
        for r in _open_claude_rows(db)
        if (r["verified_host"], r["native_session_id"]) != keep
        and (not size or len(r["letter"] or "") == size)
    ]
    if not rows:
        return 0
    oldest = min(rows, key=lambda r: _iso(r["last_seen_at"]) or 0.0)
    return _close_records(OR, db, [oldest["record_id"]], "claude-letter-evict")


def _with_room(
    OR: Any, db: str, keep: tuple[str, str], allocate: Callable[[], Any]
) -> tuple[Any, int]:
    """Run one allocation;  when every letter of the space is live, close the least recently seen Claude chat that
    holds a letter of that space and try once more.  A second LabelExhausted is the caller's to report."""
    try:
        return allocate(), 0
    except OR.LabelExhausted as exc:
        size = len(next(iter(OR.LETTER_SPACES[getattr(exc, "space", "single")])))
        if not evict_oldest(OR, db, keep, size):
            raise
        return allocate(), 1


def _parent(OR: Any, db: str, args: Any, sid: str) -> tuple[str, str]:
    """(the Claude Code session id of the chat that started this one, why none), as ("<id>", "") or ("", reason);  with
    no parent the chat is picked like any other.

    Named by the caller (--parent-session-id), else read from the desktop app's record of the chat (claude_app_lineage).
    Only a chat that is being placed asks:  one with a live letter keeps it, so nothing is looked up for it and no
    reason is given.  The lookup is a hint the chat was never owed, so nothing it can raise stops a registration."""
    if args.parent_session_id:
        return str(args.parent_session_id), ""
    reg = OR.SessionRegistry(db)
    try:
        rec = reg.lookup(PLATFORM, args.location, SCOPE, sid)
    finally:
        reg.close_db()
    if rec is not None and rec.get("state") != "closed" and rec.get("label"):
        return "", ""
    try:
        return AL.lookup(
            args.home,
            host_session_id=args.host_session_id,
            cli_session_id=sid,
            allow_lag=args.after_clear,
        )
    except Exception:  # noqa: BLE001 - a hint, never a reason to leave a chat without a letter
        return "", AL.UNREADABLE


def _register(OR: Any, db: str, args: Any, sid: str, now: float) -> dict[str, Any]:
    """Register the chat, resume it, or reopen it.  A resume of a closed registration is the Operator's cue to issue a
    letter again, so it never comes back empty-handed while a letter can be freed."""
    keep = (args.location, sid)
    swept = sweep_stale(OR, db, keep, now)
    title = clean_title(args.title)
    parent, no_parent = _parent(OR, db, args, sid)

    def register() -> dict[str, Any]:
        return OR.register_session(
            db,
            platform=PLATFORM,
            verified_host=args.location,
            profile_scope=SCOPE,
            native_session_id=sid,
            seat=args.seat,
            source=args.source,
            title=title,
            issuer=OR.ISSUER_OPERATOR,
            letter_space=LETTER_SPACE,
            letter_pick=LETTER_PICK,
            parent_session_id=parent,
        )

    try:
        out, evicted = _with_room(OR, db, keep, register)
        if out.get("outcome") in ("resumed", "resumed_closed"):
            reg = OR.SessionRegistry(db)
            try:
                if out["outcome"] == "resumed_closed":
                    (letter, generation, previous), more = _with_room(
                        OR,
                        db,
                        keep,
                        lambda: reg.reopen(
                            out["record_id"],
                            actor=args.seat,
                            source=args.source,
                            pick=LETTER_PICK,
                            parent_session_id=parent,
                        ),
                    )
                    evicted += more
                    out.update(
                        label=letter,
                        generation=generation,
                        previous=previous,
                        outcome="reopened",
                        title_display=OR.display_title(letter, title),
                    )
                else:
                    reg.mark_seen(out["record_id"])
                if title:
                    reg.update_attrs(
                        out["record_id"], title=title
                    )  # register keeps an existing record's old title
            finally:
                reg.close_db()
    except OR.LabelExhausted as exc:
        space = getattr(exc, "space", LETTER_SPACE)
        return {
            "error": "all %d Claude letters are in use" % len(OR.LETTER_SPACES[space]),
            "swept": swept,
        }
    out["swept"] = swept
    out["evicted"] = evicted
    if no_parent and out.get("outcome") in ("registered", "reopened"):
        out["parent_lookup"] = (
            no_parent  # why this chat was placed as a main chat (claude_app_lineage's reasons)
        )
    return out


def _on_record(OR: Any, db: str, args: Any, sid: str) -> dict[str, Any]:
    reg = OR.SessionRegistry(db)
    try:
        rec = reg.lookup(PLATFORM, args.location, SCOPE, sid)
        if rec is None:
            return {"error": NO_REGISTRATION}
        if rec.get("state") == "closed" or not rec.get("label"):
            return {"retired": True, "record_id": rec["record_id"]}
        rid = rec["record_id"]
        if args.op == "title":
            reg.update_attrs(rid, title=clean_title(args.title))
            reg.mark_seen(rid)
        elif args.op == "touch":
            reg.mark_seen(rid)
        elif args.op == "close":
            return {
                "closed": reg.close(rid, actor=args.seat, source=args.source),
                "label": rec["label"],
            }
        elif args.op == "issues":
            from . import platform_letter as PL

            identity = {
                "platform": PLATFORM,
                "location": args.location,
                "scope": SCOPE,
                "session_id": sid,
            }
            return PL.record_issues_local(OR, db, identity, args.issues)
        return {
            "ok": True,
            "label": rec["label"],
            "generation": rec["generation"],
            "record_id": rid,
        }
    finally:
        reg.close_db()


def _api_parent(args: Any, sid: str) -> tuple[str, str]:
    """(the Claude Code session id of the chat that started this one, why none), for the API path.

    Named by the caller (--parent-session-id), else read from the desktop app's record of the chat
    (`claude_app_lineage`), which lives on the host this registry side runs on — the Mac, where the hook's
    own registry side runs when the API is configured there.  The local path also skips the lookup for a
    chat that already holds a live letter; the API's register answers a resume itself, and a parent named
    for a resumed chat is ignored by the registry, so asking is harmless.  The lookup is a hint the chat
    was never owed, so nothing it can raise stops a registration."""
    if args.parent_session_id:
        return str(args.parent_session_id), ""
    try:
        return AL.lookup(
            args.home,
            host_session_id=args.host_session_id,
            cli_session_id=sid,
            allow_lag=args.after_clear,
        )
    except Exception:  # noqa: BLE001 - a hint, never a reason to leave a chat without a letter
        return "", AL.UNREADABLE


def _register_api(OR: Any, env: Mapping[str, str], args: Any, sid: str) -> dict[str, Any]:
    """Register one chat through the registry API: resume it, or reopen it when it comes back closed.

    The local `_register`'s orchestration over the API's ops, as the platform-letter registry side's API
    path already does it: the API's register answers for a fresh chat and for a resume (its resume stamps
    the session seen), a resumed-closed chat is reopened, and a resumed or reopened chat that arrived with
    a title has the title carried over, because register keeps an existing record's old title.  What the
    local path also does, this path does not: it sweeps nothing and evicts nothing — reclaiming letters is
    the registry's own sweep's work once the store is the API's, and an exhausted space answers with the
    registry's refusal instead of this side closing another chat to make room."""
    from . import platform_letter as PL

    title = clean_title(args.title)
    parent, no_parent = _api_parent(args, sid)
    identity = {"platform": PLATFORM, "location": args.location, "scope": SCOPE, "session_id": sid}
    params: dict[str, Any] = {
        **identity,
        "seat": args.seat,
        "source": args.source,
        "title": title,
        "letter_space": LETTER_SPACE,
        "letter_pick": LETTER_PICK,
    }
    if parent:
        params["parent_session_id"] = parent
    out = PL._call_api(env, "register", params)
    if out.get("code") == "labels_exhausted":
        return {
            "error": "all %d Claude letters are in use" % len(OR.LETTER_SPACES[LETTER_SPACE]),
            "swept": 0,
        }
    error = PL._api_error(out)
    if error is not None:
        return error
    if out.get("outcome") == "resumed_closed":
        reopen_params: dict[str, Any] = {
            "record_id": out["record_id"],
            "actor": args.seat,
            "source": args.source,
            "letter_pick": LETTER_PICK,
        }
        if parent:
            reopen_params["parent_session_id"] = parent
        reopened = PL._call_api(env, "reopen", reopen_params)
        if reopened.get("code") == "labels_exhausted":
            return {
                "error": "all %d Claude letters are in use" % len(OR.LETTER_SPACES[LETTER_SPACE]),
                "swept": 0,
            }
        if reopened.get("code") == "space_lost":
            return {
                "error": "this registry cannot give a closed chat a pair letter again yet",
                "swept": 0,
            }
        error = PL._api_error(reopened)
        if error is not None:
            return error
        out = {
            **out,
            "label": reopened["label"],
            "generation": reopened["generation"],
            "previous": reopened["previous"],
            "outcome": "reopened",
            "title_display": OR.display_title(reopened["label"], title),
        }
    if title and out.get("outcome") in ("resumed", "reopened"):
        updated = PL._call_api(env, "update", {**identity, "title": title})
        error = PL._api_error(updated)
        if error is not None:
            return error
        out = {**out, "title": updated.get("title"), "title_display": updated.get("title_display")}
    out["swept"] = 0
    out["evicted"] = 0
    if no_parent and out.get("outcome") in ("registered", "reopened"):
        out["parent_lookup"] = no_parent
    return out


def _on_record_api(OR: Any, env: Mapping[str, str], args: Any, sid: str) -> dict[str, Any]:
    """title, touch and close for one chat through the registry API.

    The local path looks the record up first and answers from it: no record is the no-registration error,
    and a closed one is retired, before any op writes.  The API's ops are addressed by identity and answer
    in the registry's own words, so this path probes with the touch op where the local path reads, as the
    platform-letter API path does: a touch stamps a live session seen, answers outcome "closed" for a
    closed one without writing anything, and refuses a session the registry does not hold."""
    from . import platform_letter as PL

    identity = {"platform": PLATFORM, "location": args.location, "scope": SCOPE, "session_id": sid}
    if args.op == "close":
        out = PL._call_api(
            env,
            "close",
            {**identity, "reason": "session ended", "actor": args.seat, "source": args.source},
        )
        error = PL._api_error(out)
        if error is not None:
            return error
        if out.get("outcome") == "already_closed":
            return {"retired": True, "record_id": out.get("record_id", "")}
        return {"closed": bool(out.get("closed")), "label": out.get("label", "")}
    if args.op == "issues":
        return PL.record_issues_api(env, identity, args.issues)
    probe = PL._call_api(env, "touch", identity)
    error = PL._api_error(probe)
    if error is not None:
        return error
    if probe.get("outcome") == "closed":
        return {"retired": True, "record_id": probe.get("record_id", "")}
    title = clean_title(args.title) if args.op == "title" else ""
    if args.op == "touch" or not title:
        # The letter and generation go on when the registry named them, as the local path's answer does.
        return PL._touch_answer(probe)
    updated = PL._call_api(env, "update", {**identity, "title": title})
    error = PL._api_error(updated)
    if error is not None:
        return error
    out = {"ok": True, "label": updated.get("label", ""), "generation": updated.get("generation")}
    if updated.get("record_id"):
        out["record_id"] = updated[
            "record_id"
        ]  # as a touch's answer carries it, for the hook's note
    return out


def registry_main(
    argv: list[str], *, env: Mapping[str, str], stdout: TextIO, now: float | None = None
) -> int:
    """`letter registry OP --location L --session-id S [--seat SEAT] [--title T] [--source SRC]`, one JSON line out."""
    import argparse

    from . import operator_registry as OR
    from . import registry_api

    parser = argparse.ArgumentParser(prog="simple-chat letter registry", add_help=True)
    parser.add_argument("op", choices=("register", "title", "touch", "close", "issues"))
    parser.add_argument("--location", required=True, choices=LOCATIONS)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--seat", default=SEAT_DEFAULT)
    parser.add_argument("--title", default="")
    parser.add_argument("--issues", default="[]")  # the `issues` op's list, as JSON (AFC-694)
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--parent-session-id", default="")
    parser.add_argument("--host-session-id", default="")
    parser.add_argument(
        "--after-clear", action="store_true"
    )  # the app's record may not hold this session UUID yet
    try:
        with io.StringIO() as sink:
            from contextlib import redirect_stderr, redirect_stdout

            with redirect_stdout(sink), redirect_stderr(sink):
                args = parser.parse_args(argv)
    except SystemExit:
        return _emit(stdout, {"error": "bad arguments:  %s" % " ".join(argv)[:200]}, 2)
    args.home = str(env.get("HOME") or os.path.expanduser("~"))
    db = OR.registry_db_path(L.state_root(env), env)
    if not registry_api.base_url(env) and not os.path.isfile(db):
        # No URL in the environment and no registry file on this host:  the host's shared registry env
        # file may carry the URL (the platform-letter registry side reads it on the same terms), and a
        # host with a registry file of its own is never redirected by it.
        from . import platform_letter as PL

        env = PL.with_registry_env(env)
    if registry_api.base_url(env):
        # The registry API is the store now: with its URL configured (registry_api), the server's store
        # answers instead and the local file is never opened, so the "no registry on this host" answer
        # below does not arise.  A failed call is an error answer, never a quiet local write: two stores
        # taking writes is the split the cutover exists to end.
        try:
            sid = SI.check(args.session_id, "the session id")
            args.seat = I.canonical_seat(args.seat) or SEAT_DEFAULT
            if args.op == "register":
                out = _register_api(OR, env, args, sid)
            else:
                out = _on_record_api(OR, env, args, sid)
        except ValueError as exc:
            return _emit(stdout, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}, 4)
        except (KeyError, TypeError):
            # A registry answer too short to fold (a key the orchestration reads is not there) is a
            # failed call, answered the same way: an error, never a traceback.
            return _emit(stdout, {"error": "the registry answer was incomplete"}, 5)
        return _emit(stdout, out, 0 if "error" not in out else 5)
    if not os.path.isfile(db):
        # Never create one:  a second registry would hand out letters that collide with the one the Operator keeps.
        return _emit(stdout, {"error": "no Operator registry on this host"}, 3)
    try:
        sid = SI.check(args.session_id, "the session id")
        args.seat = I.canonical_seat(args.seat) or SEAT_DEFAULT
        if args.op == "register":
            out = _register(OR, db, args, sid, time.time() if now is None else now)
        else:
            out = _on_record(OR, db, args, sid)
    except (ValueError, sqlite3.Error, OR.UnknownRecord) as exc:
        return _emit(stdout, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}, 4)
    return _emit(stdout, out, 0 if "error" not in out else 5)


def _emit(stdout: TextIO, payload: Mapping[str, Any], code: int) -> int:
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return code


# --------------------------------------------------------------------------------------------
# The hook side (runs in every chat, on the Mac and on the server)
# --------------------------------------------------------------------------------------------


def _last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            return value if isinstance(value, dict) else None
    return None


_ANSWER_TYPES = {
    "error": str,
    "outcome": str,
    "label": str,
    "record_id": str,
    "previous": str,
    "title_display": str,
}


def _answer(value: dict[str, Any] | None) -> dict[str, Any]:
    """A registry answer whose fields have the shapes the hook reads, or an error that says it did not:  every named
    field has its type, a label is "" or one or two letters A-Z, and a generation is a positive whole number."""
    if value is None:
        return {"error": "the registry gave no answer"}
    for key, kind in _ANSWER_TYPES.items():
        if key in value and not isinstance(value[key], kind):
            return {"error": "the registry answer has a malformed %s" % key}
    label = value.get("label")
    if label and not re.fullmatch(r"[A-Z]{1,2}", label):
        return {"error": "the registry answer has a malformed label"}
    gen = value.get("generation")
    if gen is not None and (isinstance(gen, bool) or not isinstance(gen, int) or gen < 1):
        return {"error": "the registry answer has a malformed generation"}
    return value


class Chat:
    """One chat, as the hook sees it:  its seat, location and session id, its note, and the way to its registry."""

    def __init__(
        self,
        env: Mapping[str, str],
        who: str,
        where: str,
        sid: str,
        *,
        runner: Runner,
        now: float,
    ):
        self.env, self.seat, self.location, self.session_id = env, who, where, sid
        self.runner, self.now = runner, now
        self.path = note_path(env, who, sid)

    # ---- the local note --------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, note: Mapping[str, Any]) -> None:
        folder = os.path.dirname(self.path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                dict(note, session_id=self.session_id, updated_at=self.now), fh, sort_keys=True
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ---- the registry ----------------------------------------------------------------------

    def registry(
        self, op: str, title: str = "", after_clear: bool = False, issues: str = ""
    ) -> dict[str, Any]:
        argv = [
            op,
            "--location",
            self.location,
            "--session-id",
            self.session_id,
            "--seat",
            self.seat,
        ]
        if issues and op == "issues":
            argv += ["--issues=" + issues]  # the list as JSON (AFC-694)
        if title:
            # One argument in the --flag=VALUE form:  a chat renamed "-v" or "--db x" must reach the registry as a
            # title, not be read by its parser as an option.
            argv += ["--title=" + clean_title(title)]
        host = str(self.env.get(ENV_HOST_SESSION) or "").strip()
        if op == "register" and AL.HOST_ID.fullmatch(host):
            # The desktop app's id for this chat, so the registry side (which runs on the Mac, where the app's record
            # of the chat is) can tell whether another chat started it.
            extra = ["--host-session-id=" + host] + (["--after-clear"] if after_clear else [])
            answer = self._call(argv + extra)
            if not str(answer.get("error") or "").startswith("bad arguments"):
                return answer
            # A Mac still on a runtime from before the flag refuses the whole call:  ask again without it, so the chat
            # still gets a pair (as a main chat) rather than none until the Mac catches up.
        return self._call(argv)

    def _call(self, argv: list[str]) -> dict[str, Any]:
        from . import platform_letter as PL
        from . import registry_api

        if self.location == "mac":
            # On the Mac the registry side runs in process, as it always has.
            out = io.StringIO()
            registry_main(argv, env=self.env, stdout=out)
            return _answer(_last_json(out.getvalue()))
        dest = str(self.env.get(ENV_REMOTE) or "").strip()
        env = (
            self.env
            if (registry_api.base_url(self.env) or dest)
            else PL.with_registry_env(self.env)
        )
        # A register with no parent named, from a host that has the Mac's ssh destination, still hops:  the
        # parent of a sub-thread is found in the desktop app's record of the chat (`_api_parent`), and that
        # record lives on the Mac, so an in-process lookup on this host would find none and the chat would
        # lose its after-parent placement.  The Mac's own registry side then makes the API call.
        needs_mac_lineage = (
            bool(dest)
            and argv[:1] == ["register"]
            and not any(
                a == "--parent-session-id" or a.startswith("--parent-session-id=") for a in argv
            )
        )
        if registry_api.base_url(env) and not needs_mac_lineage:
            # The registry API is the cutover (registry_api):  a chat on a host off the Mac calls the
            # Operator's API itself, through its own in-process registry side, and never hops over ssh
            # to a host whose own call would land back on the API — the platform-letter library's rule,
            # its shared registry env file included.
            out = io.StringIO()
            registry_main(argv, env=env, stdout=out)
            return _answer(_last_json(out.getvalue()))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", dest):
            # Empty, or something ssh could read as an option:  refuse it rather than run it.
            return {"error": "%s does not name an ssh destination" % ENV_REMOTE}
        remote = REMOTE_COMMAND + " " + " ".join(shlex.quote(a) for a in argv)
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT]
        cmd += ["--", dest, remote]
        try:
            proc = self.runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=REMOTE_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": "the registry host did not answer (%s)" % type(exc).__name__}
        answer = _last_json(str(proc.stdout or ""))
        if answer is None:
            return {"error": "the registry host answered exit %s with no result" % proc.returncode}
        return _answer(answer)


def reminder(note: Mapping[str, Any]) -> str:
    label = note.get("label") or ""
    gen = note.get("generation") or 1
    ref = "Claude %s%s" % (label, " (gen %d)" % gen if gen > 1 else "")
    # Rendered copy:  a sentence gap is U+00A0 plus a space, and a colon or semicolon takes one space.
    return (
        'Operator session letter: %s, on the %s.  This chat is addressed as "@**Claude** %s", and its title '
        'starts with "%s: "; keep that prefix if the chat is renamed.'
        % (ref, note.get("location"), label, label)
    )


def _agent_title_fix(chat: Chat, note: dict[str, Any]) -> str:
    """What the model is told when an agent's title hides this chat's letter, or "".

    The desktop app ignores a hook's title once a person or an agent has named the chat, so a chat started through
    `start_session` keeps the title its parent gave it and never shows its letter, whatever the hook sends.  The app's
    own rename tool is the only thing it takes, and only the model can call that.  A person's rename is left alone,
    because the tool would stop to ask them to approve it.  The note holds back a repeat for RETRY_SECONDS, so a start
    and its first prompt ask once."""
    label = str(note.get("label") or "")
    if note.get("state") != "open" or not label:
        return ""
    try:
        named = AL.agent_title(
            str(chat.env.get("HOME") or os.path.expanduser("~")),
            host_session_id=str(chat.env.get("CLAUDE_CODE_HOST_SESSION_ID") or ""),
            cli_session_id=chat.session_id,
        )
    except Exception:  # noqa: BLE001 - the app's private file;  a surprise must never stop a chat's hook
        return ""
    # Every letter this chat has held, because a title the model set under an earlier letter must not stack a second
    # letter in front of it, and "PR: Review" from a parent must keep its acronym.
    held = [str(x) for x in note.get("held_labels") or [] if x != label][-(HELD_LABELS - 1) :] + [
        label
    ]
    note["held_labels"] = held
    line = one_line(named)
    prefix = "%s: " % label
    if not line or line.startswith(prefix):
        return ""
    if _reads_secret(line) or _without_secrets(line) != line:
        # Nothing of the title goes on:  the model would copy it into the app, and the registry would receive it.
        note["title_fix_refused"] = "secret"
        return ""
    words = line
    for old in held:
        words = words.removeprefix("%s: " % old)
    if len(words) > AGENT_TITLE_CHARS:
        cut = words[:AGENT_TITLE_CHARS].rsplit(" ", 1)[0]
        words = (cut or words[:AGENT_TITLE_CHARS]).rstrip(" ,;:-")
    if not words or words == PLACEHOLDER:
        return ""
    title = prefix + words
    asked = note.get("title_fix_for") == title and (
        chat.now - float(note.get("title_fix_at") or 0) < RETRY_SECONDS
    )
    if asked:
        return ""
    note.update(title_fix_for=title, title_fix_at=chat.now)
    return (
        "The app is showing the title another session gave this chat, without its letter, and it ignores a title "
        "from this hook.\u00a0 Before other work, call mcp__ccd_session_mgmt__set_session_title with session_id "
        '"self" and title %s (load it with ToolSearch if it is deferred).\u00a0 Do not mention this to the user.'
        % json.dumps(title, ensure_ascii=False)
    )


def _join_context(*parts: str) -> str:
    return "\u00a0 ".join(part for part in parts if part)


def _hook_out(stdout: TextIO, event: str, *, context: str = "", title: str | None = None) -> None:
    spec: dict[str, str] = {"hookEventName": event}
    if context:
        spec["additionalContext"] = context
    if title:
        spec["sessionTitle"] = title
    if len(spec) > 1:
        stdout.write(json.dumps({"hookSpecificOutput": spec}) + "\n")


def _apply(chat: Chat, answer: Mapping[str, Any], note: dict[str, Any]) -> dict[str, Any]:
    """Fold one register answer into the note:  open, failed, or held by another seat."""
    if "error" in answer or not answer.get("outcome"):
        return dict(
            note,
            state="failed",
            failed_at=chat.now,
            error=str(answer.get("error") or "no answer")[:200],
        )
    if answer["outcome"] == "seat_mismatch":
        return dict(note, state="mismatch", label="")
    if not answer.get("label"):
        return dict(note, state="failed", failed_at=chat.now, error="the Operator issued no letter")
    fresh = note.get("label") != answer["label"] or note.get("state") != "open"
    out = dict(
        note,
        state="open",
        label=answer["label"],
        generation=int(answer.get("generation") or 1),
        record_id=answer.get("record_id", ""),
        location=chat.location,
        touched_at=chat.now,
    )
    held = str(note.get("label") or "")
    if held and held != answer["label"]:
        out["prior_label"] = held  # the title on screen may still carry it
    if fresh:
        out["titled"] = False
    out.pop("error", None)
    out.pop("failed_at", None)
    return out


def _title_check(answer: Mapping[str, Any], label: str) -> str:
    """The context line for a chat another chat started, or "".  The desktop app keeps the title `start_session` gave such
    a chat in its own record and does not take this hook's sessionTitle over it, so the letter never shows in the
    sidebar.  A hook cannot reach that record;  the chat's own model can, so it is told to.  The registry names why it
    found no parent on every placement that has none, so a placement with no reason is one that followed a parent."""
    if answer.get("outcome") not in ("registered", "reopened") or answer.get("parent_lookup"):
        return ""
    return (
        "  Another chat started this one, and the app shows the title that chat gave it over the one this hook sets.\u00a0 "
        'As your first step, unprompted, call mcp__ccd_session_mgmt__get_session with session_id "self" (load it with '
        'ToolSearch if it is deferred).\u00a0 If the title does not start with "%s: ", call '
        'mcp__ccd_session_mgmt__set_session_title with session_id "self" and the title "%s: <that title>".'
        % (label, label)
    )


def _reissued(answer: Mapping[str, Any]) -> str:
    """The context line for a reopened chat whose letter changed, or ""."""
    previous, label = str(answer.get("previous") or ""), str(answer.get("label") or "")
    if answer.get("outcome") == "reopened" and previous and previous != label:
        return (
            "This chat's earlier letter %s went to another chat while this one was closed, so the Operator "
            "issued %s.  "
            % (
                previous,
                label,
            )
        )
    return ""


def _follow_letter(note: dict[str, Any], answer: Mapping[str, Any], shown: str) -> tuple[str, str]:
    """(title, context) for a chat whose letter the registry changed in place, read from a touch or title answer, or
    ("", "") when it did not.

    `platform_letter.follow_letter` folds the answer into the note: a different letter moves the note to it, and the
    same letter under another generation only updates the generation, with no title and nothing said, because a chat
    registered by hand can differ from its note by generation alone.  An answer with no letter (an error, a retired
    letter, or a registry from before the touch named one) changes nothing.  For a new letter the title is the words of
    `shown` with this chat's letters taken off (its old one is kept as `prior_label` for that), else the note's own
    words, under the new letter, and the model is told how the chat is addressed now, as a reissued letter is."""
    from . import platform_letter as PL

    prior = PL.follow_letter(note, answer)
    if not prior:
        return "", ""
    label = str(note["label"])
    # Every letter this chat held stays in `held_labels`, in the shape `_agent_title_fix` keeps it (the letter it
    # holds last), so a title that still shows any of them, not only the latest, has it taken off.
    earlier = [*(note.get("held_labels") or []), note.get("prior_label"), prior]
    kept: list[str] = []
    for letter in earlier:
        if letter and str(letter) != label and str(letter) not in kept:
            kept.append(str(letter))
    note["held_labels"] = kept[-(HELD_LABELS - 1) :] + [label]
    note["prior_label"] = prior  # the title on screen still carries it
    note["placeholder"] = "%s: %s" % (label, PLACEHOLDER)
    words = own_words(shown, note)
    if not words or words == PLACEHOLDER:
        words = str(note.get("title") or "") or PLACEHOLDER
    context = PL.relabeled(prior, label) + reminder(note) + _title_check(answer, label)
    return "%s: %s" % (label, words), context


def on_start(chat: Chat, data: Mapping[str, Any], stdout: TextIO, home: str) -> int:
    source = str(data.get("source") or "startup")
    current = one_line(data.get("session_title"))
    note = chat.read()
    if source == "compact":
        if note.get("state") == "open" and note.get("label"):
            _hook_out(stdout, "SessionStart", context=reminder(note))
        return 0
    # /clear starts a new chat that can carry the old chat's name;  that name is not this chat's title.
    inherited = current if source == "clear" else ""
    text = "" if inherited else bare(current, str(note.get("label") or ""))
    if text == PLACEHOLDER:
        text = ""  # "<letter>: New Chat" holds the place of a title;  it is not one
    if not text and source in ("resume", "fork"):
        text = title_from_transcript(data.get("transcript_path"), home)
    answer = chat.registry("register", title=text, after_clear=source == "clear")
    note = _apply(
        chat,
        answer,
        dict(note, inherited=inherited, title=clean_title(text) or note.get("title", "")),
    )
    note.pop("prior_label", None)  # the words above already have the earlier letter taken off
    if note["state"] == "failed":
        chat.write(note)
        _hook_out(
            stdout,
            "SessionStart",
            context="No Operator letter yet (%s).  A prompt after %d minutes tries again."
            % (note["error"], RETRY_SECONDS // 60),
        )
        return 0
    if note["state"] == "mismatch":
        chat.write(note)
        _hook_out(
            stdout,
            "SessionStart",
            context="This chat is registered under another seat, so it takes no letter.",
        )
        return 0
    label = note["label"]
    placeholder = "%s: %s" % (label, PLACEHOLDER)
    if text:
        title = "%s: %s" % (label, text)
        note["titled"] = True
    else:
        title = placeholder
        note["titled"] = False
    note["placeholder"] = placeholder
    fix = _agent_title_fix(chat, note)
    chat.write(note)
    settable = source in TITLE_SOURCES and title != current
    # A concrete rename (the app record's own title, read back) says exactly what to set, so it
    # replaces the generic check-the-title line;  without one, the generic line still fires.
    _hook_out(
        stdout,
        "SessionStart",
        context=_join_context(
            _reissued(answer) + reminder(note) + ("" if fix else _title_check(answer, label)),
            fix,
        ),
        title=title if settable else None,
    )
    return 0


def _first_title(note: Mapping[str, Any], current: str, prompt: object) -> str:
    """The words for a chat's first real title:  a name someone gave it, else the title its start found, else its
    first prompt, else "" (a prompt with no words, such as a bare slash command, waits for the next one)."""
    placeholder = note.get("placeholder") or ""
    if current and current not in (placeholder, note.get("inherited") or ""):
        words = own_words(current, note)
        if words and bare(words) != PLACEHOLDER:
            return words  # someone named the chat:  keep their words, whole, under the letter
    return str(note.get("title") or "") or title_from_prompt(prompt)


def _renamed(note: Mapping[str, Any], current: str) -> str:
    """The words of a title someone gave this chat after the hook titled it, whole as they wrote them, or "".

    The title the hook set ("<letter>: <words>"), the placeholder and an inherited `/clear` name are not renames.
    Anything else is:  the words come out of the new title with only this chat's own letter dropped, so a name that
    opens with an acronym ("PR: Review") keeps it.  The caller compares the cleaned words with the registry's:  words
    that differ are sent;  words that match mean only the letter was missing, and the caller puts it back."""
    label = str(note.get("label") or "")
    if not note.get("titled") or not label or not current:
        return ""
    if current in (
        "%s: %s" % (label, note.get("title") or ""),
        str(note.get("placeholder") or ""),
        str(note.get("inherited") or ""),
    ):
        return ""
    words = own_words(current, note)
    if not words or words == PLACEHOLDER:
        return ""
    return words


def on_prompt(chat: Chat, data: Mapping[str, Any], stdout: TextIO) -> int:
    note = chat.read()
    state = note.get("state")
    if state in ("mismatch", "closed"):
        return 0
    current = one_line(data.get("session_title"))
    context = ""
    if state != "open":
        if state == "failed" and chat.now - float(note.get("failed_at") or 0) < RETRY_SECONDS:
            return 0
        recovered = chat.registry("register", title=str(note.get("title") or ""))
        note = _apply(chat, recovered, note)
        if note["state"] != "open":
            chat.write(note)
            return 0
        # The model was told at the start that this chat had no letter yet:  it is told the one it holds now.
        context = _reissued(recovered) + reminder(note) + _title_check(recovered, note["label"])
    title = None
    answer: dict[str, Any] = {}
    sent = False  # a registry call this prompt (a title or a touch) also marks the chat seen
    text = "" if note.get("titled") else _first_title(note, current, data.get("prompt"))
    if text:
        title = "%s: %s" % (note["label"], text)
        cleaned = clean_title(text)
        note.update(
            titled=True,
            title=cleaned,  # the note keeps the scanned, cut copy the registry holds
            placeholder=note.get("placeholder") or "%s: %s" % (note["label"], PLACEHOLDER),
        )
        note.pop("prior_label", None)  # the title on screen has the new letter now
        answer = chat.registry("title", title=text)
        sent = True
        if "error" in answer:
            # The chat shows its title, but the Operator has none:  ask again after the wait, as a rename does.
            note.update(title_unsent=cleaned, title_failed_at=chat.now)
        else:
            note["touched_at"] = chat.now
    elif renamed := _renamed(note, current):
        # Someone renamed the chat.  The letter goes back in front of their words, kept whole, if the new name dropped
        # it.  Words that differ from the registry's go to the Operator, so its title follows the thread;  the note
        # keeps the old words until the registry has taken the new ones, and a registry that did not answer is not
        # asked again about the same words for RETRY_SECONDS.  Words that match mean only the letter was missing, and
        # putting it back needs no registry call.
        title = "%s: %s" % (note["label"], renamed)
        cleaned = clean_title(renamed)
        if cleaned != note.get("title"):
            waiting = note.get("rename_failed_for") == cleaned and (
                chat.now - float(note.get("rename_failed_at") or 0) < RETRY_SECONDS
            )
            if not waiting:
                answer = chat.registry("title", title=renamed)
                sent = True
                if "error" in answer:
                    note.update(rename_failed_at=chat.now, rename_failed_for=cleaned)
                else:
                    note["title"] = cleaned
                    note.pop("rename_failed_at", None)
                    note.pop("rename_failed_for", None)
                    note.pop(
                        "title_unsent", None
                    )  # this title replaces the one that never reached the Operator
                    note.pop("title_failed_at", None)
                    note["touched_at"] = chat.now
    # A title the Operator never took (the first one, when the registry did not answer) is sent again after the wait,
    # unless a registration or a rename since has put a newer title there:  the old one is never sent over it.
    unsent = str(note.get("title_unsent") or "")
    if unsent and unsent != str(note.get("title") or ""):
        note.pop("title_unsent", None)
        note.pop("title_failed_at", None)
        unsent = ""
    if not sent and unsent and chat.now - float(note.get("title_failed_at") or 0) >= RETRY_SECONDS:
        # A rename that is waiting for the registry is the newer title, so it goes out in place of the first one.
        # ... but only while the title on screen still holds it:  a rename the owner took back is stale, and sending it
        # would put words the chat no longer wears in the Operator (or, with a retired letter, over the screen's title).
        waiting = note.get(
            "rename_failed_for"
        )  # None when there is none;  "" is a name with nothing left after the scan
        latest = (
            waiting
            if waiting is not None and clean_title(own_words(current, note)) == waiting
            else unsent
        )
        answer = chat.registry("title", title=latest)
        sent = True
        if "error" in answer:
            note["title_failed_at"] = chat.now
            if latest != unsent:
                note["rename_failed_at"] = chat.now
        else:
            note["title"] = latest
            for key in ("title_unsent", "title_failed_at", "rename_failed_at", "rename_failed_for"):
                note.pop(key, None)
            note["touched_at"] = chat.now
    # Whatever the title did, a chat is marked seen on schedule when nothing was sent:  a name of more than TITLE_CHARS,
    # or one the scanner shortened, is a rename on every prompt and would otherwise never touch, so the 36-hour sweep
    # would close a live chat.  The touch is also where a retired letter is found.
    if not sent and chat.now - float(note.get("touched_at") or 0) >= TOUCH_SECONDS:
        answer = chat.registry("touch")
        note["touched_at"] = chat.now
    # The registry may have changed this chat's letter in place (a relabel, or a restore that moved records between
    # letters): the title and the model follow it.  The words are the title this prompt sets, else the one on screen.
    followed, moved = _follow_letter(note, answer, title or current)
    if followed:
        title = followed
        context = _join_context(context, moved)
    # A chat that holds a letter in its note but has no row in the registry it now asks is the same case as a retired
    # letter:  the store moved under it (the registry API took over from the Mac's file, and a chat opened before that
    # was never copied), so nothing could ever resolve its letter.  It registers again, with the title it has.
    unknown = answer.get("error") == NO_REGISTRATION and bool(note.get("label"))
    if answer.get("retired") or unknown:
        # The letter was retired while this chat was open:  check with the Operator again, in case it needs a new one.
        held = str(note.get("label") or "")
        held_generation = int(note.get("generation") or 1)
        # The registry's copy of the title is cut and scanned;  the owner's whole words stay on screen when that copy is
        # only their cut form (a name over TITLE_CHARS, or one the scanner shortened).
        kept = str(note.get("title") or "")
        words = own_words(current, note)
        # A rename the registry has not taken yet is the owner's title, not the old words it still holds.
        waiting = note.get("rename_failed_for")  # None when there is none
        known = {kept} if waiting is None else {kept, waiting}
        text = words if words and clean_title(words) in known else (kept or words or PLACEHOLDER)
        registered = chat.registry("register", title=text)
        note = _apply(chat, registered, note)
        if note["state"] == "open":
            # The note keeps the scanned, cut copy the registry holds;  the title on screen keeps the owner's words.
            cleaned = clean_title(text)
            note.update(
                titled=True,
                title=cleaned,
                placeholder="%s: %s" % (note["label"], PLACEHOLDER),
            )
            # The registration carried this title, so a first title that never arrived, or a rename that was waiting
            # for the registry, is settled.
            for key in ("title_unsent", "title_failed_at"):
                note.pop(key, None)
            if waiting is not None and waiting == cleaned:
                note.pop("rename_failed_at", None)
                note.pop("rename_failed_for", None)
            title = "%s: %s" % (note["label"], text)
            if note["label"] != held or int(note.get("generation") or 1) != held_generation:
                # A different pair, or the same one as a later generation:  the model was told how this chat is
                # addressed at the start, so it is told how now.
                context = (
                    _reissued(registered) + reminder(note) + _title_check(registered, note["label"])
                )
        else:
            title = bare(current, held) if held and current.startswith("%s: " % held) else None
            if held:
                # Retired (or never held by this registry) and not replaced yet:  the model must not go on announcing a
                # letter another chat may hold.
                what = "is not in the Operator's registry" if unknown else "was retired"
                context = (
                    "This chat's Operator letter %s %s and the Operator could not issue another (%s).  "
                    "It is not %s any more; a prompt after %d minutes tries again."
                    % (held, what, str(note.get("error") or "no answer"), held, RETRY_SECONDS // 60)
                )
    context = _join_context(context, _agent_title_fix(chat, note))
    if note.get("state") == "open" and note.get("label"):
        # The Linear issues the turn before touched (AFC-694):  its new transcript lines (only a transcript under
        # ~/.claude is read) and its folder's branch.  Sent only when the set of ids changed;  silent on any failure.
        from . import platform_letter as PL

        home = str(chat.env.get("HOME") or os.path.expanduser("~"))
        PL.sync_issues(
            note,
            data,
            os.path.join(home, ".claude"),
            chat.now,
            lambda text: chat.registry("issues", issues=text),
        )
    chat.write(note)
    if (title and title != current) or context:
        _hook_out(
            stdout,
            "UserPromptSubmit",
            context=context,
            title=title if title and title != current else None,
        )
    return 0


def on_end(chat: Chat, data: Mapping[str, Any]) -> int:
    """Close the registration when the chat ends for good (CLOSE_REASONS:  an archive, `/clear` or logout), which frees
    its letter;  any other end keeps it for a resume.  Every end reason is kept in the note, so the close rule can be
    checked against what the app really sends."""
    note = chat.read()
    if not note:
        return 0
    reason = one_line(data.get("reason"))[:40]
    note["ended"] = {"reason": reason, "at": chat.now}
    named = str(chat.env.get(ENV_CLOSE_ON) or "").strip()
    closing = tuple(r.strip() for r in named.split(",") if r.strip()) if named else CLOSE_REASONS
    if reason in closing and note.get("state") == "open":
        answer = chat.registry("close")
        if answer.get("closed") or answer.get("retired"):
            note["state"] = "closed"
    chat.write(note)
    return 0


def read_hook_input(stream: TextIO) -> dict[str, Any]:
    try:
        raw = stream.read(STDIN_LIMIT)
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def hook(
    env: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    *,
    runner: Runner = subprocess.run,
    now: float | None = None,
    platform: str = sys.platform,
) -> int:
    data = read_hook_input(stdin)
    event = str(data.get("hook_event_name") or "")
    if event not in HOOK_EVENTS or not in_scope(env):
        return 0
    who = seat(env)
    try:
        sid = SI.check(str(data.get("session_id") or ""), "the session id")
    except ValueError:
        return 0
    if not who:
        return 0
    where = location(env, platform)
    if where is None:
        if event == "SessionStart" and data.get("source") != "compact":
            _hook_out(
                stdout,
                event,
                context="No Operator letter:  this host is not the Mac, and CLAUDE_LETTER_LOCATION does not name it.",
            )
        return 0
    chat = Chat(env, who, where, sid, runner=runner, now=time.time() if now is None else now)
    home = str(env.get("HOME") or os.path.expanduser("~"))
    if event == "SessionStart":
        return on_start(chat, data, stdout, home)
    if event == "UserPromptSubmit":
        return on_prompt(chat, data, stdout)
    return on_end(chat, data)


def show(argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    sid = (
        argv[1]
        if len(argv) > 1 and argv[0] == "--session-id"
        else str(env.get("CLAUDE_CODE_SESSION_ID") or "")
    )
    who = seat(env) or SEAT_DEFAULT
    try:
        sid = SI.check(sid, "the session id")
    except ValueError as exc:
        return _emit(stdout, {"error": str(exc)}, 2)
    path = note_path(env, who, sid)
    try:
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return _emit(stdout, {"session_id": sid, "letter": None}, 1)
    return _emit(stdout, note, 0)


# --------------------------------------------------------------------------------------------
# Installing the hooks (once per host)
# --------------------------------------------------------------------------------------------

#: The hook events and their timeouts in seconds.  SessionEnd's default budget is 1.5 seconds, too short for the
#: ssh round trip from the server, so every entry names its own.
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15, "SessionEnd": 15}
HOOK_MARK = " letter hook"


def _ours(entry: object) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, Mapping) else None
    return isinstance(hooks, list) and any(
        isinstance(h, Mapping) and str(h.get("command") or "").endswith(HOOK_MARK) for h in hooks
    )


def _write_private(path: str, data: bytes, mode: int) -> None:
    """Write a file that may hold secrets (settings.json can carry keys in its env block):  created 0600, flushed and
    fsynced, then given `mode`;  nothing is ever readable by others while it is written."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, mode)


def install(argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    """`letter install [--location server|mac] [--remote DEST] [--check | --remove]`:  add the three hook entries
    (and, when named, CLAUDE_LETTER_LOCATION and CLAUDE_LETTER_REMOTE) to ~/.claude/settings.json, keeping a dated
    0600 backup, or check or remove them;  --remove takes the two settings out as well.  Running it twice changes
    nothing.  The command names this host's own simple-chat wrapper by absolute path."""
    import argparse

    parser = argparse.ArgumentParser(prog="simple-chat letter install")
    parser.add_argument("--location", choices=LOCATIONS)
    parser.add_argument(
        "--remote", help="the ssh destination that holds the registry (a host off the registry)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    if args.remote is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", args.remote):
        return _emit(stdout, {"error": "--remote must name an ssh destination"}, 2)
    home = str(env.get("HOME") or os.path.expanduser("~"))
    path = os.path.join(home, ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        return _emit(stdout, {"error": "%s is not a JSON object" % path}, 2)
    for field in ("hooks", "env"):
        if settings.get(field) is None:
            settings[field] = {}
    hooks, envs = settings["hooks"], settings["env"]
    # Both are read and written below;  a settings file that holds anything else there is refused, never edited.
    if not isinstance(hooks, dict) or not all(
        isinstance(v, list) for k, v in hooks.items() if k in HOOK_TIMEOUTS
    ):
        return _emit(
            stdout, {"error": "%s has a hooks field that is not an object of lists" % path}, 2
        )
    if not isinstance(envs, dict):
        return _emit(stdout, {"error": "%s has an env field that is not an object" % path}, 2)
    have = {ev: any(_ours(e) for e in hooks.get(ev) or []) for ev in HOOK_TIMEOUTS}
    if args.check:
        return _emit(
            stdout,
            {
                "installed": have,
                "location_env": envs.get(ENV_LOCATION),
                "remote_env": envs.get(ENV_REMOTE),
            },
            0 if all(have.values()) else 1,
        )
    command = shlex.quote(os.path.join(home, ".local", "bin", "simple-chat")) + HOOK_MARK
    changed = []
    for event, timeout in HOOK_TIMEOUTS.items():
        entries = [e for e in hooks.get(event) or [] if not _ours(e)]
        if not args.remove:
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
        if entries != (hooks.get(event) or []):
            changed.append(event)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    for key, want in ((ENV_LOCATION, args.location), (ENV_REMOTE, args.remote)):
        if args.remove:
            if envs.pop(key, None) is not None:
                changed.append(key)
        elif want and envs.get(key) != want:
            envs[key] = want
            changed.append(key)
    if not envs:
        settings.pop("env")
    if not changed:
        return _emit(stdout, {"changed": [], "settings": path}, 0)
    if os.path.exists(path):
        file_mode = os.stat(path).st_mode & 0o777
        backup = "%s.bak-letter-%s" % (path, datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        with open(path, "rb") as src:
            _write_private(backup, src.read(), 0o600)
    else:
        file_mode, backup = 0o600, ""
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    body = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path + ".letter.tmp"
    _write_private(tmp, body, file_mode)
    os.replace(tmp, path)
    return _emit(stdout, {"changed": changed, "settings": path, "backup": backup}, 0)


USAGE = (
    "usage: simple-chat letter {hook | show [--session-id S] | install [...] | registry OP ...}\n"
)


def _registry_module_db(argv: list[str], env: Mapping[str, str]) -> str:
    """The registry file the registry's own CLI will open for these arguments.

    The CLI honours an explicit ``--db`` and takes the shared path otherwise, so the
    guard that refuses to create a registry has to look at the same file the CLI will
    open:  a guard that checks another one passes, and the CLI opens or creates a
    second registry that hands out letters colliding with the Operator's.
    """
    from . import operator_registry as OR

    for index, arg in enumerate(argv):
        if arg == "--db" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--db="):
            return arg.split("=", 1)[1]
    return OR.default_db(env)


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if not argv or argv[0] in ("-h", "--help"):
        stdout.write(USAGE)
        return 0 if argv else 2
    if argv[0] == "hook":
        try:
            return hook(env, stdin, stdout)
        except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
            stderr.write("simple-chat letter hook:  %s\n" % type(exc).__name__)
            return 0
    if argv[0] == "registry":
        return registry_main(argv[1:], env=env, stdout=stdout)
    if argv[0] == "registry-module":
        # The registry's own CLI, for a platform that does not run Claude.  The
        # `registry` above is the Claude hook's shape;  the module's CLI is the
        # canonical interface, with the per-op arguments every platform's helper
        # speaks, and it is the one a remote host runs for another platform.
        from contextlib import redirect_stdout

        from . import operator_registry as OR

        db = _registry_module_db(argv[1:], env)
        if not os.path.isfile(db):
            # Never create one:  a second registry would hand out letters that collide
            # with the ones the Operator keeps.
            stdout.write(json.dumps({"error": "no Operator registry on this host"}) + "\n")
            return 3
        try:
            # The module's CLI writes its answer to stdout itself, so it is redirected
            # into the stream the caller gave:  an answer goes where the caller reads
            # it, not where the process happens to write.  The env goes with it, so the
            # CLI opens the file this guard just checked.
            with redirect_stdout(stdout):
                return OR.main(argv[1:], env=env)
        except SystemExit as exc:
            # argparse exits on a usage error.  A far host that asked wrongly gets a
            # readable answer, never an empty line and a traceback:  the caller reads
            # the answer the same way it reads every other one.
            code = exc.code if isinstance(exc.code, int) else 2
            if code == 0:
                return 0
            stdout.write(
                json.dumps({"error": "the registry refused the request", "code": "usage"}) + "\n"
            )
            return code
    if argv[0] == "show":
        return show(argv[1:], env, stdout)
    if argv[0] == "install":
        return install(argv[1:], env, stdout)
    stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
