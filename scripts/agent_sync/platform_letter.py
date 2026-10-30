"""`platform-letter`: an Operator session letter, and the title "<letter>: <Title>", for every interactive chat on the
platforms other than Claude and MiniMax (owner request, Oct 29, 2026).  Claude has `claude_letter` and MiniMax has
`mm_session_letter`; this module is the shared library every other platform's adapter is a thin file on.

    platform-letter hook --platform P       the hook a platform runs: start, prompt, rename and end events
    platform-letter registry OP --platform P ...   the registry side: register, title, touch, close or show one chat
    platform-letter show --platform P       this chat's letter from its local note; no network
    platform-letter install --platform P    add the platform's hooks (--check, --remove)

An adapter is one file, `agent_sync/platform_letter_<platform>.py`, with a `SPEC` (a PlatformSpec: what differs per
platform) and, to be installable, an `INSTALL` (a JsonHookInstaller) or its own `install(options, env, stdout)`.  The
library does everything else: the in-scope rule, the registry transport, the note, the title, retry, touch and close.

Where the letter comes from.  The Operator's session registry (`operator_registry`, AFC-412) is one SQLite file on the
Mac, the one allocator for every host and every platform.  A chat on the Mac reaches it in process.  A chat on the
server reaches it over one SSH hop to the Mac (the destination in SESSION_LETTER_REMOTE, or the hook's --remote), by
running `~/.local/bin/platform-letter registry ...` there, so the Mac's own managed runtime makes the write.  The
registry side refuses a host that has no registry file and never creates one: a second file would issue letters that
collide with the Mac's.  A platform keeps its own namespace (`codex`, `opencode`, `muse`, `grok`, `antigravity`): a
letter is unique inside it, and `claude` and `minimax` are reserved to their own hooks.

The letters.  Every chat takes a pair (AA to ZZ, 676 per platform).  A main chat takes the doubled pairs in order
(AA, BB ... ZZ), oldest first; a sub-thread, a chat another chat started, takes the first free pair after its
parent's (parent CC, child CD, the next CE).  The allocation is the registry's own rule (`letter_space="double"`,
`letter_pick="primary"`, `parent_session_id`); this module only asks, through ONE function (`operator_register`)
that reads the registry's signature, so a registry without a parameter still answers, with the lowest free pair.  The
exception is a closed chat whose pair went to another chat: a registry that predates pair letters would give it a
single letter, so that reopen is refused (see `operator_reopen`).

What the hook does.
- A start event registers the chat, or resumes its registration, and titles it: "<letter>: <Title>" when the chat
  already has a title (or a stored one the adapter finds), else "<letter>: New Chat" until its first prompt.  A
  `compact` start only reminds the model of the letter.
- A prompt event titles a chat that is still on the placeholder from its first prompt, retries a registration that
  failed (at most every ten minutes), puts the letter back on a chat whose name dropped it and sends the new words to
  the Operator, and marks the chat seen at most every half hour.  Steady-state prompts cost no hop.
- A rename event does the rename part alone, for platforms that tell the hook when a title changed.
- An end event closes the registration when the chat is over for good (the spec's `close_reasons`, `None` for every
  end), which frees its letter.

The title reaches the chat the way the platform allows: in the hook's own output (`hook` style, a `sessionTitle`
field), through the adapter's `title_writer` (a callable that renames the chat natively), or from a plugin that reads
the `json` output.  A writer that fails leaves the title pending and is tried again on the next prompt.

The hook never blocks a chat: every failure is a line of context, or nothing, and exit 0.  Only interactive chats
take part: a launcher-run session never does (`identity.launched`, unless the spec names that launcher), nor a
headless or exec run, nor a sub-agent run unless the platform records the chat that started it.  SESSION_LETTER=0 (or
`<PLATFORM>_LETTER=0`) turns it off.  The location is the fleet location, never a guess: darwin is the Mac, and any
other host must say `--location server` (or SESSION_LETTER_LOCATION, or CLAUDE_LETTER_LOCATION), so a cloud or
unknown host takes no letter.  Titles go through the fleet's secret scanner before they are stored or relayed.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

from . import identity as I
from . import live as L
from . import secretscan
from . import session_ids as SI

ENTRY = "platform-letter"
SCOPE = "default"
LOCATIONS = ("mac", "server")
# The two platforms that have their own hooks, sweeps and registry sides: this module never writes their rows.
RESERVED_PLATFORMS = ("claude", "minimax")
PLATFORM_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
LABEL_RE = re.compile(r"[A-Z]{1,2}")
DEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*")

ENV_OFF = "SESSION_LETTER"
ENV_LOCATION = "SESSION_LETTER_LOCATION"
ENV_LOCATION_FALLBACK = "CLAUDE_LETTER_LOCATION"
# The ssh destination (a name from the host's own ssh config) that holds the registry, for a chat that is not on the
# registry's host.  It is configuration, not source: the installer writes it into the hook's own command (--remote).
ENV_REMOTE = "SESSION_LETTER_REMOTE"
ENV_REMOTE_FALLBACK = "CLAUDE_LETTER_REMOTE"
ENV_CLOSE_ON = "SESSION_LETTER_CLOSE_ON"
REMOTE_COMMAND = '"$HOME"/.local/bin/' + ENTRY + " registry"
CONNECT_TIMEOUT = (
    4  # seconds for ssh to connect; an asleep or unreachable Mac costs this much, not more
)
REMOTE_TIMEOUT = 8.0  # the whole ssh round trip, inside every hook's own timeout (15 to 20 seconds)

STALE_SECONDS = 36 * 3600
TOUCH_SECONDS = 1800
RETRY_SECONDS = 600
PLACEHOLDER = "New Chat"
TITLE_WORDS = 7
TITLE_CHARS = 60
# The most prompt text a title is looked for in, and the most words scanned for secrets: a pasted wall of text
# costs a bounded amount of work inside the hook's budget.
PROMPT_SCAN_CHARS = 4096
PROMPT_SCAN_WORDS = 64
# The scanner's context rules ("api_key: <value>", "authorization: bearer <value>", "Linear client secret = <value>")
# span several words, so a prompt word is scanned together with the words kept before it, this many at most.
SECRET_WINDOW = 12
STDIN_LIMIT = 1 << 20
# What the registry side answers for a chat it has no registration for (the registry was restored from an older copy,
# or never saw the chat), and what the hook reads as "register again".
NO_REGISTRATION = "this chat has no registration"

#: The events the library knows, whatever a platform calls them.
EVENTS = ("start", "prompt", "rename", "end")
#: The event names Claude Code and Codex style hooks use, and the default for a spec.
DEFAULT_EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "SessionEnd": "end"}
#: Timeouts in seconds for those hook entries.  The ssh hop takes up to REMOTE_TIMEOUT, and a SessionEnd budget is
#: often 1.5 seconds, too short for it, so every entry names its own.
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15, "SessionEnd": 15}
#: The `source` values of a start event after which a hook-output title is applied.
TITLE_SOURCES = ("startup", "resume", "fork")
#: The `source` values of a start event for a chat made from another chat: its title is the other chat's, letter and
#: all, so the other chat's letter is not this chat's and comes off.
CARRIED_SOURCES = ("fork", "branch", "copy")
#: What a chat is, for the in-scope rule.
KINDS = ("chat", "headless", "subagent")
OUTPUT_STYLES = ("hook", "text", "json")
_HOOK_NAMES = {"start": "SessionStart", "prompt": "UserPromptSubmit", "rename": "SessionRename"}

LETTER_PREFIX = re.compile(r"^[A-Z]{1,2}: ")
# A word starts with a letter or digit of any script (accents, Cyrillic, CJK) and goes on with letters, digits and the
# marks a name can carry ( ' ’ + # . - ), so "configuración" and "请修复登录页面" are words and an underscore is a break.
WORD = re.compile(r"[^\W_](?:[^\W_]|['’+#.\-])*")
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
Accessor = Callable[[Mapping[str, Any], Mapping[str, str]], str]


# --------------------------------------------------------------------------------------------
# What differs per platform
# --------------------------------------------------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def tidy(text: object) -> str:
    """`one_line` without control characters or lone surrogates (a title cut mid-emoji at a UTF-16 boundary): neither
    survives an ssh command line, and a NUL is not a character a title can hold."""
    return one_line(
        "".join(ch for ch in one_line(text) if unicodedata.category(ch) not in ("Cc", "Cs"))
    )


def from_payload(*keys: str) -> Accessor:
    """An accessor that reads the first of `keys` holding a string in the hook payload (or plugin event)."""

    def read(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
        for key in keys:
            value = _text(payload.get(key))
            if value:
                return value
        return ""

    return read


def from_env(*names: str) -> Accessor:
    """An accessor that reads the first of the environment variables `names` that is set."""

    def read(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
        for name in names:
            value = _text(env.get(name))
            if value:
                return value
        return ""

    return read


def either(*readers: Accessor) -> Accessor:
    """An accessor that tries each reader in turn, and answers with the first non-empty value."""

    def read(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
        for reader in readers:
            value = reader(payload, env)
            if value:
                return value
        return ""

    return read


def no_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return ""


def always_chat(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return "chat"


def no_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return ""


def payload_title(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """The chat's title as the platform shows it now, when the hook payload carries one."""
    return one_line(payload.get("session_title") or payload.get("title"))


