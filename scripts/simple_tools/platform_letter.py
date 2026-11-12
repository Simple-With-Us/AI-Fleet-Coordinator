"""`platform-letter` (also `simple-chat letter`):  an Operator session letter, and the title "<letter>: <Title>", for
every interactive chat on every platform (owner request, Oct 29, 2026;  one client for all of them, Nov 11, 2026).

    platform-letter hook --platform P       the hook a platform runs: start, prompt, rename, turn and end events
    platform-letter registry OP --platform P ...   one registry API call: register, title, touch, close or show
    platform-letter show --platform P       this chat's letter from its local note; no network
    platform-letter install --platform P    add the platform's hooks (--check, --remove)
    platform-letter request [--session-id S]   ask the assigner for this chat's letter now (`request_main`)
    platform-letter gone [--platform P] [--apply]   the chats of this host that are gone for good (archived in
                                            a platform's app):  what the hooks would report, or report it now
    platform-letter parent --platform P --session-id S   the chat that started a chat, from this host's own
                                            records:  what another host's hook asks this one (read only)

`simple-chat letter <verb>` is the same command with the same verbs.  An adapter is one file,
`simple_tools/platform_letter_<platform>.py`, with a `SPEC` (a PlatformSpec: where the platform's session id, parent
and title come from, and how a title is written) and, to be installable, an `INSTALL` (a JsonHookInstaller) or its own
`install(options, env, stdout)` for a platform whose hook configuration is not a JSON hook file.  The library does
everything else, once, for every platform:  the in-scope rule, the registry call, the note, the title, retry, touch
and close.  Claude Code and MiniMax Code are rows of the table like every other platform.

Where the letter comes from.  One assigner:  the Operator's session registry behind the Eavesdropper's registry API
(`listener_registry`, reached through `registry_api`).  Every chat on every host asks it the same way, one signed
HTTPS call per op, and nothing else:  there is no local registry file and no ssh hop (owner, Nov 11, 2026:  "one
operator that has one tool/process for all platforms").  The API's address and key come from the process
environment or, when it names neither, from Infisical through fleet_knobs (`registry_api`);  a host with neither
gets `not_configured`, which is one more way of getting no letter.  The registry keys a letter's sequence on the
seat label;  `platform` here is only the registry identity of the chat (with location, scope and session id).

No letter.  A chat that gets no letter, for any reason (the assigner refused because every pair is live, it did not
answer, the host is not configured, the location is not stated, any other error), is titled "??: <Title>" and carries
on:  nothing here ever fails a session start.  A chat that already holds a letter keeps it when a call merely fails;
only a refusal, or a registry with no registration for it, takes it off.  Later prompt, rename and turn events ask
again, at most every RETRY_SECONDS (the note's `failed_at` stamp), and the first answer that carries a letter swaps
"??" for it.  `simple-chat letter request` asks at once, whatever the stamp says, and prints the letter or the refusal
in one line.

The letters.  Every chat takes a pair (AA to ZZ).  The allocation is the assigner's own rule (`letter_space="double"`,
`letter_pick`, `parent_session_id`):  a main chat takes a doubled pair, a sub-thread the pair after its parent's
(parent CC, child CD, the next CE).  This module only asks.

What the hook does.
- A start event registers the chat, or resumes its registration, and titles it: "<letter>: <Title>" when the chat
  already has a title (or a stored one the adapter finds), else "<letter>: New Chat" until its first prompt.  A
  `compact` start only reminds the model of the letter.
- A prompt event titles a chat that is still on the placeholder from its first prompt, asks again for a missing
  letter (at most every ten minutes), puts the letter back on a chat whose name dropped it and sends the new words to
  the Operator, and marks the chat seen at most every half hour.  Steady-state prompts cost no call.
- A rename event does the rename part alone, for platforms that tell the hook when a title changed.
- A turn event (the end of a turn) reports what the turn touched, and asks again for a missing letter as a prompt does.
- An end event closes the registration when the chat is over for good (the spec's `close_reasons`, `None` for every
  end), which frees its letter.  Reclaiming idle letters is otherwise the assigner's own sweep:  no client sweeps.

Chats that are gone for good.  A platform's app can end a chat with no hook event of its own (a desktop app that
archives one), so its letter would stay taken until the assigner's idle sweep.  An adapter that can tell names
those chats, and a chat's start reports them:  one `report-gone` call to the assigner for the platform and a
location, at most once per GONE_REPORT_SECONDS (a stamp beside the notes).  `gone_sessions` answers for this
host's own chats whose hook has been quiet for an hour, however long ago that was.  `gone_elsewhere` answers for
chats the platform's app on this host runs at another location, whose notes this host does not hold;  they go in
a report of their own for that location.  The assigner closes the open rows named, and refuses a report that
would close more than its share of a location's live rows.  Nothing schedules it, and nothing it does can fail a
chat's start (`report_gone`, `report_gone_elsewhere`).  `gone` shows what would be reported, and `gone --apply`
reports it now.

A parent on another host.  A platform's app can keep its record of which chat started which on one host (the
Mac) while the chat's hook runs on another.  SESSION_LETTER_MAC_SSH (the hook's `--mac-ssh` flag, which `install
--mac-ssh DEST` writes) names the ssh destination of the Mac for an adapter that can ask it (`mac_ssh`), and
`parent` is the read-only verb such an adapter runs there:  it prints the parent's session id, or nothing.  A
chat's parent is asked for once:  the note keeps one that was found, and a lookup that found none is not
repeated for RETRY_SECONDS (`parent_of`).

The title reaches the chat the way the platform allows: in the hook's own output (`hook` style, a `sessionTitle`
field), through the adapter's `title_writer` (a callable that renames the chat natively), or from a plugin that reads
the `json` output.  A writer that fails leaves the title pending and is tried again on the next prompt.  A platform
with neither route is told its letter and asked to keep it in a name the owner gives.  A platform that takes the
hook's title for some chats only says so through its adapter's `title_advice`:  the line it answers goes to the
chat's own model with the start or prompt context, once per RETRY_SECONDS while it stays the same (`advice`).

The hook never blocks a chat: every failure is a line of context, or nothing, and exit 0.  Only interactive chats
take part: a launcher-run session never does (`identity.launched`, unless the spec names that launcher), nor a
headless or exec run, nor a sub-agent run unless the platform records the chat that started it.  SESSION_LETTER=0
turns it off, the same switch on every platform.  The location is the fleet location, never a guess: darwin is the
Mac, and any other host must say `--location server` (or SESSION_LETTER_LOCATION), and a chat on a host that does not
say is a "??" chat until it does.  Titles go through the fleet's secret scanner before they are stored or relayed.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import copy
import io
import json
import os
import pkgutil
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
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
# The locations the registry side takes:  a hook runs on the Mac or the server, and a chat no hook runs in (a Cursor
# Cloud agent, which `cursor_cloud_letters` registers from the Cursor API) is in the cloud.
REGISTRY_LOCATIONS = LOCATIONS + ("cloud",)
PLATFORM_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
LABEL_RE = re.compile(r"[A-Z]{1,2}")

ENV_OFF = "SESSION_LETTER"
ENV_LOCATION = "SESSION_LETTER_LOCATION"
ENV_CLOSE_ON = "SESSION_LETTER_CLOSE_ON"
#: The ssh destination of the Mac, for a hook on another host whose adapter must ask the Mac something only it
#: holds (`mac_ssh`).  The hook's `--mac-ssh DEST` flag sets it, since a hook file has no env block.
ENV_MAC_SSH = "SESSION_LETTER_MAC_SSH"
#: What an ssh destination may look like:  a host or user@host, and nothing ssh could read as an option.
MAC_SSH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,252}")

TOUCH_SECONDS = 1800
# The stamp that throttles asking again:  a chat whose ask failed (no letter, or a send the registry did not take)
# asks again no sooner than this after `failed_at` (or `sync_failed_at`).  `simple-chat letter request` ignores it.
RETRY_SECONDS = 600
# The gone report (`report_gone`, `report_gone_elsewhere`):  a host reports the gone chats of a platform and
# location at most this often, names only chats of its own whose hook last ran at least GONE_QUIET_SECONDS ago
# (one that ran since is plainly not gone), reads at most GONE_SCAN_NOTES of the newest notes to find them, names
# at most GONE_REPORT_IDS in one call (the assigner's own limit, listener_registry.MAX_GONE_IDS), and remembers
# at most GONE_SENT_IDS chats of another location that it has reported.
GONE_REPORT_SECONDS = 3600
GONE_QUIET_SECONDS = 3600
GONE_SCAN_NOTES = 2000
GONE_REPORT_IDS = 500
GONE_SENT_IDS = 2000
#: What a chat with no letter carries where the letter goes ("??: Fix Login"), owner Nov 11, 2026:  "just make threads
#: label themselves ??: Title whenever no letters are received".
UNLETTERED = "??"
# How many generations of parents (a sub-thread's parent, that parent's parent, and so on) are registered ahead of a
# sub-thread, a bound against a store that names a loop.
PARENT_DEPTH = 3
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
# Why a chat on a host that did not state its fleet location gets no letter (it is a "??" chat until it does).
NO_LOCATION = (
    "this host's fleet location is not stated (--location or %s)" % "SESSION_LETTER_LOCATION"
)

#: The events the library knows, whatever a platform calls them.  `turn` is the end of one turn (Claude Code's and
#: ZCode's Stop):  it reports what the turn touched (AFC-702) and never closes anything.  A spec maps its own event to
#: it;  none does by default.
EVENTS = ("start", "prompt", "rename", "end", "turn")
#: The event names Claude Code and Codex style hooks use, and the default for a spec.
DEFAULT_EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "SessionEnd": "end"}
#: Timeouts in seconds for those hook entries.  A register chains at most three registry API calls (register, reopen,
#: update) of _API_TIMEOUT each, and a SessionEnd budget is often 1.5 seconds, too short for that, so every entry
#: names its own.
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
#: The no-letter mark at the head of a title, as many times as a platform stacked it.
UNLETTERED_PREFIX = re.compile(r"^(?:\?\?: ?)+")
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
    start, prompt, rename, retry or reissue.  `shown` is the title the hook itself last put on the chat ("" when
    none) and `held` the letters the chat holds or held, so a writer that guards a person's name can tell the hook's
    own earlier title ("??: Words", "CC: Words") from a name a person gave.  The writer returns True when the chat
    now carries the title."""

    platform: str
    session_id: str
    label: str
    title: str
    words: str
    current: str
    reason: str
    data: Mapping[str, Any]
    env: Mapping[str, str]
    shown: str = ""
    held: tuple[str, ...] = ()