@dataclass(frozen=True)
class TitleRequest:
    """One title for an adapter's `title_writer` to put on the chat natively.  `title` is the whole "XX: Words",
    `words` the part after the letter, `current` what the platform shows now (may be empty), and `reason` one of
    start, prompt, rename, retry or reissue.  The writer returns True when the chat now carries the title."""

    platform: str
    session_id: str
    label: str
    title: str
    words: str
    current: str
    reason: str
    data: Mapping[str, Any]
    env: Mapping[str, str]


TitleWriter = Callable[[TitleRequest], bool]


@dataclass(frozen=True)
class PlatformSpec:
    """Everything that differs per platform.  The accessors take (payload, env): the hook payload, or the plugin's
    event as a dict, and the process environment.

    platform / seat / source   the registry namespace, the seat tag (or an accessor that reads it, for a launcher
                               whose bots each have a seat) and the source name of its registrations
    display                    how context lines name the platform (the address is "@**Codex** CC")
    session_id / parent_id     the native session id and, for a sub-thread, its parent chat's native id
    kind                       "chat", "headless" (an exec or ephemeral run) or "subagent": the in-scope rule
    current_title              the title as the platform shows it now, "" when it does not say
    stored_title               the title of an existing chat from the platform's own store, "" when unknown
    events                     the platform's event names, mapped to start, prompt, rename and end
    close_reasons              the end reasons that close a registration; None closes on every end event
    title_writer / title_in_output   how a title reaches the chat (see the module docstring)
    output                     "hook" (Claude style hookSpecificOutput), "text" (plain stdout) or "json"
    launchers                  the launchers whose sessions DO take a letter under this spec (BotFleet's own spec
                               names "botfleet"); a session any other launcher started never does
    title_sources              the start sources after which a hook-output title is applied
    locations                  the fleet locations the platform runs on (Grok and Antigravity: only "mac"); a hook
                               on any other takes no letter
    env_off                    the variable that turns the hook off for this platform (default <PLATFORM>_LETTER)
    """

    platform: str
    seat: str | Accessor
    source: str
    display: str
    session_id: Accessor = field(default=from_payload("session_id"))
    parent_id: Accessor = field(default=no_parent)
    kind: Accessor = field(default=always_chat)
    current_title: Callable[..., str] = field(default=payload_title)
    stored_title: Accessor = field(default=no_stored_title)
    events: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_EVENTS))
    close_reasons: tuple[str, ...] | None = None
    title_writer: TitleWriter | None = None
    title_in_output: bool = False
    output: str = "hook"
    launchers: tuple[str, ...] = ()
    title_sources: tuple[str, ...] = TITLE_SOURCES
    locations: tuple[str, ...] = LOCATIONS
    env_off: str = ""

    def __post_init__(self) -> None:
        if not PLATFORM_RE.fullmatch(self.platform) or self.platform in RESERVED_PLATFORMS:
            raise ValueError("%r is not a platform namespace this module may write" % self.platform)
        if isinstance(self.seat, str):
            if not I.valid_seat(self.seat) or I.canonical_seat(self.seat) != self.seat:
                raise ValueError("%r is not a canonical seat tag" % self.seat)
        elif not callable(self.seat):
            raise ValueError("a seat is a tag or an accessor")
        if not self.source.strip() or not self.display.strip():
            raise ValueError("a spec needs a source and a display name")
        if self.output not in OUTPUT_STYLES:
            raise ValueError("output must be one of %s" % ", ".join(OUTPUT_STYLES))
        if any(v not in EVENTS for v in self.events.values()):
            raise ValueError("events must map to %s" % ", ".join(EVENTS))
        if not self.locations or any(v not in LOCATIONS for v in self.locations):
            raise ValueError("locations must be some of %s" % ", ".join(LOCATIONS))

    @property
    def off_variable(self) -> str:
        return self.env_off or re.sub(r"[^A-Z0-9]", "_", self.platform.upper()) + "_LETTER"

    def seat_for(self, payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
        """The canonical seat tag this chat registers under, or "" when there is none.  Most platforms are one seat;
        a launcher's spec (BotFleet) reads the bot's seat from the environment (`from_env("AGENT_LAUNCH_SEAT")`)."""
        raw = self.seat if isinstance(self.seat, str) else self.seat(payload, env)
        seat = I.canonical_seat(raw)
        return seat if I.valid_seat(seat) else ""


#: Adapters that registered themselves in process (a test, or a platform with no module of its own).
SPECS: dict[str, PlatformSpec] = {}


def register_spec(spec: PlatformSpec) -> PlatformSpec:
    SPECS[spec.platform] = spec
    return spec


def load_adapter(platform: str) -> Any:
    """The adapter module `agent_sync/platform_letter_<platform>.py`, or None.  The name is checked first, so an
    argument can never name any other module."""
    if not PLATFORM_RE.fullmatch(platform or "") or platform in RESERVED_PLATFORMS:
        return None
    import importlib

    name = "%s.platform_letter_%s" % (__package__ or "agent_sync", platform.replace("-", "_"))
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def load_spec(platform: str) -> PlatformSpec | None:
    """The spec for `platform`: one registered in process, else its adapter module's SPEC."""
    if platform in SPECS:
        return SPECS[platform]
    module = load_adapter(platform)
    spec = getattr(module, "SPEC", None)
    return spec if isinstance(spec, PlatformSpec) and spec.platform == platform else None


# --------------------------------------------------------------------------------------------
# Whether, and where
# --------------------------------------------------------------------------------------------


def in_scope(spec: PlatformSpec, env: Mapping[str, str], data: Mapping[str, Any]) -> bool:
    """Only interactive chats take a letter.  A session a launcher started never does (BotFleet, Grok Bot and the
    wake runs have their own handles), unless the spec names that launcher; a headless run never does; a sub-agent
    run does only when the platform records the chat that started it (it is then a sub-thread).  SESSION_LETTER=0
    and `<PLATFORM>_LETTER=0` turn the hook off."""
    for name in (ENV_OFF, spec.off_variable):
        if _text(env.get(name)) == "0":
            return False
    if I.launched(env) and I.launcher_of(env) not in spec.launchers:
        return False
    kind = _text(spec.kind(data, env))
    if kind == "chat":
        return True
    return kind == "subagent" and bool(spec.parent_id(data, env))


def location(env: Mapping[str, str], platform: str = sys.platform, flag: str = "") -> str | None:
    """The fleet location of this chat, or None when it cannot be stated: darwin is the Mac, and any other host
    must say so, with --location, SESSION_LETTER_LOCATION or CLAUDE_LETTER_LOCATION.  A value that is not a fleet
    location is refused rather than passed on."""
    forced = (
        _text(flag) or _text(env.get(ENV_LOCATION)) or _text(env.get(ENV_LOCATION_FALLBACK))
    ).lower()
    if forced:
        return forced if forced in LOCATIONS else None
    return "mac" if platform == "darwin" else None


def remote_destination(env: Mapping[str, str], flag: str = "") -> str:
    return _text(flag) or _text(env.get(ENV_REMOTE)) or _text(env.get(ENV_REMOTE_FALLBACK))


def note_path(env: Mapping[str, str], who: str, sid: str) -> str:
    """Where a chat's note lives: under the state root, in a folder named by a plain seat token only, so a seat
    value with a separator can never place the note anywhere else."""
    safe = I.valid_seat(who) or "UNKNOWN"
    return os.path.join(L.state_root(env), safe, "letters", sid + ".json")


# --------------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------------


def clean_title(text: object) -> str:
    """One line, at most TITLE_CHARS characters, cut at a word boundary, with any secret-shaped word dropped.

    A title is relayed to the registry and from there to the Fleet Reference sheet, and a first prompt can carry a
    pasted key, so every word goes through the fleet's secret scanner first; it reports only that a word matched.
    Control characters and lone surrogates (a title cut mid-emoji) are dropped too: neither survives an ssh command
    line or a JSON note."""
    words = [w for w in tidy(text).split(" ") if w and not secretscan.scan(w)]
    line = " ".join(words)
    if secretscan.scan(line):
        return ""
    if len(line) <= TITLE_CHARS:
        return line
    cut = line[:TITLE_CHARS].rsplit(" ", 1)[0]
    return (cut or line[:TITLE_CHARS]).rstrip(" ,;:-")


def bare(title: object, labels: tuple[str, ...] | list[str] = ()) -> str:
    """A title without its leading letter, so "BC: Fix Login" and "Fix Login" carry the same words.

    With the chat's own letters (the one it holds and the ones it held) only those are dropped, so a title that opens
    with an acronym ("PR: Review") keeps it; without any, one or two capital letters before a colon are taken for a
    letter."""
    line = one_line(title)
    held = [label for label in labels if label]
    if held:
        for label in held:
            if line == label + ":":
                return ""  # the name was cleared down to the letter alone
            if line.startswith(label + ": "):
                return line[len(label) + 2 :]
        return line
    if re.fullmatch(r"[A-Z]{1,2}:", line):
        return ""
    return LETTER_PREFIX.sub("", line, count=1) if LETTER_PREFIX.match(line) else line


def compose(label: str, words: object) -> str:
    """The title a chat carries: its letter, a colon, a space, and its words (the placeholder while it has none)."""
    return "%s: %s" % (label, one_line(words) or PLACEHOLDER)


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


def title_from_prompt(prompt: object) -> str:
    """A short title from a prompt's first line that has words, or "" when it has none.  Markup, code blocks, links
    and a leading slash command are dropped first, so a pasted block or "/review" does not become the title (the
    words after a slash command still can)."""
    text = str(prompt or "")[:PROMPT_SCAN_CHARS]
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^<>]{1,200}>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    for line in text.splitlines():
        line = re.sub(r"^\s*/\S*", " ", line)
        words: list[str] = []
        kept: list[str] = []  # the line's tokens that are not secrets, in order
        # Each whitespace-delimited token is scanned whole, then split into words: a token such as ghp_<key> must
        # not reach the scanner as the two harmless halves an underscore would cut it into.  It is scanned as the
        # last of the tokens kept before it, because the scanner's context rules need the keyword and the value
        # together ("api_key: <value>" is a secret only with "api_key:"); the value is dropped, never the line.
        for scanned, token in enumerate(line.split()):
            if len(words) >= TITLE_WORDS or scanned >= PROMPT_SCAN_WORDS:
                break
            if secretscan.scan(" ".join([*kept[-SECRET_WINDOW:], token])):
                continue
            kept.append(token)
            words.extend(m.group() for m in WORD.finditer(token))
        del words[TITLE_WORDS:]
        while len(words) > 1 and words[-1].rstrip(".-").lower() in SMALL_WORDS:
            words.pop()  # "... Addressed From" reads cut off; end on a word that carries meaning
        if words:
            return clean_title(title_case(words))
    return ""


# --------------------------------------------------------------------------------------------
# The registry side (runs where the registry file is: the Mac)
# --------------------------------------------------------------------------------------------
#
# Everything this module asks of `operator_registry` goes through operator_register, operator_reopen and
# resolve_pick below.  They read the registry's signature, so a registry that predates a parameter still answers
# (pair letters in the lowest-free order, a sub-thread picked like a main chat), and a renamed parameter is a change
# in one place.


def valid_platform(platform: object) -> bool:
    return (
        isinstance(platform, str)
        and bool(PLATFORM_RE.fullmatch(platform))
        and platform not in RESERVED_PLATFORMS
    )


def resolve_pick(OR: Any, pick: str) -> str:
    """The letter pick to ask for: "auto" is the main-chat order (`primary`) when the registry has it, else the
    lowest free pair."""
    if pick != "auto":
        return pick
    return "primary" if "primary" in tuple(getattr(OR, "LETTER_PICKS", ())) else "lowest"


def _accepts(fn: Any, name: str) -> bool:
    import inspect

    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


#: The names the registry has used for "the chat that started this one", in the order they are tried.
PARENT_PARAMS = ("parent_session_id", "parent")


def operator_register(
    OR: Any,
    db: str,
    *,
    platform: str,
    location: str,
    sid: str,
    seat: str,
    source: str,
    title: str,
    space: str,
    pick: str,
    parent: str = "",
) -> dict[str, Any]:
    """Register one chat with the Operator's registry (the contract this module codes against)."""
    kwargs: dict[str, Any] = dict(
        platform=platform,
        verified_host=location,
        profile_scope=SCOPE,
        native_session_id=sid,
        seat=seat,
        source=source,
        title=title,
    )
    # Every optional argument is probed, so a registry that predates one of them still registers the chat with the
    # lowest free pair instead of raising TypeError.
    if _accepts(OR.register_session, "issuer"):
        kwargs["issuer"] = OR.ISSUER_OPERATOR
    if _accepts(OR.register_session, "letter_space"):
        kwargs["letter_space"] = space
    if _accepts(OR.register_session, "letter_pick"):
        kwargs["letter_pick"] = pick
    for name in PARENT_PARAMS:
        if parent and _accepts(OR.register_session, name):
            kwargs[name] = parent
            break
    return OR.register_session(db, **kwargs)


class SpaceLost(RuntimeError):
    """A reopen would give a chat that held a pair a single letter: the registry's `reopen` does not keep the space."""