TitleWriter = Callable[[TitleRequest], bool]
#: An adapter's `title_advice`:  from the title the hook wants on the chat (a TitleRequest, `reason` start or
#: prompt), the line to give the chat's own model when the platform will not take that title from the hook, or "".
TitleAdvice = Callable[[TitleRequest], str]
#: An adapter's `gone_sessions`:  from the environment and the session ids of this host's quiet chats, the ids of
#: the ones that are gone for good (its app archived them).  Ids only, and any doubt leaves a chat out.
GoneSessions = Callable[[Mapping[str, str], frozenset[str]], Iterable[str] | None]
#: An adapter's `gone_elsewhere`:  from the environment and a location other than this host's, the session ids
#: of chats that run there and are gone for good, as this host's own records of them say (a platform's app on
#: one host can run chats on another).  Ids only, bounded by the adapter, and any doubt leaves a chat out.
GoneElsewhere = Callable[[Mapping[str, str], str], Iterable[str] | None]


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
    session_env                the environment variables that hold the chat's session id inside the chat's own
                               shell, for `request` run with no --session-id (AGENT_SESSION is read for every spec)
    title_writer / title_in_output   how a title reaches the chat (see the module docstring)
    output                     "hook" (Claude style hookSpecificOutput), "text" (plain stdout) or "json"
    launchers                  the launchers whose sessions DO take a letter under this spec (BotFleet's own spec
                               names "botfleet"); a session any other launcher started never does
    title_sources              the start sources after which a hook-output title is applied
    locations                  the fleet locations the platform runs on (Grok and Antigravity: only "mac"); a hook
                               on any other takes no letter
    quiet_events               platform events whose hook output the platform does not take (Codex's SubagentStop has
                               no hookSpecificOutput): the hook still runs, and prints nothing for them
    chat_payload               from a native session id, the payload that chat's own start event would carry; it lets
                               the library look up a chat the hook did not fire for.  A sub-thread registers after its
                               parent, so a spec that has it registers a parent that holds no live registration first
    title_advice               for a platform that takes the hook's title for some chats only:  from the title the
                               hook wants on the chat, the line that has the chat's own model set it, or "" (`advice`)
    gone_sessions              for a platform whose app ends chats with no hook event (an archive):  which of the
                               session ids asked about are gone for good, for the hooks' gone report (`report_gone`)
    gone_elsewhere             for a platform whose app on this host runs chats at another location:  the session
                               ids of the ones there that are gone for good (`report_gone_elsewhere`)
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
    quiet_events: tuple[str, ...] = ()
    chat_payload: Callable[[str], Mapping[str, Any]] | None = None
    session_env: tuple[str, ...] = ()
    title_advice: TitleAdvice | None = None
    gone_sessions: GoneSessions | None = None
    gone_elsewhere: GoneElsewhere | None = None

    def __post_init__(self) -> None:
        if not PLATFORM_RE.fullmatch(self.platform):
            raise ValueError("%r is not a platform name" % self.platform)
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
    def titles(self) -> bool:
        """The hook can put a title on the chat:  through a writer, or in its own output."""
        return self.title_writer is not None or self.title_in_output or self.output == "json"

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
    """The adapter module `simple_tools/platform_letter_<platform>.py`, or None.  The name is checked first, so an
    argument can never name any other module."""
    if not PLATFORM_RE.fullmatch(platform or ""):
        return None
    import importlib

    name = "%s.platform_letter_%s" % (__package__ or "simple_tools", platform.replace("-", "_"))
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
    turns the hook off, on every platform."""
    if _text(env.get(ENV_OFF)) == "0":
        return False
    if I.launched(env) and I.launcher_of(env) not in spec.launchers:
        return False
    kind = _text(spec.kind(data, env))
    if kind == "chat":
        return True
    return kind == "subagent" and bool(spec.parent_id(data, env))


def location(env: Mapping[str, str], platform: str = sys.platform, flag: str = "") -> str | None:
    """The fleet location of this chat, or None when it cannot be stated: darwin is the Mac, and any other host
    must say so, with --location or SESSION_LETTER_LOCATION.  A value that is not a fleet location is refused rather
    than passed on."""
    forced = (_text(flag) or _text(env.get(ENV_LOCATION))).lower()
    if forced:
        return forced if forced in LOCATIONS else None
    return "mac" if platform == "darwin" else None


def mac_ssh(env: Mapping[str, str]) -> str:
    """The ssh destination of the Mac this host was told (SESSION_LETTER_MAC_SSH, or the hook's `--mac-ssh`), or
    "" when it names none or names something that is not a destination.  For an adapter whose platform keeps what
    it needs on the Mac while the chat's hook runs on another host:  the destination is configuration, never a
    constant, and nothing in this library connects anywhere by itself."""
    dest = _text(env.get(ENV_MAC_SSH))
    return dest if MAC_SSH_RE.fullmatch(dest) else ""


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
    letter.  The no-letter mark ("??: ") always comes off, however many times it is there, so a chat that gets its
    letter later, or asks again, never carries "XX: ??: Words" or "??: ??: Words"."""
    line = UNLETTERED_PREFIX.sub("", one_line(title), count=1)
    if line in (UNLETTERED, UNLETTERED + ":"):
        return ""  # the name was cleared down to the mark alone
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
# The registry side:  one registry API call per op
# --------------------------------------------------------------------------------------------
#
# operator_reopen, SpaceLost and _last_letter below are the assigner's own helpers, not a client path:  the registry
# CLI (operator_registry) imports them from here for a reopen.  with_registry_env is the wake delivery's
# (claude_wake), not the letter client's:  the letter client reads the API's address and key from its environment or
# from Infisical (`registry_api`), never from a host file.


def valid_platform(platform: object) -> bool:
    return isinstance(platform, str) and bool(PLATFORM_RE.fullmatch(platform))


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


#: The registry's own sentence for a session it does not hold, raised by its mutation functions
#: ("session ... is not registered on ...; register it first") and rendered by the API as the
#: call's error.  The client folds it back to NO_REGISTRATION, the answer the hook reads as "no registration for this
#: chat".
_NOT_REGISTERED_MARK = "is not registered on"


#: How long one registry API call from a hook may take.  The hooks carry budgets of 15 to
#: 20 seconds (HOOK_TIMEOUTS) and a register chains at most three calls (register, reopen,
#: update), so one call gets 4:  the chain's worst case stays inside the tightest budget, and
#: a healthy call to the listener answers in well under a second.  An unsettled parent is
#: registered first, at most once per RETRY_SECONDS (`_ensure_parent`).  The batch callers of
#: registry_api (the operator-registry CLI) keep its longer default.
_API_TIMEOUT = 4
REGISTRY_ENV_SUBPATH = os.path.join(".local", "bin", "operator-registry-env")


def with_registry_env(env: Mapping[str, str]) -> Mapping[str, str]:
    """Return `env` enriched from `$HOME/.local/bin/operator-registry-env` when it names no registry API URL.

    The wake delivery's helper (claude_wake), not the letter client's:  the letter client never reads this file.  It
    reads configuration only, never a store:  with nothing found, the call answers `not_configured`."""
    from . import registry_api as RA

    if RA.base_url(env):
        return env
    home = _text(env.get("HOME"))
    if not home:
        return env
    path = os.path.join(home, REGISTRY_ENV_SUBPATH)
    if not os.path.isfile(path):
        return env
    names = (RA.URL_ENV, RA.PRINCIPAL_ENV, RA.KEY_VAR)
    script = (
        '. "$1" >/dev/null 2>&1 || true\n'
        "for k in %s; do\n"
        '  eval "v=\\${$k:-}"\n'
        '  [ -n "$v" ] && printf "%%s=%%s\\0" "$k" "$v"\n'
        "done\n"
        "exit 0\n"
    ) % " ".join(names)
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", script, "operator-registry-env", path],
            env=dict(env),
            capture_output=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return env
    if proc.returncode != 0 or not proc.stdout:
        return env
    merged = dict(env)
    for item in proc.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        k, _, v = item.partition(b"=")
        try:
            key_str = k.decode("utf-8")
            val_str = v.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if val_str and not _text(merged.get(key_str)):
            merged[key_str] = val_str
    return merged


def _call_api(env: Mapping[str, str], op: str, params: Mapping[str, Any]) -> dict[str, Any]:
    from . import registry_api

    return registry_api.call(env, op, params, timeout=_API_TIMEOUT)


def _api_error(answer: Mapping[str, Any]) -> dict[str, Any] | None:
    """The answer itself when it carries an error, folded when the error is the registry saying
    the session is not registered;  None when it carries none."""
    if "error" not in answer:
        return None
    # The API names this refusal with the code not_registered as well as with the registry's
    # sentence, and its other sentences ("no session with this identity") carry no mark to
    # match, so the fold checks the code first.
    if answer.get("code") == "not_registered" or _NOT_REGISTERED_MARK in str(
        answer.get("error") or ""
    ):
        return {"error": NO_REGISTRATION}
    return dict(answer)


def _api_identity(args: Any, sid: str) -> dict[str, Any]:
    return {
        "platform": args.platform,
        "location": args.location,
        "scope": SCOPE,
        "session_id": sid,
    }


#: The tail of the refusal a chat reads when every main-chat pair is live (`refused_text`), which `refused` matches.
REFUSED_TAIL = "the Operator refused this chat a letter"


def refused_text(OR: Any, platform: str, limit: int = 0) -> str:
    """What a chat reads when every main-chat pair is live:  the limit is the 26 doubled pairs, not every pair."""
    limit = limit or len(getattr(OR, "PRIMARY_ORDER", ())) or 26
    return "all %d %s main-chat pairs (AA to ZZ) are live:  %s" % (limit, platform, REFUSED_TAIL)


def _register_api(OR: Any, env: Mapping[str, str], args: Any, sid: str) -> dict[str, Any]:
    """Register one chat through the registry API:  resume it, or reopen it when it comes back closed.

    The registry's register answers for a fresh chat and for a resume (its resume stamps the session seen), a
    resumed-closed chat is reopened, and a resumed or reopened chat that arrived with a title has the title carried
    over, because register keeps an existing record's old title.  A client sweeps nothing and evicts nothing:
    reclaiming letters is the assigner's own sweep, and an exhausted space answers with the assigner's refusal (the
    chat is then a "??" chat) instead of this hook closing another chat to make room."""
    title = clean_title(args.title)
    pick = resolve_pick(OR, args.letter_pick)
    parent = ""
    if args.parent_session_id:
        try:
            parent = SI.check(args.parent_session_id, "the parent session id")
        except ValueError:
            parent = ""  # not a session id, so no parent: the chat is picked like a main chat
    params: dict[str, Any] = {
        **_api_identity(args, sid),
        "seat": args.seat,
        "source": args.source,
        "title": title,
        "letter_space": args.letter_space,
        "letter_pick": pick,
    }
    if parent:
        params["parent_session_id"] = parent
    out = _call_api(env, "register", params)
    if out.get("code") == "labels_exhausted":
        return {"error": refused_text(OR, args.platform), "swept": 0}
    error = _api_error(out)
    if error is not None:
        return error
    if out.get("outcome") == "resumed_closed":
        reopen_params: dict[str, Any] = {
            "record_id": out["record_id"],
            "actor": args.seat,
            "source": args.source,
            "letter_pick": pick,
        }
        if parent:
            reopen_params["parent_session_id"] = parent
        reopened = _call_api(env, "reopen", reopen_params)
        if reopened.get("code") == "labels_exhausted":
            return {"error": refused_text(OR, args.platform), "swept": 0}
        if reopened.get("code") == "space_lost":
            return {
                "error": "this registry cannot give a closed chat a pair letter again yet",
                "swept": 0,
            }
        error = _api_error(reopened)
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
        updated = _call_api(env, "update", {**_api_identity(args, sid), "title": title})
        error = _api_error(updated)
        if error is not None:
            return error
        # The register answer above still names the title the record already carried, on a
        # reopen as on a resume;  the update's answer names the one just set, as the local
        # path's does.
        out = {**out, "title": updated.get("title"), "title_display": updated.get("title_display")}
    out["swept"] = 0
    out["evicted"] = 0
    return out


def _on_record_api(OR: Any, env: Mapping[str, str], args: Any, sid: str) -> dict[str, Any]:
    """title, touch, close and show for one chat through the registry API.

    No record is NO_REGISTRATION and a closed one is retired, before any op writes.  The API's ops are addressed by
    identity and answer in the registry's own words, so this path probes with the touch op:  a touch stamps a live
    session seen, answers outcome "closed" for a closed one without writing anything, and refuses a session the
    registry does not hold.  A current registry's touch answer also names the letter and generation, and this path
    passes them on (`_touch_answer`); an older registry's names neither, and the answer then carries the record id
    alone, which the hook reads as no change."""
    identity = _api_identity(args, sid)
    if args.op == "show":
        # The API has no show op:  `held` answers the letter side of the record (its id and
        # the letters it has held), which is what a show is asked for.
        held = _call_api(env, "held", identity)
        if "error" in held:
            if held.get("code") == "not_registered":
                return {"error": NO_REGISTRATION}
            return dict(held)
        shown = {
            "record_id": held.get("record_id", ""),
            "label": held.get("label", ""),
            "letters": held.get("letters", []),
            "found": True,
        }
        if shown["label"]:
            # The rest of the row (its seat and title) from the letter's own live holder, when that is this record:
            # a caller that closes only what is its own (the fx sync) reads the seat here.
            row = _call_api(env, "resolve", {"platform": args.platform, "letter": shown["label"]})
            if "error" not in row and row.get("record_id") == shown["record_id"]:
                shown.update({k: row[k] for k in ("seat", "title", "generation") if k in row})
        return shown
    if args.op == "close":
        out = _call_api(
            env, "close", {**identity, "reason": "session ended", "source": args.source}
        )
        error = _api_error(out)
        if error is not None:
            return error
        if out.get("outcome") == "already_closed":
            return {"retired": True, "record_id": out.get("record_id", "")}
        return {"closed": bool(out.get("closed")), "label": out.get("label", "")}
    if args.op == "issues":
        return record_issues_api(env, identity, args.issues)
    probe = _call_api(env, "touch", identity)
    error = _api_error(probe)
    if error is not None:
        return error
    if probe.get("outcome") == "closed":
        return {"retired": True, "record_id": probe.get("record_id", "")}
    title = clean_title(args.title) if args.op == "title" else ""
    if args.op == "touch" or not title:
        return _touch_answer(probe)
    updated = _call_api(env, "update", {**identity, "title": title})
    error = _api_error(updated)
    if error is not None:
        return error
    out = {
        "ok": True,
        "label": updated.get("label", ""),
        "generation": updated.get("generation"),
    }
    if updated.get("record_id"):
        out["record_id"] = updated[
            "record_id"
        ]  # as a touch's answer carries it, for the hook's note
    return out


def _touch_answer(probe: Mapping[str, Any]) -> dict[str, Any]:
    """The registry side's answer to a touch that found a live session, from the API's touch answer.

    The letter and generation go on when the registry named a letter, so the hook can follow a letter
    the registry changed in place.  A registry from before the touch named them answers without
    them, and so does this answer: the hook reads a missing letter as no change."""
    out: dict[str, Any] = {"ok": True, "record_id": probe.get("record_id", "")}
    if probe.get("label"):
        out["label"] = probe["label"]
        if probe.get("generation") is not None:
            out["generation"] = probe["generation"]
    return out


def _report_gone_api(env: Mapping[str, str], args: Any) -> dict[str, Any]:
    """The `report-gone` op:  one call that names the chats of one platform and host that are gone for good, and
    has the assigner close their rows.

    The list is JSON, and every entry must be a session id:  a list with anything else in it is refused whole,
    before any call.  An empty list makes no call.  The answer names the session ids whose rows were closed now
    (`gone`) and the letter each one held (`labels`);  a chat whose row was closed already, or that was never
    registered at that location, is simply not among them.  A refusal is the assigner's own answer, with its `code` (`gone_share_exceeded`) and `allowed_now`."""
    try:
        named = json.loads(args.gone)
    except ValueError:
        named = None
    if not isinstance(named, list):
        return {"error": "the gone list must be a JSON list of session ids"}
    ids: list[str] = []
    for item in named:
        try:
            sid = SI.check(item, "a gone session id")
        except ValueError:
            return {"error": "the gone list must be a JSON list of session ids"}
        if sid not in ids:
            ids.append(sid)
    if len(ids) > GONE_REPORT_IDS:
        return {"error": "one report names at most %d chats" % GONE_REPORT_IDS}
    if not ids:
        return {"ok": True, "gone": [], "count": 0}
    out = _call_api(
        env,
        "report-gone",
        {
            "platform": args.platform,
            "location": args.location,
            "scope": SCOPE,
            "native_session_ids": ids,
        },
    )
    if "error" in out:
        return dict(out)
    labels = {
        str(row.get("native_session_id") or ""): str(row.get("label") or "")
        for row in out.get("closed") or []
        if isinstance(row, Mapping)
    }
    labels.pop("", None)
    return {"ok": True, "gone": sorted(labels), "count": len(labels), "labels": labels}


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
    parser.add_argument(
        "op", choices=("register", "title", "touch", "close", "show", "issues", "report-gone")
    )
    parser.add_argument("--platform", required=True)
    parser.add_argument("--location", required=True, choices=REGISTRY_LOCATIONS)
    parser.add_argument("--session-id", default="")  # every op but report-gone is about one chat
    parser.add_argument("--seat", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--issues", default="[]")  # the `issues` op's list, as JSON (AFC-694)
    parser.add_argument("--gone", default="[]")  # the `report-gone` op's session ids, as JSON
    parser.add_argument("--source", default="")
    parser.add_argument("--parent-session-id", default="")
    parser.add_argument("--letter-space", default="double", choices=("single", "double"))
    parser.add_argument(
        "--letter-pick", default="auto", choices=("auto", "lowest", "oldest", "primary")
    )
    return parser


def registry_main(
    argv: list[str], *, env: Mapping[str, str], stdout: TextIO, now: float | None = None
) -> int:
    """`registry OP --platform P --location L --session-id S [--seat SEAT] [--title T] [--source SRC]
    [--parent-session-id PID] [--letter-space single|double] [--letter-pick PICK]`, one JSON line out.  The
    `report-gone` op names no one chat:  it takes `--gone` (a JSON list of session ids) and no `--session-id`.

    One call to the one assigner, the registry API (`registry_api`), and nothing else:  no local file is opened and
    none is created.  The exit status is 0 for an answer, 2 for bad arguments, 4 for a refused request and 5 for an
    answer that carries an error, among them a host with no API configured (`not_configured`) and a registry that
    did not answer."""
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
    if args.op != "report-gone" and not args.session_id:
        return _emit(stdout, {"error": "bad arguments"}, 2)
    try:
        if args.op == "report-gone":
            out = _report_gone_api(env, args)
        else:
            sid = SI.check(args.session_id, "the session id")
            if args.op == "register":
                out = _register_api(OR, env, args, sid)
            else:
                out = _on_record_api(OR, env, args, sid)
    except ValueError as exc:
        return _emit(stdout, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}, 4)
    except (KeyError, TypeError):
        # A registry answer too short to fold (a key the orchestration reads is not there) is a failed call,
        # answered the same way:  an error, never a traceback.
        return _emit(stdout, {"error": "the registry answer was incomplete"}, 5)
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


def _sync_folder(folder: str) -> None:
    """Flush a folder to disk after a file was renamed into it, so the rename itself survives a crash:  the file's
    own fsync covers its bytes, not the folder entry that names it."""
    dir_fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Chat:
    """One chat, as the hook sees it: its spec, location and session id, its note, and the way to its registry.

    `transport` replaces the registry call with a callable that takes the registry side's argv and answers with its
    JSON (a test's fake registry); without one the registry side runs in process and makes its one API call.  An
    empty `where` is a host that did not state its fleet location:  every call answers an error, so the chat is a
    "??" chat until its host says where it is."""

    def __init__(
        self,
        spec: PlatformSpec,
        env: Mapping[str, str],
        where: str,
        sid: str,
        *,
        now: float,
        transport: Transport | None = None,
        seat: str = "",
    ):
        self.spec, self.env, self.location, self.session_id = spec, env, where, sid
        self.now, self.transport = now, transport
        self.seat = seat or spec.seat_for({}, env)
        self.path = note_path(env, self.seat, sid)

    # ---- the local note --------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """The chat's note, or {}.  A note the seat's retired client wrote is put in this client's shape where it
        lies, the first time it is read (`upgraded_note`)."""
        note = _read_json(self.path)
        fresh = upgraded_note(note, self.spec.platform)
        if fresh is None:
            return note
        # As it was, but for its shape:  `updated_at` stays the old client's.
        _save_json(self.path, fresh)
        return fresh

    def write(self, note: Mapping[str, Any]) -> bool:
        """Save the note; False when it cannot be saved (a state folder that is not writable, or one whose rename
        could not be made durable).  The hook goes on without it, so the letter is still shown, and the next event
        only resumes the registration."""
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
            tmp = ""  # renamed into place:  nothing is left to clean up
            _sync_folder(folder)
        except (OSError, TypeError, ValueError):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
        return True

    # ---- the registry ----------------------------------------------------------------------

    def argv(self, op: str, title: str = "", parent: str = "", issues: str = "") -> list[str]:
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
        if issues and op == "issues":
            argv.append("--issues=" + issues)
        return argv

    def registry(
        self, op: str, title: str = "", parent: str = "", issues: str = ""
    ) -> dict[str, Any]:
        """One call to the registry side, answered as its JSON.  Whatever goes wrong, on the way or in the registry
        side, is an error answer: a hook never blocks a chat, and never stops on a call that failed."""
        try:
            return self._registry(op, title, parent, issues)
        except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
            return {"error": "the registry call failed (%s)" % type(exc).__name__}

    def _registry(self, op: str, title: str, parent: str, issues: str = "") -> dict[str, Any]:
        if not self.location:
            return {"error": NO_LOCATION}
        return _registry_side(self.argv(op, title, parent, issues), self.env, self.transport)


def _registry_side(
    argv: list[str], env: Mapping[str, str], transport: Transport | None
) -> dict[str, Any]:
    """The registry side's answer to one command line:  through the test's transport when there is one, else in
    process, where it makes its one API call."""
    if transport is not None:
        return _answer(dict(transport(argv)))
    out = io.StringIO()
    registry_main(argv, env=env, stdout=out)
    return _answer(_last_json(out.getvalue()))


class Out:
    """What the hook prints, in the spec's output style.  `hook` is Claude Code's hookSpecificOutput (context and a
    sessionTitle), `text` is the context alone as plain lines, `json` is one object for a plugin to read."""

    def __init__(self, stream: TextIO, spec: PlatformSpec, hook_event: str):
        self.stream, self.spec, self.hook_event = stream, spec, hook_event

    def emit(
        self, event: str, *, context: str = "", title: str | None = None, label: str = ""
    ) -> None:
        if self.hook_event and self.hook_event in self.spec.quiet_events:
            return  # the platform takes no output from this event
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


def untitled_context(
    body: Mapping[str, Any], env: Mapping[str, str], seat: str, sid: str, display: str, product: str
) -> str:
    """The context line for a platform whose hooks cannot set a chat's title (such as Cursor), from one `json` style
    answer (`{"context", "label"}`):  the chat is told its letter and how it is addressed, and that its title does not
    carry the letter.  Any other line (no letter yet, another seat's) is the library's own, as it is."""
    context = _text(
        body.get("context")
    )  # not `tidy`:  the library's sentence gaps stay as they are
    label = _text(body.get("label"))
    head, mark, _ = context.partition("Operator session letter:")
    if not (label and mark):
        return context
    try:
        with open(note_path(env, seat, sid), encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        note = {}
    note = note if isinstance(note, dict) else {}
    gen = note.get("generation") or 1
    where = "server" if note.get("location") == "server" else "Mac"
    ref = "%s %s%s" % (
        display,
        label,
        " (gen %d)" % gen if isinstance(gen, int) and gen > 1 else "",
    )
    # Rendered copy:  a sentence gap is U+00A0 plus a space.
    return (
        '%sOperator session letter: %s, on the %s.  This chat is addressed as "@**%s** %s".  %s gives a '
        "hook no way to set a chat's title, so the title does not carry the letter; a name the owner gives this chat "
        'should start with "%s: ".' % (head, ref, where, display, label, product, label)
    )


def held(note: Mapping[str, Any], *extra: object) -> tuple[str, ...]:
    """The letters this chat holds or held: the one in its note, the ones it held before, and any `extra`."""
    seen: list[str] = []
    for label in (*extra, note.get("label"), *(note.get("labels") or ())):
        if isinstance(label, str) and LABEL_RE.fullmatch(label) and label not in seen:
            seen.append(label)
    return tuple(seen)


def refused(answer: Mapping[str, Any]) -> bool:
    """The assigner answered, and the answer is no letter:  every pair is live, or it holds no registration for the
    chat.  Anything else that carries an error is a call that got no answer (not configured, unreachable, a timeout)."""
    error = str(answer.get("error") or "")
    return error == NO_REGISTRATION or (error.startswith("all ") and error.endswith(REFUSED_TAIL))


def _apply(chat: Chat, answer: Mapping[str, Any], note: dict[str, Any]) -> dict[str, Any]:
    """Fold one register answer into the note: open, failed (no letter, for any reason), or held by another seat.  A
    failed note holds no letter (`label` is ""), and its `failed_at` is the stamp that throttles asking again.

    A chat that holds a letter keeps it when the call merely got no answer (an Eavesdropper redeploy, a timeout):  only
    a refusal takes the letter off.  Its `sync_failed_at` stamp holds the next call back for RETRY_SECONDS."""
    if ("error" in answer or not answer.get("outcome")) and lettered(note) and not refused(answer):
        return dict(note, location=chat.location, sync_failed_at=chat.now)
    if "error" in answer or not answer.get("outcome"):
        return dict(
            note,
            state="failed",
            label="",
            location=chat.location,
            failed_at=chat.now,
            error=str(answer.get("error") or "no answer")[:200],
        )
    if answer["outcome"] == "seat_mismatch":
        return dict(note, state="mismatch", label="")
    if not answer.get("label"):
        return dict(
            note,
            state="failed",
            label="",
            location=chat.location,
            failed_at=chat.now,
            error="the Operator issued no letter",
        )
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
    out.pop("gone_at", None)  # a chat reported gone that registers again is not gone
    return out


def lettered(note: Mapping[str, Any]) -> bool:
    """The chat holds a letter:  its note is open and names one."""
    return note.get("state") == "open" and bool(note.get("label"))


def no_letter(spec: PlatformSpec, note: Mapping[str, Any]) -> str:
    """The context line for a chat that holds no letter, worded by whether the hook can title the chat."""
    mark = (
        'its title starts with "??: "'
        if spec.titles
        else '%s gives a hook no way to set a title, so a name given to this chat should start with "??: "'
        % spec.display
    )
    if note.get("state") == "mismatch":
        return "This chat is registered under another seat, so it takes no letter here and its title is left as it is."
    return (
        "No Operator letter yet (%s), so %s.  A later hook run asks again, at most every ten minutes, and "
        "`simple-chat letter request` asks now." % (note.get("error") or "no answer", mark)
    )


def _join(*parts: str) -> str:
    """Context lines as one context, a sentence gap between them."""
    return "  ".join(part for part in parts if part)


def advice(
    chat: Chat, note: dict[str, Any], current: str, data: Mapping[str, Any], reason: str
) -> str:
    """The line the adapter gives the chat's own model about a title the platform will not take from this hook (the
    spec's `title_advice`), or "".

    The adapter is asked on a start and on a prompt, about the title the hook wants on the chat now, because only it
    can tell which chats its platform keeps a title of its own for.  The same line is not given again for
    RETRY_SECONDS (the note's `title_fix_for` and `title_fix_at`), so a start and its first prompt ask once, and a
    chat that never does as it is told is not asked on every prompt;  a line that differs (the letter changed, the
    chat was renamed) goes at once.  An adapter that raises has no advice:  a hook never blocks a chat."""
    advise = chat.spec.title_advice
    if advise is None or note.get("state") not in ("open", "failed"):
        return ""
    label = str(note.get("label") or "") if lettered(note) else ""
    mark = label or UNLETTERED
    want = str(note.get("want") or "")
    title = want if want.startswith(mark + ": ") else compose(mark, str(note.get("title") or ""))
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
        shown=str(note.get("shown") or ""),
        held=held(note),
    )
    try:
        text = advise(request)
    except Exception:  # noqa: BLE001 - an adapter's advice never blocks a chat
        return ""
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return ""
    try:
        asked_at = float(note.get("title_fix_at") or 0)
    except (TypeError, ValueError):
        asked_at = 0.0
    if note.get("title_fix_for") == text and chat.now - asked_at < RETRY_SECONDS:
        return ""
    note.update(title_fix_for=text, title_fix_at=chat.now)
    return text


def unlettered(
    chat: Chat,
    note: dict[str, Any],
    words: str,
    current: str,
    data: Mapping[str, Any],
    reason: str,
    *,
    settable: bool = True,
) -> str | None:
    """Title a chat that holds no letter "??: <words>" (owner, Nov 11, 2026), the way `present` titles any chat, and
    keep the words in the note so the letter that comes later goes on the same words.  `words` never carries a
    letter or the mark:  it is `bare` already, so the mark is never doubled."""
    words = "" if words == PLACEHOLDER else words
    if words:
        note["title"] = words
    note["titled"] = bool(words)
    note["placeholder"] = compose(UNLETTERED, "")
    title = compose(UNLETTERED, words)
    if note.get("want") == title and note.get("pending"):
        _retry_pending(
            chat, note, current, data
        )  # a writer that failed is asked again on its own clock
        return None
    if note.get("shown") == title and current in ("", title, note.get("seen")):
        # Already on the chat, or already tried over the name the platform still shows (one it keeps over the
        # hook's title):  a steady "??" chat costs no rename on every prompt.
        return None
    shown = present(chat, note, title, current, data, reason, settable=settable)
    if current and current != title and note.get("shown") == title:
        # One try for each name the platform shows, never one for every prompt, as `_renamed` keeps it for a chat
        # that holds a letter.
        note["seen"] = current
    return shown


def ask_again(
    chat: Chat, note: dict[str, Any], words: str, parent: str, *, force: bool = False
) -> dict[str, Any]:
    """Ask the assigner for a letter for a chat that holds none, unless it asked less than RETRY_SECONDS ago (its
    `failed_at` stamp) and `force` is off.  Answers the note as it is now:  open when a letter came, else failed (with
    a fresh stamp) or as it was (not due).  An open answer is news to the model (`announce`), and the words go under
    the new letter (`titled` is false, so the next title the hook sets is "<letter>: <words>")."""
    if not force and chat.now - float(note.get("failed_at") or 0) < RETRY_SECONDS:
        return note
    # Stamped before the chain:  a hook killed inside it (a hung assigner) does not repeat it on the next prompt.
    chat.write(dict(note, failed_at=chat.now))
    fresh = _apply(chat, _enroll(chat, words, parent), dict(note, parent=parent))
    if lettered(fresh):
        fresh.update(announce=True, placeholder=compose(str(fresh["label"]), ""))
        if words:
            fresh["title"] = words
    return fresh


def _reissued(spec: PlatformSpec, answer: Mapping[str, Any]) -> str:
    """The context line for a reopened chat whose letter changed, or ""."""
    previous, label = str(answer.get("previous") or ""), str(answer.get("label") or "")
    if answer.get("outcome") == "reopened" and previous and previous != label:
        return (
            "This chat's earlier letter %s went to another chat while this one was closed, so the Operator "
            "issued %s.  " % (previous, label)
        )
    return ""


def follow_letter(note: dict[str, Any], answer: Mapping[str, Any]) -> str:
    """Fold the letter a touch or title answer names into an open chat's note.  Returns the letter the chat held
    before when the registry's is a different one, else "".

    The registry can change a live chat's letter in place (a relabel, or a restore that moves records between
    letters), and the touch and title answers are where the hook hears of it.  A different letter is the caller's
    cue to retitle the chat and tell the model.  The same letter under another generation is folded into the note
    silently, because the title carries no generation and a chat registered by hand can differ from its note by
    generation alone.  An answer with no letter (an error, a retired letter, or a registry from before the touch
    named one) changes nothing.  The letter is followed as given: the registry is the allocator, and a restore can
    hand a chat a letter another chat's note still holds, so nothing here second-guesses it."""
    if "error" in answer or answer.get("retired") or note.get("state") != "open":
        return ""
    label = answer.get("label")
    held_now = str(note.get("label") or "")
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label) or not held_now:
        return ""
    gen = answer.get("generation")
    valid = isinstance(gen, int) and not isinstance(gen, bool) and gen >= 1
    if valid:
        note["generation"] = gen
    rid = answer.get("record_id")
    if isinstance(rid, str) and rid:
        note["record_id"] = rid
    if label == held_now:
        return ""
    note["label"] = label
    if not valid:
        # The old letter's generation says nothing about the new one: it is the first, as `_apply` reads a register
        # answer that names none.
        note["generation"] = 1
    return held_now


def _follow_title(note: dict[str, Any], prior: str, shown: str) -> str:
    """The title for a chat whose letter just moved from `prior` to the note's: the words of `shown` with every letter
    the chat held taken off (the note's title when that leaves none), under the new letter.  The old letter stays
    in the note's history, so a later prompt still reads a title that carries it as this chat's own."""
    label = str(note["label"])
    note["labels"] = list(held(note, label, prior))[:8]
    note["placeholder"] = compose(label, "")
    words = bare(shown, held(note))
    if not words or words == PLACEHOLDER:
        words = str(note.get("title") or "")
    return compose(label, "" if words == PLACEHOLDER else words)


def relabeled(prior: str, label: str) -> str:
    """The context line for a chat whose letter the registry changed in place."""
    return "This chat's Operator letter changed from %s to %s; it is addressed as %s now.  " % (
        prior,
        label,
        label,
    )


# ---- the Linear issues a chat works on (AFC-694) ----------------------------------------
#
# At the start of each turn the hook looks at what the turn before touched (the transcript lines
# since its last look, the git branch of the chat's folder, and the Zulip topics it posted to),
# folds the issue ids it finds into the note, and tells the registry when the set of ids changed.
# The registry keeps the list for the wake router (operator_registry.normalize_issues has the rule).
# Nothing here may block or break a prompt: every read is bounded and every failure is silent.

#: The most transcript one turn reads: only its newest lines, so a huge transcript never slows a prompt.
ISSUE_SCAN_BYTES = 256 * 1024
ISSUE_GIT_TIMEOUT = 2
# The topic of a `simple-chat post --topic "..."` command, and the line `simple-chat post` prints.
_TOPIC_FLAG = re.compile(r"--topic(?:=|\s+)(?:\"([^\"\n]*)\"|'([^'\n]*)'|(\S+))")
_POSTED_TOPIC = re.compile("posted id [0-9]+ to #[^\\n]*? › ([^\\n]+)")
# The parts of a transcript line that are a tool's output, not the chat's own words: a file the turn
# read can name dozens of issues it never worked on, so only the topic rules read those parts.
_QUOTED_TYPES = frozenset({"tool_result", "function_call_output", "custom_tool_call_output"})
_QUOTED_KEYS = frozenset({"toolUseResult", "output", "stdout", "stderr", "result"})
_issue_finders: dict[bool, Any] = {}


def _issue_finder(any_case: bool) -> Any:
    """The rule that finds an issue id in text.  Text writes an id the way Linear does, in capitals;
    a branch name is lower case by convention ("claude/afc-694-slug"), so it is read in either case."""
    if any_case not in _issue_finders:
        from . import operator_registry as OR

        _issue_finders[any_case] = re.compile(
            r"(?<![A-Za-z0-9])((?:%s)-[0-9]{1,7})(?![A-Za-z0-9])" % OR.ISSUE_KEYS_PATTERN,
            re.IGNORECASE if any_case else 0,
        )
    return _issue_finders[any_case]


def issue_ids_in(text: str, *, any_case: bool = False) -> set[str]:
    from . import operator_registry as OR

    return {OR.valid_issue_id(m.group(1)) for m in _issue_finder(any_case).finditer(text)} - {""}


def _topic_issue_ids(text: str) -> set[str]:
    found: set[str] = set()
    for m in _TOPIC_FLAG.finditer(text):
        found |= issue_ids_in(next(g for g in m.groups() if g is not None))
    for m in _POSTED_TOPIC.finditer(text):
        found |= issue_ids_in(m.group(1))
    return found


def _transcript_strings(
    value: Any, own: list[str], quoted: list[str], inside: bool, depth: int = 0
) -> None:
    if depth > 40:
        return
    if isinstance(value, str):
        (quoted if inside else own).append(value)
    elif isinstance(value, Mapping):
        kind = value.get("type")
        hidden = (
            inside
            or (isinstance(kind, str) and kind in _QUOTED_TYPES)
            or value.get("isMeta") is True
        )
        for key, item in value.items():
            _transcript_strings(item, own, quoted, hidden or key in _QUOTED_KEYS, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _transcript_strings(item, own, quoted, inside, depth + 1)


_NOT_JSON = object()


def parsed_lines(lines: list[str]) -> list[tuple[str, Any]]:
    """Each transcript line with its JSON (or _NOT_JSON), parsed once for every reader of the same lines."""
    out: list[tuple[str, Any]] = []
    for line in lines:
        try:
            out.append((line, json.loads(line)))
        except ValueError:
            out.append((line, _NOT_JSON))
    return out


def issue_ids_in_lines(lines: list[str], parsed: list[tuple[str, Any]] | None = None) -> set[str]:
    """The issue ids a stretch of transcript names: any id in the chat's own words (its prompts, its
    replies, the commands and tool inputs it wrote), and the topic of any Zulip post, even one a tool's
    output shows.  A line that is not JSON is read as the chat's own words."""
    found: set[str] = set()
    for line, entry in parsed_lines(lines) if parsed is None else parsed:
        if entry is _NOT_JSON:
            found |= issue_ids_in(line) | _topic_issue_ids(line)
            continue
        own: list[str] = []
        quoted: list[str] = []
        _transcript_strings(entry, own, quoted, False)
        for text in own:
            found |= issue_ids_in(text) | _topic_issue_ids(text)
        for text in quoted:
            found |= _topic_issue_ids(text)
    return found


def read_new_lines(path: object, root: str, scan: object) -> tuple[list[str], dict[str, Any]]:
    """The complete lines a transcript gained since `scan` (the {"path", "offset"} the last call
    returned), and the scan to keep for the next call.

    Only a regular file under `root` is read, so a stray path in hook data cannot point anywhere else.
    At most ISSUE_SCAN_BYTES are read: a first scan, and a transcript that shrank or grew by more than
    that, take only its tail, from the first whole line in it.  A line still being written waits for
    the next turn.  Raises OSError or ValueError for anything that cannot be read."""
    if not isinstance(path, str) or not path:
        raise ValueError("no transcript path")
    base = os.path.realpath(root) + os.sep
    real = os.path.realpath(path)
    if not real.startswith(base) or not os.path.isfile(real):
        raise ValueError("the transcript is not under the platform's own folder")
    size = os.path.getsize(real)
    last = scan if isinstance(scan, Mapping) else {}
    offset = last.get("offset") if last.get("path") == real else None
    known = isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= size
    start = offset if known else max(0, size - ISSUE_SCAN_BYTES)
    if size - start > ISSUE_SCAN_BYTES:
        start, known = size - ISSUE_SCAN_BYTES, False
    with open(real, "rb") as fh:
        fh.seek(start)
        blob = fh.read(size - start)
    end = blob.rfind(b"\n")
    if end < 0:
        return [], {"path": real, "offset": start}
    lines = blob[: end + 1].decode("utf-8", errors="replace").splitlines()
    if start > 0 and not known:
        lines = lines[1:]  # the cut landed inside a line, or at the start of one it cannot tell
    return lines, {"path": real, "offset": start + end + 1}


def branch_issue_ids(cwd: object, run: Callable[..., Any] = subprocess.run) -> set[str]:
    """The issue ids in the git branch of `cwd` ("claude/afc-694-slug" names AFC-694), or none.  Only a
    folder that is a git tree answers, and git gets ISSUE_GIT_TIMEOUT seconds and no prompt."""
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return set()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        proc = run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=ISSUE_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    if getattr(proc, "returncode", 1) != 0:
        return set()
    branch = str(getattr(proc, "stdout", "") or "").strip()
    return issue_ids_in(branch, any_case=True) if branch and branch != "HEAD" else set()


#: The most Zulip topics a note keeps for its chat (the newest), and the longest topic it keeps (Zulip's own limit).
TOPIC_KEEP = 20
TOPIC_CHARS = 60
# The hosted Zulip tools a chat posts with (`zulip_post`, `zulip_reply`, under any MCP server prefix).
_POST_TOOLS = re.compile(r"(?:^|__)zulip_(?:post|reply)$")
# The programs and verbs that post, the flags they take before the verb with a value, and the line `simple-chat post`
# prints for each post it made (cli.py `cmd_post`), read only from the output of that same command.
_POST_PROGRAMS = frozenset({"simple-chat", "agent-sync"})
_POST_VERBS = frozenset({"post", "reply"})
_GLOBAL_VALUE_FLAGS = frozenset({"--as", "--default-seat", "--rc", "--session"})
_POSTED_LINE = re.compile("^posted id [0-9]+ to #[^\\n]*? › ([^\\n]+)$", re.MULTILINE)
_SHELL_PUNCTUATION = ";&|()\n"
# The words that may stand before the program in the command that runs it:  a wrapper (and its flags and their
# values) or a shell keyword.  `echo simple-chat post ...` only names the program, so it is not a post.
_PREFIX_WORDS = frozenset(
    {
        "sudo",
        "env",
        "command",
        "nohup",
        "time",
        "exec",
        "timeout",
        "nice",
        "ionice",
        "stdbuf",
        "then",
        "do",
        "else",
        "if",
        "while",
        "!",
    }
)
_ASSIGNMENT = re.compile("^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile("^[0-9]+(?:[.][0-9]+)?[smhd]?$")  # `timeout 10 simple-chat ...`


def _clean_topic(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:TOPIC_CHARS].strip()


def _runs_program(before: list[str]) -> bool:
    """True when the words before a program name only wrap it (`sudo`, `env A=b`, a shell keyword), so it is run."""
    for i, word in enumerate(before):
        if (
            os.path.basename(word) in _PREFIX_WORDS
            or _ASSIGNMENT.match(word)
            or word.startswith("-")
            or _DURATION.match(word)
        ):
            continue
        if i > 0 and before[i - 1].startswith("-") and "=" not in before[i - 1]:
            continue  # the value of a wrapper's flag, such as `sudo -u jay`
        return False
    return True


def _shell_tokens(command: str, posix: bool) -> list[str]:
    lexer = shlex.shlex(command, posix=posix, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"  # a newline ends a command, as `;` does
    lexer.whitespace_split = True
    return list(lexer)


def _command_posts(command: object) -> list[str] | None:
    """The literal `--topic` of each `simple-chat` (or `agent-sync`) `post` or `reply` in one shell command, or None
    when the command posts nothing.  A topic that is not literal (a `$VAR`) is kept as "" so the printed line can
    name it.  `read --topic`, another program's `--topic` and a command that does not parse are not posts."""
    if not isinstance(command, str):
        return None
    try:
        tokens = _shell_tokens(command, True)
        raw_tokens = _shell_tokens(command, False)  # the same words with their quotes kept
    except ValueError:
        return None
    if len(raw_tokens) != len(tokens):
        raw_tokens = [""] * len(
            tokens
        )  # no quoting to go by:  a `$` topic then needs the printed line
    segments: list[list[str]] = [[]]
    raw_segments: list[list[str]] = [[]]
    for token, raw in zip(tokens, raw_tokens):
        if token and all(ch in _SHELL_PUNCTUATION for ch in token):
            segments.append([])
            raw_segments.append([])
        else:
            segments[-1].append(token)
            raw_segments[-1].append(raw)
    posts: list[str] = []
    for words, raws in zip(segments, raw_segments):
        for at, word in enumerate(words):
            if os.path.basename(word) not in _POST_PROGRAMS or not _runs_program(words[:at]):
                continue
            rest = words[at + 1 :]
            i = 0
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in _GLOBAL_VALUE_FLAGS else 1
            if i >= len(rest) or rest[i] not in _POST_VERBS:
                break
            topic = ""
            args = rest[i + 1 :]
            raw_args = raws[at + 1 :][i + 1 :]
            raw_topic = ""
            for j, arg in enumerate(args):
                if arg == "--topic" and j + 1 < len(args):
                    topic, raw_topic = args[j + 1], raw_args[j + 1]
                elif arg.startswith("--topic="):
                    topic, raw_topic = arg[len("--topic=") :], raw_args[j][len("--topic=") :]
            # A shell variable or a substitution is not the topic the command posted to:  only the printed line says
            # that.  (A hosted call's topic and a printed line's never went through a shell, so `$` is just text.)
            # A topic in single quotes is literal, so it is kept:  judged from this argument's own quoting.
            expands = any(mark in topic for mark in ("$", "`")) and raw_topic != f"'{topic}'"
            posts.append("" if expands else _clean_topic(topic))
            break
    return posts or None


def _result_text(value: Any, depth: int = 0) -> str:
    """The text of a tool result's content (a string, or a list of text parts)."""
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_result_text(item, depth + 1) for item in value)
    if isinstance(value, Mapping):
        return _result_text(
            value.get("text") if "text" in value else value.get("content"), depth + 1
        )
    return ""


def _walk_topics(
    value: Any, pending: dict[str, tuple[str, list[str]]], found: list[str], depth: int = 0
) -> None:
    """One transcript entry's part in the topic count:  a post call (a Bash `simple-chat post` or `reply`, or a hosted
    `zulip_post` or `zulip_reply`) waits in `pending` under its tool_use id, and counts only when its own tool_result
    comes back without an error."""
    if depth > 40:
        return
    if isinstance(value, list):
        for item in value:
            _walk_topics(item, pending, found, depth + 1)
        return
    if not isinstance(value, Mapping):
        return
    kind = value.get("type")
    if kind == "tool_use":
        call_id, name, args = value.get("id"), value.get("name"), value.get("input")
        if not (
            isinstance(call_id, str)
            and call_id
            and isinstance(name, str)
            and isinstance(args, Mapping)
        ):
            return
        if _POST_TOOLS.search(name):
            pending[call_id] = ("hosted", [_clean_topic(args.get("topic"))])
        else:
            posts = _command_posts(args.get("command"))
            if posts is not None:
                pending[call_id] = ("command", posts)
        return
    if kind == "tool_result":
        call = (
            pending.pop(value.get("tool_use_id"), None)
            if isinstance(value.get("tool_use_id"), str)
            else None
        )
        if call is None or value.get("is_error") is True:
            return
        how, topics = call
        if how == "command":
            printed = [
                m.group(1) for m in _POSTED_LINE.finditer(_result_text(value.get("content")))
            ]
            topics = [_clean_topic(t) for t in printed] if printed else topics
        found.extend(topics)
        return
    for item in value.values():
        if isinstance(item, (list, Mapping)):
            _walk_topics(item, pending, found, depth + 1)


def topics_in_lines(lines: list[str], parsed: list[tuple[str, Any]] | None = None) -> list[str]:
    """The Zulip topics a stretch of a Claude Code transcript posted to, in the order the posts came back, each once:
    a Bash `simple-chat post` or `reply` command the chat ran (the topic its printed `posted id N to #channel › topic`
    line names, else its literal `--topic`), and a hosted `zulip_post` or `zulip_reply` call.  Either counts only when
    its own tool result comes back without an error.  Nothing else is a post:  not a `--topic` in a prompt, a reply or
    another command (`simple-chat read --topic`), and not a `posted id` line in some other tool's output (a log the
    turn read).  A call whose result is not in these lines is not counted.  Other platforms' transcripts give none."""
    found: list[str] = []
    pending: dict[str, tuple[str, list[str]]] = {}
    for _, entry in parsed_lines(lines) if parsed is None else parsed:
        if entry is not _NOT_JSON:
            _walk_topics(entry, pending, found)
    seen: set[str] = set()
    out: list[str] = []
    for topic in found:
        if topic and topic not in seen:
            seen.add(topic)
            out.append(topic)
    return out


def merge_topics(old: object, seen: list[str], now: float) -> list[dict[str, Any]]:
    """The note's `topics` list with `seen` folded in:  each topic once, stamped with the last time a turn posted to
    it, newest last, at most TOPIC_KEEP."""
    kept: dict[str, float] = {}
    for item in old if isinstance(old, list) else []:
        if isinstance(item, Mapping):
            topic = _clean_topic(item.get("topic"))
            at = item.get("at")
            if topic and isinstance(at, (int, float)) and not isinstance(at, bool):
                kept.pop(topic, None)
                kept[topic] = float(at)
    for topic in (_clean_topic(item) for item in seen):
        if topic:
            kept.pop(topic, None)
            kept[topic] = now
    ordered = sorted(kept.items(), key=lambda kv: kv[1])[-TOPIC_KEEP:]
    return [{"topic": topic, "at": at} for topic, at in ordered]


def collect_issue_ids(
    note: dict[str, Any],
    data: Mapping[str, Any],
    root: str,
    run: Callable[..., Any] = subprocess.run,
    now: float | None = None,
) -> set[str]:
    """The ids this turn finds, from the hook data's `transcript_path` (only the lines since the scan
    point the note keeps) and `cwd` (its branch).  A source that fails gives nothing.  Given `now`, the
    Zulip topics those lines posted to are folded into the note's `topics` as well (AFC-702)."""
    found: set[str] = set()
    path = data.get("transcript_path")
    if isinstance(path, str) and path:
        try:
            lines, note["issues_scan"] = read_new_lines(path, root, note.get("issues_scan"))
            parsed = parsed_lines(lines)  # one parse for both readers
            found |= issue_ids_in_lines(lines, parsed)
            if now is not None:
                topics = topics_in_lines(lines, parsed)
                if topics:
                    note["topics"] = merge_topics(note.get("topics"), topics, now)
        except (OSError, ValueError, TypeError):
            pass
    try:
        found |= branch_issue_ids(data.get("cwd"), run)
    except Exception:  # noqa: BLE001 - a hook never stops for this
        pass
    return found


def sync_issues(
    note: dict[str, Any],
    data: Mapping[str, Any],
    root: str,
    now: float,
    send: Callable[[str], Mapping[str, Any]],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Fold this turn's ids into the note's `issues`, and tell the registry when the set of ids changed.

    `send` takes the whole list as JSON (the registry's update replaces what it holds) and answers with
    the registry's answer.  It is called only when the set of ids differs from the one the registry
    last took (`issues_sent`), and not again for RETRY_SECONDS after a call that failed, so a registry
    that is down, or too old to know the field, costs one call per wait.  A hook with neither a
    transcript nor a folder in its data finds nothing and sends nothing.  Nothing here raises."""
    try:
        from . import operator_registry as OR

        seen = collect_issue_ids(note, data, root, run, now)
        old = note.get("issues")
        issues = OR.merge_issues(old, seen, now) if seen else OR.normalize_issues(old or [])
        if issues:
            note["issues"] = issues
        wanted = sorted(OR.issue_ids(issues))
        if not wanted or wanted == sorted(note.get("issues_sent") or []):
            return
        if now - float(note.get("issues_failed_at") or 0) < RETRY_SECONDS:
            return
        answer = send(json.dumps(issues, separators=(",", ":")))
        if not isinstance(answer, Mapping) or "error" in answer or answer.get("retired"):
            note["issues_failed_at"] = now
            return
        taken = answer.get("issues")
        note["issues_sent"] = sorted(OR.issue_ids(taken)) if isinstance(taken, list) else wanted
        note.pop("issues_failed_at", None)
    except Exception:  # noqa: BLE001 - a hook never stops for this
        return


def end_of_turn(
    note: dict[str, Any],
    data: Mapping[str, Any],
    root: str,
    now: float,
    send: Callable[[str], Mapping[str, Any]],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """The end-of-turn report a platform's turn-end hook calls (AFC-702):  the Linear issues and Zulip topics the turn
    that just ended touched, so the last turn of a chat is recorded without waiting for a next prompt.

    It is the same scan the prompt hook runs (`sync_issues`), from the same scan point, so a turn is read once:  the
    prompt hook stays the fallback for a turn whose end hook never ran (a platform with no turn-end event, a hook that
    timed out), and finds nothing new after one that did.  The issues reach the registry;  the topics stay in the
    chat's own note until the registry has a field for them.  Only an open chat with a letter reports.  Nothing here
    raises, and a registry that does not answer costs at most one call per RETRY_SECONDS."""
    if note.get("state") != "open" or not note.get("label"):
        return
    sync_issues(note, data, root, now, send, run=run)


#: The note keys the end-of-turn report owns.  Only these go back into the note after the registry call.
TURN_KEYS = ("issues", "issues_scan", "issues_sent", "issues_failed_at", "topics")


def turn_note(
    before: Mapping[str, Any], after: Mapping[str, Any], fresh: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The note to save after an end-of-turn report:  the note as it is on disk now (`fresh`, read again after the
    registry call, which can take seconds), with only TURN_KEYS taken from the report's copy (`after`;  a key the
    report removed from `before` is removed).  So a SessionEnd or a prompt hook that wrote the note meanwhile (a
    close, a new title) is never undone by a stale copy.  None when the note is gone:  nothing is saved."""
    if not fresh:
        return None
    out = dict(fresh)
    for key in TURN_KEYS:
        if key in after:
            out[key] = after[key]
        elif key in before:
            out.pop(key, None)
    return out


def on_turn(chat: Chat, data: Mapping[str, Any]) -> int:
    """A turn ended (a spec that maps its turn-end event to `turn`):  ask again for a missing letter, report the turn,
    print nothing, close nothing.

    A letter that comes here goes on the title through the adapter's writer when it has one;  a platform that takes
    titles only from hook output gets it at the next prompt, which reads `titled` false and `announce` true.  For a
    lettered chat on a platform whose writer renames natively, the turn end also does the rename part (a platform
    that retitled the chat itself, dropping the letter, gets it back)."""
    note = chat.read()
    if not note:
        return 0
    if lettered(note) and chat.spec.title_writer is not None:
        on_rename(chat, data, Out(io.StringIO(), chat.spec, ""))
        note = chat.read()
    elif not lettered(note) and note.get("state") not in ("mismatch", "closed"):
        parent = parent_of(chat, note, data)
        note = ask_again(chat, note, str(note.get("title") or ""), parent)
        if lettered(note) and note.get("announce"):
            current = tidy(chat.spec.current_title(data, chat.env))
            title = compose(str(note["label"]), str(note.get("title") or ""))
            present(chat, note, title, current, data, "retry", settable=False)
            note["titled"] = note.get("shown") == title
        chat.write(note)
    before = copy.deepcopy(note)
    end_of_turn(
        note,
        data,
        _text(chat.env.get("HOME")) or os.path.expanduser("~"),
        chat.now,
        lambda text: chat.registry("issues", issues=text),
    )
    merged = turn_note(before, note, chat.read())
    if merged is not None:
        chat.write(merged)
    return 0


def _issues_answer(out: Mapping[str, Any]) -> dict[str, Any]:
    """The registry side's answer to an `issues` call, from the registry's update answer."""
    if out.get("outcome") == "closed":
        return {"retired": True, "record_id": out.get("record_id", "")}
    return {
        "ok": True,
        "record_id": out.get("record_id", ""),
        "issues": list(out.get("issues") or []),
    }


def _issues_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text  # refused by the registry as any value that is not a list is


def record_issues_api(
    env: Mapping[str, str], identity: Mapping[str, Any], text: str
) -> dict[str, Any]:
    """The `issues` op through the registry API: its update, with the list it carries."""
    out = _call_api(env, "update", {**identity, "issues": _issues_value(text)})
    error = _api_error(out)
    if error is not None:
        return error
    return _issues_answer(out)


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
        shown=str(note.get("shown") or ""),
        held=held(note),
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


def parent_of(chat: Chat, note: dict[str, Any], data: Mapping[str, Any]) -> str:
    """The session id of the chat that started this one, or "".

    A parent the note knows is not asked for again:  a chat's parent never changes.  A lookup that found none is
    not repeated for RETRY_SECONDS (the note's `parent_asked_at`), because an adapter's lookup can cost a call to
    another host, and a chat with no letter would otherwise make it on every prompt.  An adapter that raises has
    found no parent."""
    known = str(note.get("parent") or "")
    asked = note.get("parent_asked_at")
    if known or (asked is not None and 0 <= chat.now - _seconds(asked) < RETRY_SECONDS):
        return known
    note["parent_asked_at"] = chat.now
    try:
        found = _text(chat.spec.parent_id(data, chat.env))
    except Exception:  # noqa: BLE001 - a parent is a hint the chat was never owed
        return ""
    if found:
        # Kept at once:  the registration that uses it may wait for its own window.
        note["parent"] = found
    return found


def _settled(note: Mapping[str, Any]) -> bool:
    """The note needs no registration made for it: the chat holds a letter, or another seat holds the chat."""
    return note.get("state") == "mismatch" or (
        note.get("state") == "open" and bool(note.get("label"))
    )


def _ensure_parent(chat: Chat, parent: str, depth: int = 0) -> None:
    """Register a sub-thread's parent before the sub-thread, when the parent holds no live registration.

    The registry gives a sub-thread the pair after its parent's only when it knows the parent; a parent it has never
    seen (its own registration failed, or was swept) counts as no parent, and the sub-thread would take a main pair
    ahead of it.  The parent's note says what the hook last got: open is left alone and costs no hop, anything else
    (no note, failed, closed) is registered now, with the parent's own parent first.  The parent's title and the
    model's reminder wait for its next prompt (`titled` is false and `announce` true).  A spec without `chat_payload`
    does nothing here, and nothing here ever stops the sub-thread's own registration."""
    spec = chat.spec
    if spec.chat_payload is None or not parent or depth >= PARENT_DEPTH:
        return
    try:
        sid = SI.check(parent, "the parent session id")
        mother = Chat(
            spec,
            chat.env,
            chat.location,
            sid,
            now=chat.now,
            transport=chat.transport,
            seat=chat.seat,
        )
        note = mother.read()
        if _settled(note):
            return
        payload = spec.chat_payload(sid)
        if _text(spec.kind(payload, chat.env)) not in ("chat", "subagent"):
            return
        grand = _text(spec.parent_id(payload, chat.env)) or str(note.get("parent") or "")
        _ensure_parent(mother, grand, depth + 1)
        note = (
            mother.read()
        )  # the grandparents' turn may have registered this one (a store that names a loop)
        if _settled(note):
            return
        mother.write(dict(note, failed_at=chat.now))
        done = _apply(
            mother,
            mother.registry("register", title=str(note.get("title") or ""), parent=grand),
            dict(note, parent=grand),
        )
        if done["state"] == "open":
            done["announce"] = True
        mother.write(done)
    except Exception:  # noqa: BLE001 - the sub-thread is registered whether or not its parent could be
        return


def _enroll(chat: Chat, title: str, parent: str) -> dict[str, Any]:
    """The registry's answer to registering this chat, after its parent has been (see `_ensure_parent`)."""
    _ensure_parent(chat, parent)
    return chat.registry("register", title=title, parent=parent)


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
    parent = parent_of(chat, note, data)
    answer = _enroll(chat, loose, parent)
    start = dict(note, inherited=inherited, title=loose or note.get("title", ""), parent=parent)
    if inherited:
        start["seen"] = inherited  # the name the chat started with is not a rename
    note = _apply(chat, answer, start)
    if note["state"] == "mismatch":
        # Another seat holds this chat:  the note says so, and its title is that seat's to keep.
        chat.write(note)
        out.emit("start", context=no_letter(spec, note))
        return 0
    if note["state"] == "failed":
        # No letter, for whatever reason:  the chat is titled "??: <Title>" and carries on.
        shown = unlettered(
            chat, note, loose, current, data, "start", settable=source in spec.title_sources
        )
        fix = advice(chat, note, current, data, "start")
        chat.write(note)
        out.emit("start", context=_join(no_letter(spec, note), fix), title=shown)
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
    fix = advice(chat, note, current, data, "start")
    chat.write(note)
    out.emit(
        "start",
        context=_join(_reissued(spec, answer) + reminder(spec, note), fix),
        title=shown,
        label=label,
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
    if not lettered(note):  # a note that says open with no letter is not one to trust
        # No letter yet:  ask again when the stamp says it is due, else (or when the answer is still no) keep the
        # chat titled "??: <words>", the words being a name someone gave it, its start's title or its first prompt.
        parent = parent_of(chat, note, data)
        note = ask_again(chat, note, _first_title(note, current, data.get("prompt")), parent)
        if not lettered(note):
            shown = unlettered(
                chat, note, _first_title(note, current, data.get("prompt")), current, data, "prompt"
            )
            fix = advice(chat, note, current, data, "prompt")
            chat.write(note)
            if shown == current:
                shown = None
            if shown or fix:
                out.emit("prompt", context=fix, title=shown)
            return 0
    # The model has not been told this letter yet:  a letter given while this chat was a sub-thread's parent, or one
    # that came after the chat had none, is news.
    announce = bool(note.pop("announce", False))
    title = None
    answer: dict[str, Any] = {}
    text = "" if note.get("titled") else _first_title(note, current, data.get("prompt"))
    if announce and not text and not note.get("titled"):
        # A letter that came on a prompt with no words (a bare slash command):  the "??" goes now, the words later.
        title = compose(note["label"], "")
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
    moved = ""
    prior = follow_letter(note, answer)
    if prior:
        # The registry changed this chat's letter in place: the title takes the new one, over the words the chat
        # shows (or is about to show) with any letter it held taken off.
        title = _follow_title(note, prior, title or current)
        moved = relabeled(prior, str(note["label"]))
        announce = True
    if _gone(answer):
        # The Operator has no registration for this chat any more (its letter was retired, or the registry was
        # restored from an older copy): check with it again, in case it needs a new one.
        words = str(note.get("title") or "") or bare(current, held(note))
        if words == PLACEHOLDER:
            words = ""
        parent = str(note.get("parent") or "")
        note = _apply(chat, _enroll(chat, words, parent), note)
        if note["state"] == "open":
            note.pop("unsent", None)
            note.update(titled=bool(words), title=words, placeholder=compose(note["label"], ""))
            title = compose(note["label"], words)
            announce = True  # the letter may have changed
        else:
            # The letter is gone and no new one came:  the chat is a "??" chat now.
            note.update(titled=bool(words), placeholder=compose(UNLETTERED, ""))
            if words:
                note["title"] = words
            title = compose(UNLETTERED, words)
    shown = None
    if title:
        shown = present(chat, note, title, current, data, "prompt")
    else:
        _retry_pending(chat, note, current, data)
    if note.get("state") == "open" and note.get("label"):
        # The Linear issues the turn before touched (AFC-694), from the transcript and folder the hook data names.
        sync_issues(
            note,
            data,
            _text(chat.env.get("HOME")) or os.path.expanduser("~"),
            chat.now,
            lambda text: chat.registry("issues", issues=text),
        )
    fix = advice(chat, note, current, data, "prompt")
    chat.write(note)
    if announce or (shown and shown != current) or fix:
        out.emit(
            "prompt",
            context=_join(moved + reminder(spec, note) if announce else "", fix),
            title=shown,
            label=str(note.get("label") or ""),
        )
    return 0


def on_rename(chat: Chat, data: Mapping[str, Any], out: Out) -> int:
    """A platform that says when a chat was renamed: keep the letter on its title and the Operator's words current."""
    note = chat.read()
    current = tidy(chat.spec.current_title(data, chat.env))
    if note and not lettered(note) and note.get("state") not in ("mismatch", "closed"):
        # A chat with no letter keeps its "??" over a new name, and asks again when the stamp says it is due.
        words = bare(current, held(note))
        if not words or current in (note.get("shown"), note.get("seen")):
            return 0
        note["seen"] = current
        parent = parent_of(chat, note, data)
        note = ask_again(chat, note, words, parent)
        if lettered(note):
            note["titled"] = True
            note["title"] = words
            shown = present(chat, note, compose(str(note["label"]), words), current, data, "rename")
            context = reminder(chat.spec, note)
            note.pop("announce", None)
        else:
            shown, context = unlettered(chat, note, words, current, data, "rename"), ""
        chat.write(note)
        if shown or context:
            out.emit("rename", context=context, title=shown, label=str(note.get("label") or ""))
        return 0
    if note.get("state") != "open":
        return 0
    words, show = _renamed(note, current)
    if not (words or show):
        return 0
    note["seen"] = current
    moved = ""
    answer: dict[str, Any] = {}
    if words:
        note["title"] = words
        answer = _push(
            chat, note, "title", words
        )  # a failed send is kept in the note for a later prompt
    elif chat.now - float(note.get("touched_at") or 0) >= TOUCH_SECONDS:
        # Only the letter was dropped, so the Operator needs no words, but the letter put back must be the one the
        # registry holds now, so a touch that is due (TOUCH_SECONDS, as on a prompt) is made here.
        answer = _push(chat, note, "touch")
    prior = follow_letter(note, answer)
    if prior:
        show = _follow_title(note, prior, show or current)
        moved = relabeled(prior, str(note["label"])) + reminder(chat.spec, note)
    shown = present(chat, note, show, current, data, "rename") if show else None
    chat.write(note)
    if shown or moved:
        out.emit("rename", context=moved, title=shown, label=str(note.get("label") or ""))
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
    now: float | None = None,
    platform: str = sys.platform,
    transport: Transport | None = None,
    location_flag: str = "",
    stderr: TextIO | None = None,
) -> int:
    """Run one hook event for `spec`.  Always 0: whatever goes wrong is a line of context, or nothing."""
    try:
        return _run_hook(spec, env, stdin, stdout, now, platform, transport, location_flag)
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
    now: float | None,
    platform: str,
    transport: Transport | None,
    location_flag: str,
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
    if where is not None and where not in spec.locations:
        return 0  # the platform does not run there, so this is not one of its chats
    if where is None and "server" not in spec.locations:
        return 0  # a Mac-only platform on a host that is not the Mac:  not one of its chats
    # A host that does not state its location still runs the chat:  every registry call answers NO_LOCATION, so the
    # chat is a "??" chat until the host says where it is.
    chat = Chat(
        spec,
        env,
        where or "",
        sid,
        now=time.time() if now is None else now,
        transport=transport,
        seat=who,
    )
    home = str(env.get("HOME") or os.path.expanduser("~"))
    if event == "start":
        code = on_start(chat, data, out, home)
        if _text(data.get("source")) != "compact":
            _report_gone_after_start(chat, stdout)
        return code
    if event == "prompt":
        return on_prompt(chat, data, out)
    if event == "rename":
        return on_rename(chat, data, out)
    if event == "turn":
        return on_turn(chat, data)
    return on_end(chat, data)


def show(spec: PlatformSpec, argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    """Print a chat's note:  the chat named with --session-id, else the one this shell runs in, by the spec's own
    reader and then its session id variables (`session_env`, AGENT_SESSION), as `request` finds it."""
    sid = argv[1] if len(argv) > 1 and argv[0] == "--session-id" else ""
    if not sid:
        try:
            # The spec's own reader, for one that reads the environment.
            own = spec.session_id({}, env)
        except Exception:  # noqa: BLE001 - a reader that cannot answer here names no chat
            own = ""
        names = (*spec.session_env, "AGENT_SESSION")
        found = [own, *(_text(env.get(name)) for name in names)]
        sid = next((value for value in found if value), "")
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
# Asking for a letter now:  `simple-chat letter request`, one command on every platform
# --------------------------------------------------------------------------------------------


def adapter_platforms() -> list[str]:
    """Every platform in the table:  each adapter module beside this one, and any spec registered in process."""
    names = set(SPECS)
    for info in pkgutil.iter_modules([os.path.dirname(os.path.abspath(__file__))]):
        if info.name.startswith("platform_letter_"):
            names.add(info.name[len("platform_letter_") :].replace("_", "-"))
    return sorted(name for name in names if valid_platform(name))


#: The shapes of the note fields the request path reads:  a field that is present must have its type.
_NOTE_FIELDS: dict[str, tuple[type, ...]] = {
    "state": (str,),
    "label": (str,),
    "platform": (str,),
    "title": (str,),
    "shown": (str,),
    "parent": (str,),
    "location": (str,),
    "error": (str,),
    "failed_at": (int, float),
    "sync_failed_at": (int, float),
    "touched_at": (int, float),
    "generation": (int,),
    "labels": (list,),
}


def note_problem(note: Mapping[str, Any]) -> str:
    """What is wrong with a letter note's shape, or "" when every field the request path reads has its type and a
    label is "" or one or two capitals."""
    for key, kinds in _NOTE_FIELDS.items():
        value = note.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, kinds)):
            return "%s must be %s" % (key, " or ".join(k.__name__ for k in kinds))
    label = note.get("label") or ""
    if label and not LABEL_RE.fullmatch(label):
        return "label must be one or two capital letters"
    return ""


def find_chat(env: Mapping[str, str], sid: str = "") -> tuple[PlatformSpec, str, str] | str:
    """(spec, session id, seat) of the chat a `request` is about, or why there is none.

    With a session id, the chat whose letter note has that name, under any seat.  Without one, the chat this shell
    runs in:  the first spec in the table whose own session-id reader, or whose session id variables (its
    `session_env`, then AGENT_SESSION, the fleet's own name for it), name an id whose note exists.  The note is the proof the chat is that platform's:  a
    stray variable from another platform's shell finds no note and is never registered."""
    root = L.state_root(env)
    if sid:
        try:
            sid = SI.check(sid, "the session id")
        except ValueError as exc:
            return str(exc)
        try:
            seats = sorted(os.listdir(root))
        except OSError:
            seats = []
        for seat in seats:
            if not I.valid_seat(seat):
                continue
            path = os.path.join(root, seat, "letters", sid + ".json")
            try:
                with open(path, encoding="utf-8") as fh:
                    note = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(note, dict) or note_problem(note):
                problem = note_problem(note) if isinstance(note, dict) else "not a JSON object"
                return "invalid letter note for chat %s: %s" % (sid, problem)
            platform = str(note.get("platform") or "")
            spec = load_spec(platform) if valid_platform(platform) else None
            if spec is None:
                return "chat %s is a %s chat, which has no entry in the letter table" % (
                    sid,
                    platform or "unknown",
                )
            return spec, sid, seat
        return "no chat with the session id %s has a letter note on this host" % sid
    for platform in adapter_platforms():
        spec = load_spec(platform)
        if spec is None:
            continue
        seat = spec.seat_for({}, env)
        try:
            own = spec.session_id(
                {}, env
            )  # the spec's own reader, for one that reads the environment
        except Exception:  # noqa: BLE001 - a reader that cannot answer here names no chat
            own = ""
        for value in (own, *(env.get(name) for name in (*spec.session_env, "AGENT_SESSION"))):
            try:
                found = SI.check(_text(value), "the session id")
            except ValueError:
                continue
            if seat and os.path.isfile(note_path(env, seat, found)):
                return spec, found, seat
    return "no chat with a letter note runs in this shell; pass --session-id S"


def request_main(
    argv: list[str],
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    *,
    now: float | None = None,
    platform: str = sys.platform,
    transport: Transport | None = None,
) -> int:
    """`simple-chat letter request [--session-id S]`:  ask the one assigner for this chat's letter now, whatever the
    retry stamp says, and print the letter or the reason there is none in one line (owner, Nov 11, 2026:  "enable a
    thread to request letters after the initial thread opening").  The command is the same on every platform;  only
    the spec's own readers (where its session id comes from) differ, and they are data in the table.

    A letter that comes is put in the chat's note, and on its title through the adapter's writer when it has one;  a
    platform that takes titles only from hook output swaps "??" for it at its next prompt.  Exit 0 with a letter, 1
    without one, 2 for bad arguments."""
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    try:
        flags = _flags(argv, ("--session-id",))
    except ValueError:
        stdout.write("usage: simple-chat letter request [--session-id S]\n")
        return 2
    found = find_chat(env, flags.get("session-id", ""))
    if isinstance(found, str):
        stdout.write("no letter: %s\n" % found)
        return 1
    spec, sid, seat = found
    chat = Chat(
        spec,
        env,
        location(env, platform) or "",
        sid,
        now=time.time() if now is None else now,
        transport=transport,
        seat=seat,
    )
    note = chat.read()
    problem = note_problem(note)
    if problem:
        stdout.write("no letter: invalid letter note for chat %s: %s\n" % (sid, problem))
        return 1
    if note.get("location") in LOCATIONS:
        chat.location = str(
            note["location"]
        )  # the location the chat's own hook registered it under
    if note.get("state") == "mismatch":
        stdout.write(
            "no letter: %s chat %s is registered under another seat\n" % (spec.display, sid)
        )
        return 1
    if lettered(note):
        note.pop("sync_failed_at", None)  # an explicit ask is never held back by the stamp
        answer = _push(chat, note, "touch")
        prior = follow_letter(note, answer)
        if prior:
            note["announce"] = True
            note["titled"] = False  # the next prompt puts the new letter on the title
        if not _gone(answer):
            chat.write(note)
            if _failed(answer):
                stdout.write(
                    "letter %s (from this chat's note; the assigner did not answer: %s)\n"
                    % (note["label"], answer.get("error"))
                )
                return 0
            stdout.write("letter %s: %s chat %s\n" % (note["label"], spec.display, sid))
            return 0
        note = dict(note, state="failed", label="", failed_at=0)
    words = str(note.get("title") or "") or bare(note.get("shown") or "", held(note))
    parent = str(note.get("parent") or "")
    note = ask_again(chat, note, "" if words == PLACEHOLDER else words, parent, force=True)
    if lettered(note):
        title = compose(str(note["label"]), str(note.get("title") or ""))
        payload = dict(spec.chat_payload(sid)) if spec.chat_payload else {"session_id": sid}
        present(chat, note, title, str(note.get("shown") or ""), payload, "retry", settable=False)
        note["titled"] = note.get("shown") == title
        chat.write(note)
        stdout.write("letter %s: %s chat %s\n" % (note["label"], spec.display, sid))
        return 0
    chat.write(note)
    stdout.write("no letter: %s\n" % (note.get("error") or "no answer"))
    return 1


# --------------------------------------------------------------------------------------------
# A chat's parent, for a hook on another host:  `parent`, a read-only verb
# --------------------------------------------------------------------------------------------

#: The payload key that carries the platform's own id for a chat (`parent --app-id`), for an adapter whose
#: records are keyed by it.  A hook's own payload never has it.
PARENT_APP_ID = "letter_app_id"
#: The payload key that marks the `parent` verb's own question, so an adapter that would ask another host for a
#: parent answers this one from its own records and never passes the question on.
PARENT_VERB = "letter_parent_verb"
#: What such an id may look like on a command line:  the characters of a session id, and no more of them.
APP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def parent_main(
    argv: list[str], env: Mapping[str, str] | None = None, stdout: TextIO | None = None
) -> int:
    """`parent --platform P --session-id S [--app-id A] [--source SRC]`:  the session id of the chat that started
    chat S, on a line of its own, or nothing when this host knows none.  Exit 0 either way, 2 for bad arguments.

    It only reads:  the adapter's `parent_id` is asked about a start payload made from the arguments (`--app-id`,
    the platform's own id for the chat, rides in it as PARENT_APP_ID;  `--source` is the start's source), and no
    registry is called and no note is read or written.  It is what a hook on another host runs here over ssh when
    the platform keeps its record of who started whom on this host.  Every argument is checked before the
    adapter sees it, and only a session id is ever printed."""
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    try:
        flags = _flags(argv, ("--platform", "--session-id", "--app-id", "--source"))
        sid = SI.check(flags.get("session-id", ""), "the session id")
    except ValueError:
        return 2
    name = flags.get("platform", "")
    app_id, source = flags.get("app-id", ""), flags.get("source", "")
    spec = load_spec(name) if valid_platform(name) else None
    if spec is None or (app_id and not APP_ID_RE.fullmatch(app_id)):
        return 2
    if source and source not in ("startup", "resume", "clear", "fork"):
        return 2
    payload: dict[str, Any] = {
        "hook_event_name": "SessionStart",
        "session_id": sid,
        "source": source or "startup",
        PARENT_VERB: True,
    }
    if app_id:
        payload[PARENT_APP_ID] = app_id
    try:
        parent = SI.check(spec.parent_id(payload, env), "the parent session id")
    except Exception:  # noqa: BLE001 - no parent known is no line, never an error
        return 0
    if parent != sid:
        stdout.write(parent + "\n")
    return 0


# --------------------------------------------------------------------------------------------
# Chats that are gone for good:  one report per host, platform and location, from the hooks
# --------------------------------------------------------------------------------------------
#
# Some ends reach no hook:  a platform's app can archive a chat with no end event of its own.  The adapter knows
# those chats (`gone_sessions` for the ones that run on this host, `gone_elsewhere` for the ones its app runs on
# another host), and the assigner has one call for them, `report-gone`, which closes the rows a caller names at one
# location (and refuses a report that would close more than a share of that location's live rows).  A chat's start
# is the trigger:  no timer, no scheduler.


def gone_stamp_path(env: Mapping[str, str], seat: str, platform: str, where: str) -> str:
    """Where a host keeps the time of its last gone report for one platform and location:  beside the seat's letter
    notes, under a name no session id can have (a session id never starts with a dot)."""
    folder = os.path.dirname(note_path(env, seat, "stamp"))
    return os.path.join(folder, ".gone-%s-%s" % (platform, where))


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: str, data: Mapping[str, Any]) -> bool:
    """Save a small private file whole (an unpredictable temporary name, then a rename);  False when it cannot be."""
    tmp = ""
    try:
        folder = os.path.dirname(path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".letter-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(data), fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = ""
        _sync_folder(folder)
    except (OSError, TypeError, ValueError):
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
    return True


def _seconds(value: object) -> float:
    """A time from a note or a stamp, or 0 when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def upgraded_note(note: Mapping[str, Any], platform: str) -> dict[str, Any] | None:
    """A note the retired per-seat client wrote, in this client's shape, or None when it needs no change.

    Such a note names no `platform`, and keeps the letters the chat held before under `held_labels` (oldest first,
    its own last) and `prior_label`, where this client keeps them under `labels` (newest first).  The platform is the
    reader's:  a seat's retired client wrote for one platform only."""
    if not note or note.get("platform"):
        return None
    earlier = note.get("held_labels")
    order = [
        note.get("label"),
        note.get("prior_label"),
        *reversed(earlier if isinstance(earlier, list) else []),
        *(note.get("labels") if isinstance(note.get("labels"), list) else []),
    ]
    labels: list[str] = []
    for label in order:
        if isinstance(label, str) and LABEL_RE.fullmatch(label) and label not in labels:
            labels.append(label)
    out = {k: v for k, v in note.items() if k not in ("held_labels", "prior_label")}
    out.update(platform=platform, labels=labels[:8])
    return out


def quiet_chats(
    env: Mapping[str, str],
    seat: str,
    platform: str,
    where: str,
    now: float,
    *,
    adopt: bool = False,
    rewrite: bool = True,
) -> dict[str, dict[str, Any]]:
    """The chats of this host a gone report may name, by session id, each with its letter and the time its hook last
    ran:  the seat's own letter notes for the platform and location that hold a letter, were not reported before
    (`gone_at`), and whose hook has not run for GONE_QUIET_SECONDS.  A chat whose hook ran more recently is plainly
    not gone.  There is no upper bound on how long a chat has been quiet:  a chat is often archived days after its
    last prompt.  The newest GONE_SCAN_NOTES note files are read, and the GONE_REPORT_IDS most recently quiet chats
    are answered, so the work is bounded however many notes a host has kept.

    `adopt` is for the platform a seat's retired client wrote for:  a note of that client (no `platform`) is put in
    this client's shape where it lies (`upgraded_note`), so its chat can be named too.  Without `rewrite` it is only
    read that way and its file is left as it is (a dry run changes nothing)."""
    folder = os.path.dirname(note_path(env, seat, "stamp"))
    files: list[tuple[float, str]] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".json") or name.startswith("."):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        files.append((entry.stat().st_mtime, name))
                except OSError:
                    continue
    except OSError:
        return {}
    files.sort(reverse=True)
    found: list[dict[str, Any]] = []
    for _, name in files[:GONE_SCAN_NOTES]:
        path = os.path.join(folder, name)
        note = _read_json(path)
        try:
            sid = SI.check(name[: -len(".json")], "the session id")
        except ValueError:
            continue
        if note.get("session_id") != sid:
            continue
        if adopt:
            fresh = upgraded_note(note, platform)
            if fresh is not None:
                if rewrite:
                    _save_json(path, fresh)
                note = fresh
        if note.get("platform") != platform or note.get("location") != where:
            continue
        if not lettered(note) or note.get("gone_at"):
            continue
        seen = _seconds(note.get("updated_at"))
        label = note.get("label")
        if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
            continue
        if now - seen >= GONE_QUIET_SECONDS:
            found.append({"session_id": sid, "label": label, "seen": seen})
    found.sort(key=lambda row: (-row["seen"], row["session_id"]))
    return {row["session_id"]: row for row in found[:GONE_REPORT_IDS]}


def gone_chats(
    spec: PlatformSpec,
    env: Mapping[str, str],
    seat: str,
    where: str,
    now: float,
    *,
    rewrite: bool = True,
) -> list[dict[str, Any]]:
    """The chats of this host the adapter says are gone for good (`gone_sessions`), the one longest quiet first.
    Only an id that was asked about counts, so an adapter can never name a chat that is not one of this host's own
    quiet ones.  An adapter that raises is the caller's to catch."""
    if spec.gone_sessions is None:
        return []
    adopt = isinstance(spec.seat, str) and spec.seat == seat
    quiet = quiet_chats(env, seat, spec.platform, where, now, adopt=adopt, rewrite=rewrite)
    if not quiet:
        return []
    named = spec.gone_sessions(env, frozenset(quiet))
    gone = [quiet[sid] for sid in set(named or ()) if isinstance(sid, str) and sid in quiet]
    gone.sort(key=lambda row: (row["seen"], row["session_id"]))
    return gone


def gone_chats_elsewhere(
    spec: PlatformSpec, env: Mapping[str, str], there: str, sent: object = ()
) -> tuple[list[dict[str, Any]], list[str]]:
    """(the chats that run at `there`, another location, that the adapter says are gone for good and that this host
    has not reported yet;  the ids it reported before that the adapter still names).

    This host holds no note of such a chat, so what it reported is kept in the report's stamp (`sent`), and a chat is
    named once.  One the adapter stops naming (it was unarchived) is forgotten, so it is named again if it goes
    again.  Only session ids count, sorted, so a report is the same whatever order the adapter answers in."""
    if spec.gone_elsewhere is None:
        return [], []
    named: list[str] = []
    for item in spec.gone_elsewhere(env, there) or ():
        try:
            sid = SI.check(item, "a gone session id")
        except ValueError:
            continue
        if sid not in named:
            named.append(sid)
    named.sort()
    listed = sent if isinstance(sent, (list, tuple)) else ()
    before = {sid for sid in listed if isinstance(sid, str)}
    kept = [sid for sid in named if sid in before]
    fresh = [{"session_id": sid, "label": ""} for sid in named if sid not in before]
    return fresh, kept


def _send_gone(
    spec: PlatformSpec,
    env: Mapping[str, str],
    seat: str,
    there: str,
    ids: list[str],
    transport: Transport | None,
) -> dict[str, Any]:
    """One `report-gone` call for one platform and location, answered as the registry side answers it.  Whatever
    goes wrong is an error answer."""
    argv = [
        "report-gone",
        "--platform",
        spec.platform,
        "--location",
        there,
        "--seat",
        seat,
        "--source",
        spec.source,
        "--gone=" + json.dumps(ids),
    ]
    try:
        return _registry_side(argv, env, transport)
    except Exception as exc:  # noqa: BLE001 - a report never blocks a chat
        return {"error": "the registry call failed (%s)" % type(exc).__name__}


def _allowed(value: object) -> int:
    """How many chats the assigner said it would take, from a stamp or an answer, or 0."""
    return value if type(value) is int and value > 0 else 0


def report_gone(
    spec: PlatformSpec,
    env: Mapping[str, str],
    seat: str,
    where: str,
    *,
    now: float,
    transport: Transport | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Tell the assigner which of this host's chats are gone for good, in ONE `report-gone` call for the platform, at
    most once per GONE_REPORT_SECONDS (the stamp beside the notes;  `force` sends whatever the stamp says).

    Answers what happened:  `skipped` with the reason nothing was looked for, else `gone` (the chats named) with
    `closed` (the session ids whose rows the assigner closed now) or `error`.  The stamp is written before anything
    else, so a report that fails, or a hook killed inside it, is tried again after the window and never on every
    start.  The assigner refuses a report that would close more than its share of the location's live rows and says
    how many it would take (`allowed_now`):  the stamp keeps that number, and the next report names no more than that.

    A chat the report named is marked in its note (`gone_at`) once the assigner has answered, whether its row was
    closed now or was closed already, so it is not named again.  The note stays open, with its touch made due:  if
    the chat is alive after all, its next prompt asks the assigner, hears its letter is gone and registers again
    (`on_prompt`), which takes the mark off (`_apply`).  A refused or failed report changes no note."""
    if spec.gone_sessions is None or not seat or where not in LOCATIONS:
        return {"skipped": "this platform names no gone chats on this host"}
    path = gone_stamp_path(env, seat, spec.platform, where)
    stamp = _read_json(path)
    if not force and "at" in stamp and 0 <= now - _seconds(stamp["at"]) < GONE_REPORT_SECONDS:
        return {"skipped": "not due"}
    limit = _allowed(stamp.get("allowed"))
    if not _save_json(path, {"at": now, **({"allowed": limit} if limit else {})}):
        # No stamp, no throttle:  nothing is sent.
        return {"skipped": "the stamp could not be saved"}
    gone = gone_chats(spec, env, seat, where, now)[: limit or GONE_REPORT_IDS]
    if not gone:
        _save_json(path, {"at": now})
        return {"gone": []}
    answer = _send_gone(spec, env, seat, where, [row["session_id"] for row in gone], transport)
    if "error" in answer:
        if answer.get("code") == "gone_share_exceeded":
            _save_json(path, {"at": now, "allowed": max(1, _allowed(answer.get("allowed_now")))})
        return {"gone": gone, "error": str(answer.get("error") or "no answer")[:200]}
    _save_json(path, {"at": now})
    closed = [sid for sid in answer.get("gone") or [] if isinstance(sid, str)]
    for row in gone:
        chat = Chat(spec, env, where, row["session_id"], now=now, transport=transport, seat=seat)
        note = chat.read()
        if lettered(note) and _seconds(note.get("updated_at")) == row["seen"]:
            chat.write(dict(note, gone_at=now, touched_at=0))
    return {"gone": gone, "closed": closed}


def report_gone_elsewhere(
    spec: PlatformSpec,
    env: Mapping[str, str],
    seat: str,
    there: str,
    *,
    now: float,
    transport: Transport | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Tell the assigner which chats that run at `there`, a location other than this host's, are gone for good
    (`gone_elsewhere`:  a platform's app on this host can run, and archive, chats on another one).  ONE `report-gone`
    call for the platform and that location, with its own stamp and window, and the same rules as `report_gone`:
    the stamp first, a refusal's `allowed_now` kept for the next report, and no error ever raised into a chat.

    The assigner closes only rows that are open at `there` under an id the report names, so an id it does not hold
    open (the chat's own end already closed it, or its hook never registered it) costs nothing.  This host has no
    note to mark, so the ids a report was answered for are kept in the stamp (`sent`) and not named again while the
    adapter still names them."""
    if spec.gone_elsewhere is None or not seat or there not in LOCATIONS:
        return {"skipped": "this platform names no gone chats at that location"}
    path = gone_stamp_path(env, seat, spec.platform, there)
    stamp = _read_json(path)
    if not force and "at" in stamp and 0 <= now - _seconds(stamp["at"]) < GONE_REPORT_SECONDS:
        return {"skipped": "not due"}
    limit = _allowed(stamp.get("allowed"))
    sent = stamp.get("sent")

    def save(sent_now: object, allowed: int = 0) -> bool:
        listed = sent_now if isinstance(sent_now, list) else []
        kept = [sid for sid in listed if isinstance(sid, str)]
        body: dict[str, Any] = {"at": now, "sent": kept[-GONE_SENT_IDS:]}
        if allowed:
            body["allowed"] = allowed
        return _save_json(path, body)

    if not save(sent, limit):
        return {"skipped": "the stamp could not be saved"}
    fresh, kept = gone_chats_elsewhere(spec, env, there, sent)
    fresh = fresh[: limit or GONE_REPORT_IDS]
    if not fresh:
        save(kept)
        return {"gone": []}
    ids = [row["session_id"] for row in fresh]
    answer = _send_gone(spec, env, seat, there, ids, transport)
    if "error" in answer:
        refused = answer.get("code") == "gone_share_exceeded"
        save(kept, max(1, _allowed(answer.get("allowed_now"))) if refused else 0)
        return {"gone": fresh, "error": str(answer.get("error") or "no answer")[:200]}
    save(kept + ids)
    closed = [sid for sid in answer.get("gone") or [] if isinstance(sid, str)]
    labels = answer.get("labels")
    for row in fresh:
        label = labels.get(row["session_id"]) if isinstance(labels, Mapping) else ""
        row["label"] = label if isinstance(label, str) and LABEL_RE.fullmatch(label) else ""
    return {"gone": fresh, "closed": closed}


def _report_gone_after_start(chat: Chat, stdout: TextIO) -> None:
    """The gone reports a chat's start makes, after the chat's own answer is out:  this host's own chats, then the
    chats the platform's app runs at each other location.  Nothing here reaches the chat:  whatever goes wrong is
    swallowed, and each report's stamp holds its next try back for the window."""
    spec = chat.spec
    if spec.gone_sessions is None and spec.gone_elsewhere is None:
        return
    if chat.location not in LOCATIONS:
        return
    try:
        stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        report_gone(
            spec, chat.env, chat.seat, chat.location, now=chat.now, transport=chat.transport
        )
    except Exception:  # noqa: BLE001 - a report never blocks a chat, whatever an adapter or the host does
        pass
    for there in LOCATIONS:
        if there == chat.location:
            continue
        try:
            report_gone_elsewhere(
                spec, chat.env, chat.seat, there, now=chat.now, transport=chat.transport
            )
        except Exception:  # noqa: BLE001 - a report never blocks a chat, whatever an adapter or the host does
            pass


GONE_USAGE = "usage: platform-letter gone [--platform P] [--location L] [--apply]\n"


def gone_main(
    argv: list[str],
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    *,
    now: float | None = None,
    platform: str = sys.platform,
    transport: Transport | None = None,
) -> int:
    """`gone [--platform P] [--location L] [--apply]`:  what this host's hooks would report gone, for the platform
    named or for every platform whose adapter names its gone chats (`gone_sessions`, `gone_elsewhere`), and `--apply`
    to report it now, whatever the stamps say.  One line per chat names its letter (a dash for a chat that runs on
    another host, whose letter this host does not know), the first 8 characters of its session id and its location,
    never a title.  Exit 0, 1 when a report was refused or failed, 2 for bad arguments."""
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    apply = "--apply" in argv
    try:
        flags = _flags([word for word in argv if word != "--apply"], ("--platform", "--location"))
    except ValueError:
        stdout.write(GONE_USAGE)
        return 2
    where = location(env, platform, flags.get("location", ""))
    if where is None:
        stdout.write("gone report:  %s\n" % NO_LOCATION)
        return 2
    specs: list[PlatformSpec] = []
    if "platform" in flags:
        name = flags["platform"]
        spec = load_spec(name) if valid_platform(name) else None
        if spec is None:
            stdout.write("gone report:  no adapter for the platform %r\n" % name)
            return 2
        if spec.gone_sessions is None and spec.gone_elsewhere is None:
            stdout.write("gone report:  the %s adapter names no gone chats\n" % name)
            return 2
        specs.append(spec)
    else:
        for name in adapter_platforms():
            try:
                spec = load_spec(name)
            except Exception:  # noqa: BLE001 - an adapter that cannot load has nothing to report
                continue
            if spec is None or (spec.gone_sessions is None and spec.gone_elsewhere is None):
                continue
            specs.append(spec)
    stamp = time.time() if now is None else now
    code = 0
    for spec in specs:
        code = max(code, _gone_for(spec, env, where, stdout, stamp, apply, transport))
    return code


def _gone_for(
    spec: PlatformSpec,
    env: Mapping[str, str],
    where: str,
    stdout: TextIO,
    now: float,
    apply: bool,
    transport: Transport | None,
) -> int:
    """`gone` for one platform:  this host's own chats, then each other location's, with the lines for each and
    the report when `apply`.  The worst exit status of them."""
    seat = spec.seat_for({}, env)
    code = 0
    for there in (where, *(loc for loc in LOCATIONS if loc != where)):
        local = there == where
        if (spec.gone_sessions if local else spec.gone_elsewhere) is None or not seat:
            continue
        head = "%s gone report, %s:  " % (spec.display, there)

        def tag(row: Mapping[str, Any], there: str = there) -> str:
            letter = row["label"] or "-"
            return "%s %s (%s, %s)" % (spec.display, letter, row["session_id"][:8], there)

        try:
            if apply and local:
                done = report_gone(spec, env, seat, there, now=now, transport=transport, force=True)
            elif apply:
                done = report_gone_elsewhere(
                    spec, env, seat, there, now=now, transport=transport, force=True
                )
            elif local:
                done = {"gone": gone_chats(spec, env, seat, there, now, rewrite=False)}
            else:
                sent = _read_json(gone_stamp_path(env, seat, spec.platform, there)).get("sent")
                done = {"gone": gone_chats_elsewhere(spec, env, there, sent)[0]}
        except Exception as exc:  # noqa: BLE001 - an adapter that cannot say reports nothing
            stdout.write(head + "could not read this host's chats (%s)\n" % type(exc).__name__)
            code = max(code, 1)
            continue
        gone = done.get("gone") or []
        if not apply:
            stdout.writelines("would report %s\n" % tag(row) for row in gone)
            stdout.write(
                head + "%d gone  (dry run:  nothing sent;  --apply reports them now)\n" % len(gone)
            )
        elif "error" in done:
            stdout.write(head + "%d gone, not reported:  %s\n" % (len(gone), done["error"]))
            code = max(code, 1)
        else:
            closed = set(done.get("closed") or [])
            for row in gone:
                word = "reported" if row["session_id"] in closed else "not open"
                stdout.write("%s %s\n" % (word, tag(row)))
            stdout.write(head + "%d gone, %d closed now\n" % (len(gone), len(closed)))
    return code


# --------------------------------------------------------------------------------------------
# Installing hooks into a JSON hook file (once per host)
# --------------------------------------------------------------------------------------------


def command_for(home: str, platform: str, where: str = "", mac: str = "") -> str:
    """The hook command an adapter's hook file carries: this host's own `platform-letter` by absolute path, with the
    location, and the Mac's ssh destination when the host was given one, as flags (a hook file has no env block to
    put them in)."""
    parts = [
        shlex.quote(os.path.join(home, ".local", "bin", ENTRY)),
        "hook",
        "--platform",
        platform,
    ]
    if where:
        parts += ["--location", where]
    if mac and MAC_SSH_RE.fullmatch(mac):
        parts += ["--mac-ssh", mac]
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


#: The commands of the two letter clients this table replaced, and the platform whose entries they are.
RETIRED_HOOKS = {"simple-chat": "claude", "mm-session-letter": "minimax"}


def installed_command(entry: object, platform: str) -> list[str] | None:
    """The words of the command in a hook entry when it is this platform's `platform-letter hook` (or `simple-chat
    letter hook`, the same command), else None.  The entry may have any shape: every string in it is looked at.

    The hooks installed before Claude and MiniMax joined the table (`simple-chat letter hook` with no --platform, and
    `mm-session-letter hook-...`) count as theirs (RETIRED_HOOKS), so `install` replaces them in place instead of
    adding a second set."""
    for text in _strings(entry):
        try:
            words = shlex.split(text)
        except ValueError:
            continue
        program = os.path.basename(words[0]) if words else ""
        if program == ENTRY and words[1:2] == ["hook"]:
            rest = words[2:]
        elif program == "simple-chat" and words[1:3] == ["letter", "hook"]:
            rest = words[3:]
        elif program in RETIRED_HOOKS and words[1:2] and words[1].startswith("hook-"):
            rest = []
        else:
            continue
        named = _flag(rest, "--platform") or RETIRED_HOOKS.get(program, "")
        if named == platform:
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
    check: bool = False
    remove: bool = False
    mac_ssh: str = ""


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
    location goes into the hook command as a flag; running it again without one keeps the one already installed."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    path = installer.path if os.path.isabs(installer.path) else os.path.join(home, installer.path)
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
    mac = opts.mac_ssh or _flag(inherited, "--mac-ssh")
    command = command_for(home, platform, where, mac)
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
            _sync_folder(os.path.dirname(path))
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
    """`install --platform P [--location server|mac] [--mac-ssh DEST] [--check | --remove]`: find the platform's
    adapter and run its installer.  `--mac-ssh` names the ssh destination of the Mac for a host that is not the Mac
    (SESSION_LETTER_MAC_SSH):  the hook command carries it, and an install without the flag keeps the one it finds.  An adapter with its own `install(options, env, stdout)` (a plugin directory, a
    bundle) is called as it is; one with an `INSTALL` (a JsonHookInstaller) gets `install_json_hooks`."""
    import argparse

    parser = argparse.ArgumentParser(prog=ENTRY + " install")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--location", choices=LOCATIONS)
    parser.add_argument("--mac-ssh", default="")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    if args.mac_ssh and not MAC_SSH_RE.fullmatch(args.mac_ssh):
        return _emit(stdout, {"error": "--mac-ssh must name an ssh destination"}, 2)
    opts = InstallOptions(args.platform, args.location or "", args.check, args.remove, args.mac_ssh)
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
    "usage: platform-letter {hook --platform P [--location L]\n"
    "                        | registry OP --platform P ...\n"
    "                        | show [--platform P] [--session-id S]\n"
    "                        | install --platform P [--location L] [--mac-ssh DEST] [--check | --remove]\n"
    "                        | request [--session-id S]\n"
    "                        | gone [--platform P] [--location L] [--apply]\n"
    "                        | parent --platform P --session-id S [--app-id A] [--source SRC]}\n"
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
            # A command line this hook cannot read (a retired flag) is a hook that does nothing and exits 0:  the hook
            # never blocks a chat.  Re-running `install` writes the current command.
            flags = _flags(argv[1:], ("--platform", "--location", "--mac-ssh"))
            if MAC_SSH_RE.fullmatch(flags.get("mac-ssh", "")):
                # The flag is the setting, for a hook file with no env block.  The process environment is kept
                # as the mapping it is (the registry API reads its own settings from that very object).
                if env is os.environ:
                    os.environ[ENV_MAC_SSH] = flags["mac-ssh"]
                else:
                    env = {**env, ENV_MAC_SSH: flags["mac-ssh"]}
            spec = load_spec(flags.get("platform", ""))
            if spec is None:
                stderr.write("platform-letter hook: no adapter for the platform\n")
                return 0
            # A platform whose hook payload has no event name, or whose hook answers in a shape of its own
            # (Antigravity: an event inferred from the payload, an answer of injected steps), owns its entry:
            # `hook_main` takes what `hook` takes, minus the spec, and calls `hook` itself.
            own = getattr(load_adapter(flags.get("platform", "")), "hook_main", None)
            if callable(own):
                return int(
                    own(
                        env,
                        stdin,
                        stdout,
                        transport=transport,
                        location_flag=flags.get("location", ""),
                        stderr=stderr,
                    )
                )
            return hook(
                spec,
                env,
                stdin,
                stdout,
                transport=transport,
                location_flag=flags.get("location", ""),
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
        if "platform" not in flags:
            # No platform named:  the chat this shell runs in (or the one named), found by its note, as `request`
            # finds it.
            found = find_chat(env, flags.get("session-id", ""))
            if isinstance(found, str):
                return _emit(stdout, {"error": found}, 1)
            return show(found[0], ["--session-id", found[1]], env, stdout)
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
    if argv[0] == "request":
        return request_main(argv[1:], env, stdout, transport=transport)
    if argv[0] == "parent":
        return parent_main(argv[1:], env, stdout)
    if argv[0] in ("gone", "sweep-archived"):  # the second is the verb's name before this client
        return gone_main(argv[1:], env, stdout, transport=transport)
    stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