def _last_letter(db: str, record_id: str) -> tuple[str, bool]:
    """(the letter a chat held last, whether another live chat of the platform holds it now), read-only like
    `_open_rows`: mode=rw so a hot journal can be recovered, and never a create."""
    import sqlite3
    from pathlib import Path

    conn = sqlite3.connect("%s?mode=rw" % Path(os.path.abspath(db)).as_uri(), uri=True)
    try:
        row = conn.execute(
            "SELECT letter, platform FROM alias_assignments WHERE record_id = ? ORDER BY assigned_at DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if row is None:
            return "", False
        taken = conn.execute(
            "SELECT 1 FROM alias_assignments WHERE platform = ? AND letter = ? AND retired_at IS NULL",
            (row[1], row[0]),
        ).fetchone()
        return row[0], taken is not None
    finally:
        conn.close()


def operator_reopen(
    OR: Any,
    db: str,
    reg: Any,
    record_id: str,
    *,
    actor: str,
    source: str,
    pick: str,
    parent: str = "",
) -> tuple[str, int, str]:
    """Give a closed chat a letter again (`SessionRegistry.reopen`), with the pick and parent when it takes them.

    A chat that held a pair comes back with a pair.  A `reopen` that predates pair letters (it takes no `pick`) gives
    the chat its old pair back when that is free, which is right, and allocates from the single letters when it is
    not, which would title the chat "A: ..." on a platform where every other chat has a pair.  That second case is
    refused before anything is written (SpaceLost): the chat waits for a letter until the Mac's registry is the
    pair-aware one, and nothing is left behind in its history to answer for a letter it never had."""
    kwargs: dict[str, Any] = dict(actor=actor, source=source)
    if _accepts(reg.reopen, "pick"):
        kwargs["pick"] = pick
    else:
        previous, taken = _last_letter(db, record_id)
        if len(previous) == 2 and taken:
            raise SpaceLost(
                "the pair %s went to another chat, and this registry reopens into single letters"
                % previous
            )
    for name in PARENT_PARAMS:
        if parent and _accepts(reg.reopen, name):
            kwargs[name] = parent
            break
    return reg.reopen(record_id, **kwargs)


def _iso(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _open_rows(OR: Any, db: str, platform: str, source: str) -> list[Any]:
    """Every open registration of this platform that this adapter's hook made, with its live letter, read with the
    registry's own read-only rule: mode=rw, so a hot journal can be recovered, and never a create.

    Only this hook's `source`: the hook keeps those rows' last_seen_at fresh, so their age means idleness.  A chat the
    Operator registered another way is never marked seen by this hook, so its age says nothing, and it is never swept
    or evicted here."""
    import sqlite3
    from pathlib import Path

    conn = sqlite3.connect("%s?mode=rw" % Path(os.path.abspath(db)).as_uri(), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT s.record_id, s.verified_host, s.native_session_id, s.last_seen_at, a.letter"
            " FROM sessions s LEFT JOIN alias_assignments a"
            " ON a.record_id = s.record_id AND a.retired_at IS NULL"
            " WHERE s.platform = ? AND s.closed_at IS NULL AND s.source = ?",
            (OR.normalize_platform(platform), source),
        ).fetchall()
    finally:
        conn.close()


def _close_records(OR: Any, db: str, records: list[str], source: str, reason: str) -> int:
    """Close each record on its own, so one that cannot be closed never keeps the rest open; a failure is named on
    stderr by record id and exception type only."""
    if not records:
        return 0
    import sqlite3

    reg = OR.SessionRegistry(db)
    closed = 0
    try:
        for rid in records:
            try:
                closed += bool(reg.close(rid, actor="OPERATOR", source=source, reason=reason))
            except (sqlite3.Error, OR.UnknownRecord) as exc:
                sys.stderr.write(
                    "platform-letter: could not close %s (%s)\n" % (rid, type(exc).__name__)
                )
    finally:
        reg.close_db()
    return closed


def sweep_stale(
    OR: Any, db: str, platform: str, source: str, keep: tuple[str, str], now: float
) -> int:
    """Close this adapter's registrations not seen for STALE_SECONDS, except the chat being registered."""
    stale = []
    for row in _open_rows(OR, db, platform, source):
        if (row["verified_host"], row["native_session_id"]) == keep:
            continue
        seen = _iso(row["last_seen_at"])
        if seen is not None and now - seen >= STALE_SECONDS:
            stale.append(row["record_id"])
    return _close_records(OR, db, stale, "platform-letter-sweep", "not seen for 36 hours")


def evict_oldest(
    OR: Any, db: str, platform: str, source: str, keep: tuple[str, str], space: str
) -> int:
    """Close the least recently seen open registration of the space that is full, except the chat being registered."""
    size = 2 if space == "double" else 1
    rows = [
        r
        for r in _open_rows(OR, db, platform, source)
        if (r["verified_host"], r["native_session_id"]) != keep and len(r["letter"] or "") == size
    ]
    if not rows:
        return 0
    oldest = min(rows, key=lambda r: _iso(r["last_seen_at"]) or 0.0)
    return _close_records(
        OR, db, [oldest["record_id"]], "platform-letter-evict", "every letter live"
    )


def _with_room(
    OR: Any, db: str, args: Any, keep: tuple[str, str], allocate: Callable[[], Any]
) -> tuple[Any, int]:
    """Run one allocation; when every letter of the space is live, close the least recently seen chat of this
    adapter and try once more.  A second LabelExhausted is the caller's to report."""
    try:
        return allocate(), 0
    except OR.LabelExhausted as exc:
        space = getattr(exc, "space", args.letter_space)
        if not evict_oldest(OR, db, args.platform, args.source, keep, space):
            raise
        return allocate(), 1


def _register(OR: Any, db: str, args: Any, sid: str, now: float) -> dict[str, Any]:
    """Register the chat, resume it, or reopen it.  A resume of a closed registration is the Operator's cue to issue a
    letter again, so it never comes back empty-handed while a letter can be freed."""
    keep = (args.location, sid)
    swept = sweep_stale(OR, db, args.platform, args.source, keep, now)
    title = clean_title(args.title)
    pick = resolve_pick(OR, args.letter_pick)
    parent = ""
    if args.parent_session_id:
        try:
            parent = SI.check(args.parent_session_id, "the parent session id")
        except ValueError:
            parent = ""  # not a session id, so no parent: the chat is picked like a main chat

    def register() -> dict[str, Any]:
        return operator_register(
            OR,
            db,
            platform=args.platform,
            location=args.location,
            sid=sid,
            seat=args.seat,
            source=args.source,
            title=title,
            space=args.letter_space,
            pick=pick,
            parent=parent,
        )

    try:
        out, evicted = _with_room(OR, db, args, keep, register)
        if out.get("outcome") in ("resumed", "resumed_closed"):
            reg = OR.SessionRegistry(db)
            try:
                if out["outcome"] == "resumed_closed":
                    (letter, generation, previous), more = _with_room(
                        OR,
                        db,
                        args,
                        keep,
                        lambda: operator_reopen(
                            OR,
                            db,
                            reg,
                            out["record_id"],
                            actor=args.seat,
                            source=args.source,
                            pick=pick,
                            parent=parent,
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
    except OR.LabelExhausted:
        return {"error": "all %s letters are in use" % args.letter_space, "swept": swept}
    except SpaceLost:
        return {
            "error": "this registry cannot give a closed chat a pair letter again yet",
            "swept": swept,
        }
    out["swept"] = swept
    out["evicted"] = evicted
    return out


def _on_record(OR: Any, db: str, args: Any, sid: str) -> dict[str, Any]:
    reg = OR.SessionRegistry(db, read_only=args.op == "show")
    try:
        rec = reg.lookup(args.platform, args.location, SCOPE, sid)
        if rec is None:
            return {"error": NO_REGISTRATION}
        if args.op == "show":
            return dict(rec, found=True)
        if rec.get("state") == "closed" or not rec.get("label"):
            return {"retired": True, "record_id": rec["record_id"]}
        rid = rec["record_id"]
        if args.op == "title":
            if args.title:
                reg.update_attrs(rid, title=clean_title(args.title))
            reg.mark_seen(rid)
        elif args.op == "touch":
            reg.mark_seen(rid)
        elif args.op == "close":
            return {
                "closed": reg.close(
                    rid, actor=args.seat, source=args.source, reason="session ended"
                ),
                "label": rec["label"],
            }
        return {"ok": True, "label": rec["label"], "generation": rec["generation"]}
    finally:
        reg.close_db()


def _make_parser() -> Any:
    import argparse

    class Refused(ValueError):
        pass

    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> Any:
            raise Refused(message)

        def exit(self, status: int = 0, message: str | None = None) -> Any:
            raise Refused(message or "exit")

    parser = Parser(prog=ENTRY + " registry", add_help=False, allow_abbrev=False)
    parser.refused = Refused  # type: ignore[attr-defined]
    parser.add_argument("op", choices=("register", "title", "touch", "close", "show"))
    parser.add_argument("--platform", required=True)
    parser.add_argument("--location", required=True, choices=LOCATIONS)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--seat", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--parent-session-id", default="")
    parser.add_argument("--letter-space", default="double", choices=("single", "double"))
    parser.add_argument(
        "--letter-pick", default="auto", choices=("auto", "lowest", "oldest", "primary")
    )
    parser.add_argument("--db", default="", help="the registry file (default: the Operator's)")
    return parser


def registry_main(
    argv: list[str], *, env: Mapping[str, str], stdout: TextIO, now: float | None = None
) -> int:
    """`registry OP --platform P --location L --session-id S [--seat SEAT] [--title T] [--source SRC]
    [--parent-session-id PID] [--letter-space single|double] [--letter-pick PICK] [--db FILE]`, one JSON line out.

    The exit status is 0 for an answer, 2 for bad arguments, 3 when this host has no registry, 4 for a refused
    request and 5 for an answer that carries an error."""
    import sqlite3

    from . import operator_registry as OR

    parser = _make_parser()
    try:
        args = parser.parse_args(argv)
    except parser.refused:
        return _emit(stdout, {"error": "bad arguments"}, 2)
    if not valid_platform(args.platform):
        return _emit(stdout, {"error": "%r is not a platform this side writes" % args.platform}, 2)
    args.platform = OR.normalize_platform(args.platform)
    args.seat = I.canonical_seat(args.seat)
    if args.op == "register" and not I.valid_seat(args.seat):
        return _emit(stdout, {"error": "a registration needs a seat"}, 2)
    args.seat = args.seat or "OPERATOR"
    args.source = args.source.strip() or "platform-letter:" + args.platform
    db = args.db or OR.registry_db_path(L.state_root(env), env)
    if not os.path.isfile(db):
        # Never create one: a second registry would hand out letters that collide with the one the Operator keeps.
        return _emit(stdout, {"error": "no Operator registry on this host"}, 3)
    try:
        sid = SI.check(args.session_id, "the session id")
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
    """A registry answer whose fields have the shapes the hook reads, or an error that says it did not: every named
    field has its type, a label is "" or one or two letters A-Z, and a generation is a positive whole number."""
    if value is None:
        return {"error": "the registry gave no answer"}
    for key, kind in _ANSWER_TYPES.items():
        if key in value and not isinstance(value[key], kind):
            return {"error": "the registry answer has a malformed %s" % key}
    label = value.get("label")
    if label and not LABEL_RE.fullmatch(label):
        return {"error": "the registry answer has a malformed label"}
    gen = value.get("generation")
    if gen is not None and (isinstance(gen, bool) or not isinstance(gen, int) or gen < 1):
        return {"error": "the registry answer has a malformed generation"}
    return value


Transport = Callable[[list[str]], Mapping[str, Any]]


class Chat:
    """One chat, as the hook sees it: its spec, location and session id, its note, and the way to its registry.

    `transport` replaces the registry round trip with a callable that takes the registry side's argv and answers with
    its JSON (a test's fake registry); without one the Mac runs the registry side in process and the server runs it
    over ssh."""

    def __init__(
        self,
        spec: PlatformSpec,
        env: Mapping[str, str],
        where: str,
        sid: str,
        *,
        runner: Runner,
        now: float,
        remote: str = "",
        transport: Transport | None = None,
        seat: str = "",
    ):
        self.spec, self.env, self.location, self.session_id = spec, env, where, sid
        self.runner, self.now, self.remote, self.transport = runner, now, remote, transport
        self.seat = seat or spec.seat_for({}, env)
        self.path = note_path(env, self.seat, sid)

    # ---- the local note --------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, note: Mapping[str, Any]) -> bool:
        """Save the note; False when it cannot be saved (a state folder that is not writable).  The hook goes on
        without it, so the letter is still shown, and the next event only resumes the registration."""
        tmp = ""
        try:
            folder = os.path.dirname(self.path)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            # An unpredictable name made with O_EXCL:  a symlink planted at a guessable temporary name can never
            # redirect the write to another file.
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".letter-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    dict(
                        note,
                        session_id=self.session_id,
                        platform=self.spec.platform,
                        updated_at=self.now,
                    ),
                    fh,
                    sort_keys=True,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
        return True

    # ---- the registry ----------------------------------------------------------------------

    def argv(self, op: str, title: str = "", parent: str = "") -> list[str]:
        """The registry side's command line.  Values that carry words go in the `--name=value` form, so a title that
        starts with a dash is still the title and never an option."""
        argv = [
            op,
            "--platform",
            self.spec.platform,
            "--location",
            self.location,
            "--session-id",
            self.session_id,
            "--seat",
            self.seat,
            "--source",
            self.spec.source,
        ]
        words = clean_title(title)
        if words:
            argv.append("--title=" + words)
        if parent and op == "register":
            try:
                argv.append("--parent-session-id=" + SI.check(parent, "the parent session id"))
            except ValueError:
                pass  # not a session id, so no parent: the chat is picked like a main chat
        return argv

    def registry(self, op: str, title: str = "", parent: str = "") -> dict[str, Any]:
        """One call to the registry side, answered as its JSON.  Whatever goes wrong, on the way or in the registry
        side, is an error answer: a hook never blocks a chat, and never stops on a call that failed."""
        try:
            return self._registry(op, title, parent)
        except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
            return {"error": "the registry call failed (%s)" % type(exc).__name__}

    def _registry(self, op: str, title: str, parent: str) -> dict[str, Any]:
        argv = self.argv(op, title, parent)
        if self.transport is not None:
            return _answer(dict(self.transport(argv)))
        if self.location == "mac":
            out = io.StringIO()
            registry_main(argv, env=self.env, stdout=out)
            return _answer(_last_json(out.getvalue()))
        dest = self.remote
        if not DEST_RE.fullmatch(dest):
            # Empty, or something ssh could read as an option: refuse it rather than run it.
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


class Out:
    """What the hook prints, in the spec's output style.  `hook` is Claude Code's hookSpecificOutput (context and a
    sessionTitle), `text` is the context alone as plain lines, `json` is one object for a plugin to read."""

    def __init__(self, stream: TextIO, spec: PlatformSpec, hook_event: str):
        self.stream, self.spec, self.hook_event = stream, spec, hook_event

    def emit(
        self, event: str, *, context: str = "", title: str | None = None, label: str = ""
    ) -> None:
        style = self.spec.output
        if style == "hook":
            body: dict[str, str] = {
                "hookEventName": self.hook_event or _HOOK_NAMES.get(event, "SessionStart")
            }
            if context:
                body["additionalContext"] = context
            if title and self.spec.title_in_output:
                body["sessionTitle"] = title
            if len(body) > 1:
                self.stream.write(json.dumps({"hookSpecificOutput": body}) + "\n")
        elif style == "text":
            if context:
                self.stream.write(context + "\n")
        else:
            body = {"event": event, "platform": self.spec.platform}
            if label:
                body["label"] = label
            if context:
                body["context"] = context
            if title:
                body["title"] = title
            if len(body) > 2:
                self.stream.write(json.dumps({"platform_letter": body}, sort_keys=True) + "\n")


def reminder(spec: PlatformSpec, note: Mapping[str, Any]) -> str:
    label = note.get("label") or ""
    gen = note.get("generation") or 1
    ref = "%s %s%s" % (spec.display, label, " (gen %d)" % gen if gen > 1 else "")
    # Rendered copy: a sentence gap is U+00A0 plus a space, and a colon or semicolon takes one space.
    return (
        'Operator session letter: %s, on the %s.  This chat is addressed as "@**%s** %s", and its title '
        'starts with "%s: "; keep that prefix if the chat is renamed.'
        % (ref, note.get("location"), spec.display, label, label)
    )


def held(note: Mapping[str, Any], *extra: object) -> tuple[str, ...]:
    """The letters this chat holds or held: the one in its note, the ones it held before, and any `extra`."""
    seen: list[str] = []
    for label in (*extra, note.get("label"), *(note.get("labels") or ())):
        if isinstance(label, str) and LABEL_RE.fullmatch(label) and label not in seen:
            seen.append(label)
    return tuple(seen)


def _apply(chat: Chat, answer: Mapping[str, Any], note: dict[str, Any]) -> dict[str, Any]:
    """Fold one register answer into the note: open, failed, or held by another seat."""
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
        labels=list(held(note, answer["label"], answer.get("previous")))[:8],
    )
    if fresh:
        out["titled"] = False
    out.pop("error", None)
    out.pop("failed_at", None)
    out.pop("sync_failed_at", None)  # the registry just answered
    return out


def _reissued(spec: PlatformSpec, answer: Mapping[str, Any]) -> str:
    """The context line for a reopened chat whose letter changed, or ""."""
    previous, label = str(answer.get("previous") or ""), str(answer.get("label") or "")
    if answer.get("outcome") == "reopened" and previous and previous != label:
        return (
            "This chat's earlier letter %s went to another chat while this one was closed, so the Operator "
            "issued %s.  " % (previous, label)
        )
    return ""


def _write_title(
    chat: Chat,
    note: Mapping[str, Any],
    title: str,
    current: str,
    data: Mapping[str, Any],
    reason: str,
) -> bool:
    """Ask the adapter's writer to put `title` on the chat.  A writer that raises has failed, and a hook never
    blocks a chat, so the exception is swallowed."""
    writer = chat.spec.title_writer
    if writer is None:
        return False
    label = str(note.get("label") or "")
    request = TitleRequest(
        platform=chat.spec.platform,
        session_id=chat.session_id,
        label=label,
        title=title,
        words=bare(title, (label,)),
        current=current,
        reason=reason,
        data=data,
        env=chat.env,
    )
    try:
        return bool(writer(request))
    except Exception:  # noqa: BLE001 - a title writer never blocks a chat
        return False


def present(
    chat: Chat,
    note: dict[str, Any],
    title: str,
    current: str,
    data: Mapping[str, Any],
    reason: str,
    *,
    settable: bool = True,
) -> str | None:
    """Put `title` on the chat: through the adapter's writer when it has one, else in the hook's own output.  Returns
    the title the hook should print, or None.  A writer that fails with no output route leaves the title pending, and
    a later prompt tries again, no sooner than RETRY_SECONDS after the failure."""
    spec = chat.spec
    note["want"] = title
    if title == current:
        note.update(shown=title, pending=False)
        note.pop("retry_at", None)
        return None
    wrote = _write_title(chat, note, title, current, data, reason)
    if wrote:
        note.update(shown=title, pending=False)
        note.pop("retry_at", None)
        return None
    if spec.title_in_output and settable:
        note.update(shown=title, pending=False)
        note.pop("retry_at", None)
        return title
    note["pending"] = spec.title_writer is not None
    if note["pending"]:
        note["retry_at"] = (
            chat.now + RETRY_SECONDS
        )  # a writer that is down is not asked again on every prompt
    return None


def _retry_pending(chat: Chat, note: dict[str, Any], current: str, data: Mapping[str, Any]) -> None:
    want = str(note.get("want") or "")
    if not (note.get("pending") and want and chat.spec.title_writer is not None):
        return
    if chat.now < float(note.get("retry_at") or 0):
        return
    if want == current or _write_title(chat, note, want, current, data, "retry"):
        note.update(shown=want, pending=False)
        note.pop("retry_at", None)
    else:
        note["retry_at"] = chat.now + RETRY_SECONDS


def _gone(answer: Mapping[str, Any]) -> bool:
    """The Operator has no live registration for this chat: its letter was retired, or the registry was restored from
    an older copy.  Either way the hook registers again."""
    return bool(answer.get("retired")) or answer.get("error") == NO_REGISTRATION


def _failed(answer: Mapping[str, Any]) -> bool:
    """The call did not reach a registry that answered (an asleep Mac, a refused request): try it again later."""
    return "error" in answer and not _gone(answer)


def _push(chat: Chat, note: dict[str, Any], op: str, words: str = "") -> dict[str, Any]:
    """Tell the Operator a chat's words (`title`) or that it is still here (`touch`).  An answer that is an error is not
    a delivery: the words stay in the note as `unsent`, and nothing more is sent until RETRY_SECONDS have passed, so a
    Mac that is asleep costs one hop every ten minutes, not one on every prompt.  Returns the registry's answer, or {}
    when the call was held back."""
    if op == "title" and words:
        note["unsent"] = words
    if chat.now - float(note.get("sync_failed_at") or 0) < RETRY_SECONDS:
        return {}
    answer = chat.registry(op, title=words)
    if _failed(answer):
        note["sync_failed_at"] = chat.now
        return answer
    note.pop("sync_failed_at", None)
    if not _gone(answer):
        note["touched_at"] = chat.now
        if op == "title":
            note.pop("unsent", None)
    return answer


def on_start(chat: Chat, data: Mapping[str, Any], out: Out, home: str) -> int:
    spec = chat.spec
    source = _text(data.get("source")) or "startup"
    current = tidy(spec.current_title(data, chat.env))
    note = chat.read()
    if source == "compact":
        if note.get("state") == "open" and note.get("label"):
            out.emit("start", context=reminder(spec, note), label=str(note["label"]))
        return 0
    # /clear starts a new chat that can carry the old chat's name; that name is not this chat's title.
    cleared = source == "clear"
    # A fork, branch or copy carries the other chat's title, and with it the other chat's letter: its words are this
    # chat's to keep, its letter is not.
    carried = source in CARRIED_SOURCES
    named = "" if cleared else bare(current)
    inherited = current if (cleared or carried) else ""
    if named == PLACEHOLDER:
        named = ""  # "<letter>: New Chat" holds the place of a title; it is not one
    loose = named
    if not loose and source in ("resume", "fork"):
        loose = clean_title(spec.stored_title(data, chat.env))
    parent = _text(spec.parent_id(data, chat.env)) or str(note.get("parent") or "")
    answer = chat.registry("register", title=loose, parent=parent)
    start = dict(note, inherited=inherited, title=loose or note.get("title", ""), parent=parent)
    if inherited:
        start["seen"] = inherited  # the name the chat started with is not a rename
    note = _apply(chat, answer, start)
    if note["state"] == "failed":
        chat.write(note)
        out.emit(
            "start",
            context="No Operator letter yet (%s).  "
            "A later prompt tries again, at most every ten minutes." % note["error"],
        )
        return 0
    if note["state"] == "mismatch":
        chat.write(note)
        out.emit(
            "start", context="This chat is registered under another seat, so it takes no letter."
        )
        return 0
    label = note["label"]
    text = loose
    if named:
        # With the letters this chat holds in hand, only those come off its title: an acronym stays.  A carried
        # title is the other chat's, and its letter is not one this chat holds, so the letter-shaped prefix goes.
        text = named if carried else bare(current, held(note, answer.get("previous")))
        if text == PLACEHOLDER:
            text = ""
        note["title"] = text
        if text != loose:
            _push(chat, note, "title", text)
    placeholder = compose(label, "")
    note["titled"] = bool(text)
    note["placeholder"] = placeholder
    title = compose(label, text)
    shown = present(
        chat, note, title, current, data, "start", settable=source in spec.title_sources
    )
    chat.write(note)
    out.emit(
        "start", context=_reissued(spec, answer) + reminder(spec, note), title=shown, label=label
    )
    return 0


def _first_title(note: Mapping[str, Any], current: str, prompt: object) -> str:
    """The words for a chat's first real title: a name someone gave it, else the title its start found, else its
    first prompt, else "" (a prompt with no words, such as a bare slash command, waits for the next one)."""
    placeholder = note.get("placeholder") or ""
    words = bare(current, held(note))
    if (
        current
        and current not in (placeholder, note.get("inherited") or "")
        and words != PLACEHOLDER
    ):
        return words or ""  # someone named the chat: keep their words under the letter
    return str(note.get("title") or "") or title_from_prompt(prompt)


def _renamed(note: Mapping[str, Any], current: str) -> tuple[str, str]:
    """(words, title) for a chat someone renamed since the hook last set its title.  `words` is what the Operator
    does not have yet, or ""; `title` is the whole title to show when the new name dropped the letter, or ""."""
    if not current or current in (note.get("shown"), note.get("seen")) or not note.get("label"):
        return "", ""
    label = str(note["label"])
    words = bare(current, held(note))
    if not words or words == PLACEHOLDER:
        return "", ""
    show = "" if current.startswith(label + ": ") else compose(label, words)
    return (words if words != note.get("title") else ""), show


def on_prompt(chat: Chat, data: Mapping[str, Any], out: Out) -> int:
    spec = chat.spec
    note = chat.read()
    state = note.get("state")
    if state in ("mismatch", "closed"):
        return 0
    current = tidy(spec.current_title(data, chat.env))
    announce = False  # the model has not been told this letter yet
    if state != "open" or not note.get(
        "label"
    ):  # a note that says open with no letter is not one to trust
        if state == "failed" and chat.now - float(note.get("failed_at") or 0) < RETRY_SECONDS:
            return 0
        parent = _text(spec.parent_id(data, chat.env)) or str(note.get("parent") or "")
        note = _apply(
            chat,
            chat.registry("register", title=str(note.get("title") or ""), parent=parent),
            dict(note, parent=parent),
        )
        if note["state"] != "open":
            chat.write(note)
            return 0
        announce = (
            True  # a start that found no Operator now has one: the letter is news to the model
        )
    title = None
    answer: dict[str, Any] = {}
    text = "" if note.get("titled") else _first_title(note, current, data.get("prompt"))
    if text:
        title = compose(note["label"], text)
        note.update(
            titled=True,
            title=text,
            placeholder=note.get("placeholder") or compose(note["label"], ""),
        )
        answer = _push(chat, note, "title", text)
    else:
        words, show = _renamed(note, current)
        if words or show:
            note["seen"] = (
                current  # one try for each name the platform shows, never one for every prompt
            )
            if words:
                note["title"] = words
                answer = _push(chat, note, "title", words)
            title = show or None
        elif note.get("unsent"):
            answer = _push(chat, note, "title", str(note["unsent"]))  # an earlier send failed
        elif chat.now - float(note.get("touched_at") or 0) >= TOUCH_SECONDS:
            answer = _push(chat, note, "touch")
    if _gone(answer):
        # The Operator has no registration for this chat any more (its letter was retired, or the registry was
        # restored from an older copy): check with it again, in case it needs a new one.
        words = str(note.get("title") or "") or bare(current, held(note))
        if words == PLACEHOLDER:
            words = ""
        parent = str(note.get("parent") or "")
        note = _apply(chat, chat.registry("register", title=words, parent=parent), note)
        if note["state"] == "open":
            note.pop("unsent", None)
            note.update(titled=bool(words), title=words, placeholder=compose(note["label"], ""))
            title = compose(note["label"], words)
            announce = True  # the letter may have changed
        else:
            title = bare(current, held(note)) if current != bare(current, held(note)) else None
    shown = None
    if title:
        shown = present(chat, note, title, current, data, "prompt")
    else:
        _retry_pending(chat, note, current, data)
    chat.write(note)
    if announce or (shown and shown != current):
        out.emit(
            "prompt",
            context=reminder(spec, note) if announce else "",
            title=shown,
            label=str(note.get("label") or ""),
        )
    return 0


def on_rename(chat: Chat, data: Mapping[str, Any], out: Out) -> int:
    """A platform that says when a chat was renamed: keep the letter on its title and the Operator's words current."""
    note = chat.read()
    if note.get("state") != "open":
        return 0
    current = tidy(chat.spec.current_title(data, chat.env))
    words, show = _renamed(note, current)
    if not (words or show):
        return 0
    note["seen"] = current
    if words:
        note["title"] = words
        _push(chat, note, "title", words)  # a failed send is kept in the note for a later prompt
    shown = present(chat, note, show, current, data, "rename") if show else None
    chat.write(note)
    if shown:
        out.emit("rename", title=shown, label=str(note.get("label") or ""))
    return 0


def on_end(chat: Chat, data: Mapping[str, Any]) -> int:
    """Close the registration when the chat ends for good (the spec's close_reasons, or SESSION_LETTER_CLOSE_ON), which
    frees its letter; any other end keeps it for a resume.  Every end reason is kept in the note, so the close rule can
    be checked against what the platform really sends."""
    note = chat.read()
    if not note:
        return 0
    reason = one_line(data.get("reason"))[:40]
    note["ended"] = {"reason": reason, "at": chat.now}
    named = _text(chat.env.get(ENV_CLOSE_ON))
    closing: tuple[str, ...] | None = (
        tuple(r.strip() for r in named.split(",") if r.strip())
        if named
        else chat.spec.close_reasons
    )
    if (closing is None or reason in closing) and note.get("state") == "open":
        answer = chat.registry("close")
        if answer.get("closed") or _gone(answer):
            note["state"] = "closed"  # a registration the Operator no longer has needs no closing
    chat.write(note)
    return 0


def read_hook_input(stream: TextIO) -> dict[str, Any]:
    try:
        raw = stream.read(STDIN_LIMIT)
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def canonical_event(spec: PlatformSpec, data: Mapping[str, Any]) -> str:
    """The library's name for the event in a payload: the platform's own name mapped through the spec, or one of the
    library's names sent as it is (a plugin posting {"event": "start"} needs no mapping)."""
    raw = _text(data.get("hook_event_name")) or _text(data.get("event"))
    if raw in spec.events:
        return spec.events[raw]
    return raw if raw in EVENTS else ""


def hook(
    spec: PlatformSpec,
    env: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    *,
    runner: Runner = subprocess.run,
    now: float | None = None,
    platform: str = sys.platform,
    transport: Transport | None = None,
    location_flag: str = "",
    remote_flag: str = "",
    stderr: TextIO | None = None,
) -> int:
    """Run one hook event for `spec`.  Always 0: whatever goes wrong is a line of context, or nothing."""
    try:
        return _run_hook(
            spec, env, stdin, stdout, runner, now, platform, transport, location_flag, remote_flag
        )
    except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat, whatever an adapter or the host does
        try:
            (stderr or sys.stderr).write("platform-letter hook: %s\n" % type(exc).__name__)
        except (OSError, ValueError):
            pass
        return 0


def _run_hook(
    spec: PlatformSpec,
    env: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    runner: Runner,
    now: float | None,
    platform: str,
    transport: Transport | None,
    location_flag: str,
    remote_flag: str,
) -> int:
    data = read_hook_input(stdin)
    event = canonical_event(spec, data)
    if not event or not in_scope(spec, env, data):
        return 0
    try:
        sid = SI.check(spec.session_id(data, env), "the session id")
    except ValueError:
        return 0
    who = spec.seat_for(data, env)
    if not who:
        return 0
    where = location(env, platform, location_flag)
    out = Out(stdout, spec, _text(data.get("hook_event_name")))
    if where is None:
        if event == "start" and data.get("source") != "compact":
            out.emit(
                event,
                context="No Operator letter: this host is not the Mac, and --location or %s does not name it."
                % ENV_LOCATION,
            )
        return 0
    if where not in spec.locations:
        return 0  # the platform does not run there, so this is not one of its chats
    chat = Chat(
        spec,
        env,
        where,
        sid,
        runner=runner,
        now=time.time() if now is None else now,
        remote=remote_destination(env, remote_flag),
        transport=transport,
        seat=who,
    )
    home = str(env.get("HOME") or os.path.expanduser("~"))
    if event == "start":
        return on_start(chat, data, out, home)
    if event == "prompt":
        return on_prompt(chat, data, out)
    if event == "rename":
        return on_rename(chat, data, out)
    return on_end(chat, data)


def show(spec: PlatformSpec, argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    sid = argv[1] if len(argv) > 1 and argv[0] == "--session-id" else spec.session_id({}, env)
    try:
        sid = SI.check(sid, "the session id")
    except ValueError as exc:
        return _emit(stdout, {"error": str(exc)}, 2)
    try:
        with open(note_path(env, spec.seat_for({}, env) or "UNKNOWN", sid), encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return _emit(stdout, {"session_id": sid, "letter": None}, 1)
    return _emit(stdout, note, 0)


# --------------------------------------------------------------------------------------------
# Installing hooks into a JSON hook file (once per host)
# --------------------------------------------------------------------------------------------


def command_for(home: str, platform: str, where: str = "", remote: str = "") -> str:
    """The hook command an adapter's hook file carries: this host's own `platform-letter` by absolute path, with the
    location and the registry host as flags (a hook file has no env block to put them in)."""
    parts = [
        shlex.quote(os.path.join(home, ".local", "bin", ENTRY)),
        "hook",
        "--platform",
        platform,
    ]
    if where:
        parts += ["--location", where]
    if remote:
        parts += ["--remote", remote]
    return " ".join(parts)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _flag(words: list[str], name: str) -> str:
    for i, word in enumerate(words):
        if word == name and i + 1 < len(words):
            return words[i + 1]
        if word.startswith(name + "="):
            return word[len(name) + 1 :]
    return ""


def installed_command(entry: object, platform: str) -> list[str] | None:
    """The words of the command in a hook entry when it is this platform's `platform-letter hook`, else None.  The
    entry may have any shape: every string in it is looked at."""
    for text in _strings(entry):
        try:
            words = shlex.split(text)
        except ValueError:
            continue
        if (
            len(words) >= 2
            and os.path.basename(words[0]) == ENTRY
            and words[1] == "hook"
            and _flag(words[2:], "--platform") == platform
        ):
            return words
    return None


# A hook entry is often a group (Claude Code and Codex: a matcher and a list of hooks), and the group can hold hooks
# that are not ours, under a matcher that is not ours.  The installer therefore works on this platform's own command
# object (the dict that holds the command string) and leaves the rest of the group as it finds it.
_GONE = object()


def _is_ours(node: dict[str, Any], platform: str) -> bool:
    """The dict holds this platform's command as a string, or in a list of strings, of its own."""
    return any(
        isinstance(s, str) and installed_command(s, platform)
        for v in node.values()
        for s in (v if isinstance(v, list) else [v])
    )


def _command_objects(node: Any, platform: str) -> list[dict[str, Any]]:
    """The dicts in a hook entry that hold this platform's command string directly (the entry itself, when flat)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if _is_ours(node, platform):
            found.append(node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                found += _command_objects(value, platform)
    elif isinstance(node, list):
        for item in node:
            found += _command_objects(item, platform)
    return found


def _strip(node: Any, platform: str) -> Any:
    """A copy of `node` without this platform's command objects.  A group that loses its last hook goes with it
    (`_GONE`), and a group that keeps another tool's hook keeps its matcher and that hook."""
    if isinstance(node, list):
        return [k for k in (_strip(item, platform) for item in node) if k is not _GONE]
    if not isinstance(node, dict):
        return node
    if _is_ours(node, platform):
        return _GONE
    out: dict[str, Any] = {}
    for key, value in node.items():
        if isinstance(value, (dict, list)):
            kept = _strip(value, platform)
            if kept is _GONE or (isinstance(value, list) and value and not kept):
                return _GONE
            out[key] = kept
        else:
            out[key] = value
    return out


def _renew(node: Any, platform: str, fresh: dict[str, Any], used: list[bool]) -> Any:
    """A copy of `node` with the first command object of this platform (in document order, across the entries sharing
    `used`) replaced by `fresh` where it stands, and every later one taken out like `_strip` does."""
    if isinstance(node, list):
        return [k for k in (_renew(item, platform, fresh, used) for item in node) if k is not _GONE]
    if not isinstance(node, dict):
        return node
    if _is_ours(node, platform):
        if used[0]:
            return _GONE
        used[0] = True
        return copy.deepcopy(fresh)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if isinstance(value, (dict, list)):
            kept = _renew(value, platform, fresh, used)
            if kept is _GONE or (isinstance(value, list) and value and not kept):
                return _GONE
            out[key] = kept
        else:
            out[key] = value
    return out


def _vacant(node: Any) -> bool:
    """True for a document that holds nothing but empty objects and lists: the file has nothing left worth keeping."""
    if isinstance(node, dict):
        return all(_vacant(v) for v in node.values())
    if isinstance(node, list):
        return all(_vacant(v) for v in node)
    return False


def hooks_key(doc: dict[str, Any]) -> dict[str, Any]:
    """Where Claude Code and Codex style files keep their events: the top-level "hooks" object."""
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = doc["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ValueError("the hooks field is not an object")
    return hooks


def named_group(name: str, **defaults: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Where a file of named hook groups keeps this adapter's events: the object under the key `name`, created with
    `defaults` (such as enabled: true) when it is absent."""

    def locate(doc: dict[str, Any]) -> dict[str, Any]:
        group = doc.get(name)
        if group is None:
            group = doc[name] = dict(defaults)
        if not isinstance(group, dict):
            raise ValueError("the %s group is not an object" % name)
        return group

    return locate


def command_entry(command: str, timeout: int, event: str) -> dict[str, Any]:
    """One hook entry in the shape Claude Code and Codex use."""
    return {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}


@dataclass(frozen=True)
class JsonHookInstaller:
    """How an adapter's platform keeps its hooks, for `install_json_hooks`.

    path         the hook file, under the home directory unless absolute (~/.codex/hooks.json)
    events       the platform's event names this adapter hooks, with a timeout in seconds each (a SessionEnd
                 budget is often 1.5 seconds, too short for the ssh hop, so every entry names its own)
    locate       finds the object that maps an event name to its list of entries, creating it when absent
    build        builds one entry from (command, timeout, event) in the platform's own shape
    whole_file   the file holds only this adapter's hooks (a ~/.grok/hooks/*.json file of its own): --remove
                 deletes it, after a backup, once no hook is left
    trust        what the platform needs after a hook is added (Codex trusts each new entry by hash, Muse needs
                 its plugin approved): reported in the install answer for the owner or the orchestrator to run;
                 the installer never runs it
    """

    path: str
    events: Mapping[str, int]
    locate: Callable[[dict[str, Any]], dict[str, Any]] = hooks_key
    build: Callable[[str, int, str], Any] = command_entry
    whole_file: bool = False
    trust: str = ""


@dataclass(frozen=True)
class InstallOptions:
    platform: str
    location: str = ""
    remote: str = ""
    check: bool = False
    remove: bool = False


def _write_private(path: str, data: bytes, mode: int, *, exclusive: bool = False) -> None:
    """Write a file that may hold secrets: created 0600, flushed and fsynced, then given `mode`; nothing is ever
    readable by others while it is written.  With `exclusive` an existing file is an error (FileExistsError), never
    overwritten."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, mode)


def _backup(path: str) -> str:
    """Copy `path` to `<path>.bak-letter-<UTC stamp>` (0600) and return the new name.  The stamp has microseconds and
    a name that is taken gets a counter, so two rewrites in the same moment (an install, then a remove) never share a
    backup: the first one holds the file as the owner left it."""
    with open(path, "rb") as src:
        data = src.read()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for n in range(1000):
        name = "%s.bak-letter-%s%s" % (path, stamp, "-%d" % n if n else "")
        try:
            _write_private(name, data, 0o600, exclusive=True)
        except FileExistsError:
            continue
        return name
    raise OSError("no free backup name for %s" % path)


def _plan_event(
    installer: JsonHookInstaller,
    events: Mapping[str, Any],
    ev: str,
    timeout: int,
    command: str,
    platform: str,
    remove: bool,
) -> tuple[list[Any], list[Any]]:
    """(the entries `ev` holds after the install or the remove, the entries it holds now)."""
    entries = list(events.get(ev) or [])
    if remove:
        return _strip(entries, platform), entries
    wanted = installer.build(command, timeout, ev)
    if not any(installed_command(e, platform) for e in entries):
        return [*entries, wanted], entries
    fresh = next(iter(_command_objects(wanted, platform)), wanted)
    return _renew(entries, platform, fresh, [False]), entries


def install_json_hooks(
    installer: JsonHookInstaller, opts: InstallOptions, env: Mapping[str, str], stdout: TextIO
) -> int:
    """Add (or, with remove, take out; with check, report) one adapter's hooks in a JSON hook file.

    It is idempotent: an entry that is already current is left where it is, an outdated one is replaced in place,
    and a missing one is appended, so running it twice changes nothing and no other hook is ever removed or
    reordered.  Only this platform's own command is touched: a group that also holds another tool's hook
    keeps its matcher and that hook, and a group is dropped only when it has no hook left.  A file it
    rewrites is backed up first (`<file>.bak-letter-<UTC stamp>`, 0600, a name no earlier backup has) and keeps its
    mode.  A file that is not the shape the adapter's `locate` expects is refused, never edited.  The
    location and registry host go into the hook command as flags; running it again without them keeps the ones
    already installed."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    path = installer.path if os.path.isabs(installer.path) else os.path.join(home, installer.path)
    if opts.remote and not DEST_RE.fullmatch(opts.remote):
        return _emit(stdout, {"error": "--remote must name an ssh destination"}, 2)
    if opts.location and opts.location not in LOCATIONS:
        return _emit(stdout, {"error": "--location must be mac or server"}, 2)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        existed = True
    except FileNotFoundError:
        doc, existed = {}, False
    except ValueError:
        return _emit(stdout, {"error": "%s is not valid JSON" % path}, 2)
    if not isinstance(doc, dict):
        return _emit(stdout, {"error": "%s is not a JSON object" % path}, 2)
    try:
        events = installer.locate(doc)
        for name in installer.events:
            if events.get(name) is not None and not isinstance(events[name], list):
                raise ValueError("the %s field is not a list" % name)
    except ValueError as exc:
        return _emit(stdout, {"error": "%s: %s" % (path, exc)}, 2)
    platform = opts.platform
    already = [
        installed_command(e, platform) for ev in installer.events for e in events.get(ev) or []
    ]
    inherited = next((w for w in already if w), [])
    where = opts.location or _flag(inherited, "--location")
    remote = opts.remote or _flag(inherited, "--remote")
    command = command_for(home, platform, where, remote)
    have = {
        ev: any(installed_command(e, platform) for e in events.get(ev) or [])
        for ev in installer.events
    }
    if opts.check:
        current = {}
        for ev, timeout in installer.events.items():
            new, entries = _plan_event(installer, events, ev, timeout, command, platform, False)
            current[ev] = have[ev] and new == entries  # an install would change nothing
        answer = {"platform": platform, "path": path, "installed": have, "current": current}
        return _emit(stdout, answer, 0 if all(have.values()) else 1)
    changed = []
    for ev, timeout in installer.events.items():
        new, entries = _plan_event(installer, events, ev, timeout, command, platform, opts.remove)
        if new != entries:
            changed.append(ev)
        if new:
            events[ev] = new
        else:
            events.pop(ev, None)
    drop = installer.whole_file and opts.remove and _vacant(doc)
    if not changed:
        return _emit(stdout, {"platform": platform, "changed": [], "path": path}, 0)
    backup = ""
    if existed:
        file_mode = os.stat(path).st_mode & 0o777
        backup = _backup(path)
    else:
        file_mode = 0o600
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if drop:
        os.remove(path)
    else:
        body = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".letter-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, file_mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    answer = {"platform": platform, "changed": changed, "path": path, "backup": backup}
    if drop:
        answer["removed_file"] = True
    if not opts.remove:
        answer["command"] = command
        if installer.trust:
            answer["trust"] = installer.trust
    return _emit(stdout, answer, 0)


def install_main(argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    """`install --platform P [--location server|mac] [--remote DEST] [--check | --remove]`: find the platform's
    adapter and run its installer.  An adapter with its own `install(options, env, stdout)` (a plugin directory, a
    bundle) is called as it is; one with an `INSTALL` (a JsonHookInstaller) gets `install_json_hooks`."""
    import argparse

    parser = argparse.ArgumentParser(prog=ENTRY + " install")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--location", choices=LOCATIONS)
    parser.add_argument(
        "--remote", help="the ssh destination that holds the registry (a host off the registry)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    opts = InstallOptions(
        args.platform, args.location or "", args.remote or "", args.check, args.remove
    )
    spec = load_spec(args.platform)
    adapter = load_adapter(args.platform)
    if spec is None:
        return _emit(stdout, {"error": "no adapter for the platform %r" % args.platform}, 2)
    custom = getattr(adapter, "install", None)
    if callable(custom):
        return int(custom(opts, env, stdout))
    installer = getattr(adapter, "INSTALL", None)
    if not isinstance(installer, JsonHookInstaller):
        return _emit(stdout, {"error": "the %s adapter has no installer" % args.platform}, 2)
    return install_json_hooks(installer, opts, env, stdout)


# --------------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------------

USAGE = (
    "usage: platform-letter {hook --platform P [--location L] [--remote DEST]\n"
    "                        | registry OP --platform P ...\n"
    "                        | show --platform P [--session-id S]\n"
    "                        | install --platform P [--location L] [--remote DEST] [--check | --remove]}\n"
)


def _flags(argv: list[str], names: tuple[str, ...]) -> dict[str, str]:
    """`--name value` and `--name=value` for the given names; anything else is a ValueError."""
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        word = argv[i]
        name, eq, value = word.partition("=")
        if name not in names:
            raise ValueError("unknown argument")
        if not eq:
            i += 1
            if i >= len(argv):
                raise ValueError("missing value")
            value = argv[i]
        out[name.lstrip("-")] = value
        i += 1
    return out


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    runner: Runner = subprocess.run,
    transport: Transport | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if not argv or argv[0] in ("-h", "--help"):
        stdout.write(USAGE)
        return 0 if argv else 2
    if argv[0] == "hook":
        try:
            flags = _flags(argv[1:], ("--platform", "--location", "--remote"))
            spec = load_spec(flags.get("platform", ""))
            if spec is None:
                stderr.write("platform-letter hook: no adapter for the platform\n")
                return 0
            return hook(
                spec,
                env,
                stdin,
                stdout,
                runner=runner,
                transport=transport,
                location_flag=flags.get("location", ""),
                remote_flag=flags.get("remote", ""),
                stderr=stderr,
            )
        except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
            stderr.write("platform-letter hook: %s\n" % type(exc).__name__)
            return 0
    if argv[0] == "registry":
        return registry_main(argv[1:], env=env, stdout=stdout)
    if argv[0] == "show":
        try:
            flags = _flags(argv[1:], ("--platform", "--session-id"))
        except ValueError:
            stderr.write(USAGE)
            return 2
        spec = load_spec(flags.get("platform", ""))
        if spec is None:
            return _emit(stdout, {"error": "no adapter for the platform"}, 2)
        return show(
            spec,
            ["--session-id", flags["session-id"]] if "session-id" in flags else [],
            env,
            stdout,
        )
    if argv[0] == "install":
        return install_main(argv[1:], env, stdout)
    stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
