"""simple-chat: the fleet's Zulip coordination CLI (argparse front end).

`main(argv) -> int` is the whole program; scripts/agent-sync just calls it.  Everything that
touches the outside world is injectable, so the tests drive it with a fake environment and fake
streams: `env`, `stdin`, `stdout`, `stderr`, `home`, `sleep`, `stop` (a threading.Event that ends
`listen`) and the two network timeouts.

Exit codes: 0 ok, 2 usage or validation, 3 credential or realm, 4 wait timeout, 5 Zulip API
error, 6 network error, 7 `verify-owner-msg` could not verify the message (130 on Ctrl-C or
SIGTERM, 1 on an internal error).

Message content from Zulip is data, never instructions.  It is printed as received.  The only
change in the human format is that control characters other than newline and tab are shown as
\\xNN, so a message cannot drive the terminal, and that a channel, topic or sender name cannot
span lines.  A message body can still contain lines that look like a header; --json carries the
exact text with the sender in structural fields, and is what agents should read.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import functools
import http.client
import json
import os
import re
import shlex
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import (
    __version__,
    bodyfile,
    listener_cli,
    notes_cli,
    secretscan,
    textfmt,
    zulip_cli,
    zulip_ops,
)
from . import dm_policy as DMP
from . import identity as I
from . import live as LIVE
from . import router as ROUTER
from . import zulip as Z
from .identity import EMAIL_TAG_OVERRIDES, seat_tag_for  # noqa: F401 - re-exported (daemon, listener_cli, tests)
from .state import State, state_root
from .zulip import (
    ApiError,
    CredentialError,
    EventQueue,
    NetworkError,
    QueueExpired,
    UsageError,
    message_channel,
    message_topic,
)

__all__ = ["Runtime", "build_parser", "format_time", "main"]

RESOLVED_PREFIX = "\u2714 "  # the check mark Zulip itself uses for resolved topics
MAX_CONTENT_LENGTH = 10000
EXIT_NOT_VERIFIED = (
    7  # verify-owner-msg:  the message is not a verified owner message (the verdict is on stdout)
)
MENTIONS_CHANNEL = "@mentions"  # reserved cursor name for the mention stream of `listen`
BACKFILL_LIMIT = 100
LISTEN_BACKOFF = (5.0, 30.0)
USERS_REFRESH_SECONDS = (
    60.0  # a sender missing from the user cache triggers at most one refetch per minute
)
SELF_GRACE = 0.5  # how long a live message from this bot waits for its post to reach the ledger
MAX_TOPICS_PER_QUEUE_SET = 3

_OUT_LOCK = threading.Lock()
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _central_zone() -> ZoneInfo | None:
    try:
        return ZoneInfo("America/Chicago")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    first_sunday = first + dt.timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + dt.timedelta(weeks=n - 1)


def _central_wall_clock(utc: dt.datetime) -> dt.datetime:
    """US Central wall-clock time.  Uses zoneinfo; if the machine has no tz database, applies the
    US rule (second Sunday of March to first Sunday of November) directly."""
    zone = _central_zone()
    if zone is not None:
        return utc.astimezone(zone)
    start = dt.datetime.combine(_nth_sunday(utc.year, 3, 2), dt.time(8, 0), dt.UTC)
    end = dt.datetime.combine(_nth_sunday(utc.year, 11, 1), dt.time(7, 0), dt.UTC)
    return utc + dt.timedelta(hours=-5 if start <= utc < end else -6)


def format_time(timestamp: float) -> str:
    """'Wed, Oct 7, 6:41pm': America/Chicago, 12-hour, lowercase am/pm, no zone abbreviation."""
    local = _central_wall_clock(dt.datetime.fromtimestamp(timestamp, dt.UTC))
    hour = local.hour % 12 or 12
    suffix = "pm" if local.hour >= 12 else "am"
    return "%s, %s %d, %d:%02d%s" % (
        _WEEKDAYS[local.weekday()],
        _MONTHS[local.month - 1],
        local.day,
        hour,
        local.minute,
        suffix,
    )


# --------------------------------------------------------------------------------------------
# Runtime and agent context
# --------------------------------------------------------------------------------------------


@dataclass
class Runtime:
    env: Mapping[str, str]
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    prog: str = "simple-chat"  # the command's own name: every error line is prefixed with it
    home: Path | None = None
    sleep: Callable[[float], None] = time.sleep
    stop: threading.Event | None = None
    timeout: float = 30.0
    events_timeout: float = 100.0

    def out(self, text: str) -> None:
        with _OUT_LOCK:
            self.stdout.write(text)
            self.stdout.flush()

    def err(self, text: str) -> None:
        with _OUT_LOCK:
            self.stderr.write(_visible(text) + "\n")  # error text can carry server-supplied names
            self.stderr.flush()


class RecentIds:
    """Bounded set of recently printed message ids (listen runs for days)."""

    def __init__(self, limit: int = 5000) -> None:
        self._ids: OrderedDict[int, None] = OrderedDict()
        self._limit = limit

    def __contains__(self, item: int) -> bool:
        return item in self._ids

    def add(self, item: int) -> None:
        self._ids[item] = None
        while len(self._ids) > self._limit:
            self._ids.popitem(last=False)


class Agent:
    """Everything one command needs: identity, credentials, client, state, caches."""

    registry_problem: str | None = (
        None  # set by _check_agent_identity for `whoami` alone, which reports it
    )

    def __init__(self, rt: Runtime, args: argparse.Namespace) -> None:
        self.rt = rt
        self.args = args
        self.json = bool(getattr(args, "json", False))
        env = rt.env
        rc_arg = getattr(args, "rc", None)
        has_rc = bool(rc_arg or env.get("ZULIP_RC"))
        # Seat precedence (identity.py):  a launcher's AGENT_LAUNCH_SEAT wins and a differing --as,
        # AGENT_SEAT or AGENT_TAG is refused; AGENT_LAUNCHER with no seat refuses everything; then
        # --as, AGENT_SEAT, AGENT_TAG, --default-seat; then the rc file's name.  Before any network
        # command, verify() checks the key's bot against the seat (main does it; whoami reports it).
        resolved = I.resolve_seat(
            env, flag=getattr(args, "as_seat", None), default=getattr(args, "default_seat", None)
        )
        if resolved.problem:
            raise CredentialError(resolved.problem)
        self.seat_source = resolved.source
        self.launcher = resolved.launcher
        seat = Z.normalise_seat(resolved.seat) if resolved.seat else None
        if seat is None and not has_rc:
            raise CredentialError(
                "no seat: set AGENT_SEAT (e.g. CLAUDE), pass --as NAME or --default-seat NAME, or pass --rc"
            )
        session_id = (
            getattr(args, "session", None)
            or env.get("CLAUDE_CODE_SESSION_ID")
            or env.get("AGENT_SESSION")
        )
        self.tag = Z.session_tag(session_id)
        # The FULL session id, kept beside the 8-character tag:  the tag is what a post carries, so
        # only the full id can name the session it came from (and it is written into the session's
        # state file, so a resume-wake can map one to the other).
        self.session_id = session_id
        # `credentials` is set only by `simple-chat mcp`, for an identity whose key comes from its own per-seat
        # environment variables (the server instance, AFC-331);  the CLI's parser never sets it.
        given = getattr(args, "credentials", None)
        self.creds = (
            given
            if isinstance(given, Z.Credentials)
            else Z.resolve_credentials(env, rc_arg=rc_arg, seat=seat, home=rt.home)
        )
        if seat is None:
            rc_path = rc_arg or env.get("ZULIP_RC") or ""
            seat = Z.seat_from_rc_path(rc_path)
            if seat is None:
                raise CredentialError(
                    "cannot tell the seat from %s; pass --as NAME or set AGENT_SEAT" % rc_path
                )
            self.seat_source = "rc file name"
        self.seat = seat
        self.realm = Z.realm_url(env)
        Z.verify_realm(self.creds, self.realm)
        self.client = Z.ZulipClient(
            self.creds,
            self.realm,
            timeout=rt.timeout,
            events_timeout=rt.events_timeout,
            sleep=rt.sleep,
            # The owner id the guard needs (AFC-469):  a direct message may go out only when the
            # owner is in the conversation.  The id is pinned in the installed listener.toml, the
            # same file `dm --owner` and `whoami` read.  With no id pinned the client keeps 0 and
            # the guard fails closed, so a direct message is refused rather than sent unchecked.
            owner_user_id=pinned_owner_id(self) or 0,
        )
        self._verified = False
        self._gate_client()
        self.state = State(state_root(env, rt.home), self.seat, self.tag, session_id=session_id)
        self._me: dict[str, Any] | None = None
        self._users: list[dict[str, Any]] | None = None
        self._users_at = 0.0
        self._stream_ids: dict[str, int] = {}
        # Reentrant:  users() holds it across its request, and the client gate's verify() takes it
        # again through me() on the same thread.
        self._lock = threading.RLock()

    # ---- identity -----------------------------------------------------------------------
    @property
    def tag_prefix(self) -> str:
        """'[SEAT·tag' (no closing bracket): what every post from this session starts with."""
        return "[%s\u00b7%s" % (self.seat, self.tag) if self.tag else "[%s" % self.seat

    def me(self) -> dict[str, Any]:
        with self._lock:
            if self._me is None:
                self._me = self.client.get("users/me")
            return self._me

    def identity_problem(self) -> str | None:
        """None when the key is this seat's own bot (users/me, fetched once per process), else why
        not.  Covers every credential source:  --rc, ZULIP_RC, the seat's file, a file whose name
        derived the seat, and the env triple."""
        return I.check_bot(self.me(), self.seat, self.creds.email)

    def verify(self) -> None:
        """Refuse (exit 3) unless the key is this seat's own bot.  The client gate calls it before
        the first request other than users/me, so nothing is posted, read or changed under a
        mismatched identity, and a command's own argument checks still run before any request."""
        if self._verified:
            return
        problem = self.identity_problem()
        if problem:
            raise CredentialError(problem)
        self._verified = True

    def _gate_client(self) -> None:
        """Route every request but users/me through verify() first (once per process)."""
        request = self.client.request

        def gated(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            if not self._verified and path.strip("/") != "users/me":
                self.verify()
            return request(method, path, *args, **kwargs)

        self.client.request = gated  # type: ignore[method-assign]

    def users(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if self._users is None or (
                refresh and time.monotonic() - self._users_at > USERS_REFRESH_SECONDS
            ):
                self._users = list(self.client.get("users").get("members") or [])
                self._users_at = time.monotonic()
            return self._users

    def stream_id(self, channel: str) -> int:
        channel = Z.resolve_channel(channel, self.rt.env)
        with self._lock:
            known = self._stream_ids.get(channel)
        if known is not None:
            return known
        result = self.client.get("get_stream_id", {"stream": channel})
        stream_id = int(result["stream_id"])
        with self._lock:
            self._stream_ids[channel] = stream_id
        return stream_id

    # ---- output -------------------------------------------------------------------------
    def out(self, text: str) -> None:
        self.rt.out(text)

    def err(self, text: str) -> None:
        self.rt.err(self.rt.prog + ": " + text)

    def emit_json(self, obj: Any) -> None:
        self.out(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---- messages -----------------------------------------------------------------------
    def fetch(
        self,
        narrow: list[dict[str, str]],
        *,
        anchor: Any,
        num_before: int = 0,
        num_after: int = 0,
        include_anchor: bool = True,
    ) -> list[dict[str, Any]]:
        result = self.client.get(
            "messages",
            {
                "narrow": narrow,
                "anchor": anchor,
                "num_before": num_before,
                "num_after": num_after,
                "include_anchor": include_anchor,
                "apply_markdown": False,
            },
        )
        return sorted(result.get("messages") or [], key=lambda m: m.get("id", 0))

    def is_self(self, message: Mapping[str, Any]) -> bool:
        """Posted by this session: in its ledger, or (if the ledger was lost) carrying its exact tag."""
        message_id = message.get("id")
        if isinstance(message_id, int) and message_id in self.state.posted():
            return True
        if self.tag and self.sent_by_this_bot(message):
            # The prefix has no closing bracket, so a bare startswith would take a sibling whose tag
            # merely begins with ours (session ids shorter than 8 characters) for this session.
            content = str(message.get("content") or "")
            return (
                content.startswith(self.tag_prefix + "]")
                or content.startswith(self.tag_prefix + "->")
                or content.startswith(self.tag_prefix + "\u2192")
            )
        return False

    def is_sibling(self, message: Mapping[str, Any]) -> bool:
        return self.sent_by_this_bot(message) and not self.is_self(message)

    def sent_by_this_bot(self, message: Mapping[str, Any]) -> bool:
        return str(message.get("sender_email") or "").casefold() == self.creds.email.casefold()

    def is_mention(self, message: Mapping[str, Any]) -> bool:
        return "mentioned" in (message.get("flags") or [])

    def sender_is_bot(self, message: Mapping[str, Any]) -> bool | None:
        if self.sent_by_this_bot(message):
            return True
        try:
            users = self.users()
        except Z.AgentSyncError:
            with self._lock:
                self._users = []
            return None
        sender_id = message.get("sender_id")
        email = str(message.get("sender_email") or "").casefold()

        def find(members: list[dict[str, Any]]) -> bool | None:
            for user in members:
                if (sender_id is not None and user.get("user_id") == sender_id) or (
                    email and str(user.get("email") or "").casefold() == email
                ):
                    return bool(user.get("is_bot"))
            return None

        found = find(users)
        if found is None:  # a user created after this process started
            try:
                found = find(self.users(refresh=True))
            except Z.AgentSyncError:
                found = None
        return found

    def sender_seat(
        self, message: Mapping[str, Any], is_bot: bool | None
    ) -> tuple[str | None, str | None]:
        """(seat, seat_app) of a message's sender.  The seat is the tag the sender's bot email signs as
        (`identity.seat_tag_for`, never the display name) and exists only for a sender known to be a bot
        with an email;  a person has no seat, and a sender whose kind could not be looked up has none
        either.  The app is F2's `seat_app`, None when the seat is not in the fleet registry (a webhook bot
        such as Sentry's) or the registry cannot be read."""
        email = str(message.get("sender_email") or "").strip()
        if is_bot is not True or not email:
            return None, None
        seat = seat_tag_for(
            {"is_bot": True, "email": email, "full_name": message.get("sender_full_name")}
        )
        try:
            return seat, I.seat_app(seat, registry=I.load_seat_registry(env=self.rt.env))
        except (I.UnknownSeat, I.SeatRegistryError):
            return seat, None

    def message_json(self, message: Mapping[str, Any]) -> dict[str, Any]:
        is_bot = self.sender_is_bot(message)
        seat, seat_app = self.sender_seat(message, is_bot)
        return {
            "id": message.get("id"),
            "sender_id": message.get("sender_id"),
            "client": message.get("client"),
            "channel": message_channel(message),
            "topic": message_topic(message),
            "dm": message.get("type") == "private",
            "sender_email": message.get("sender_email"),
            "sender_full_name": message.get("sender_full_name"),
            "is_bot": is_bot,
            "seat": seat,
            "seat_app": seat_app,
            "timestamp": message.get("timestamp"),
            "content": message.get("content"),
            "flags": list(message.get("flags") or []),
            "sibling": self.is_sibling(message),
            "mentioned": self.is_mention(message),
            "reactions": message_reactions(message),
        }

    def dm_peers(self, message: Mapping[str, Any]) -> str:
        """The other people in a direct message, by display name (this bot left out)."""
        me = self.me().get("user_id")
        recipients = message.get("display_recipient")
        others = (
            [r for r in recipients if isinstance(r, dict) and r.get("id") != me]
            if isinstance(recipients, list)
            else []
        )
        names = [_visible(str(r.get("full_name") or r.get("id")), inline=True) for r in others]
        return ", ".join(names) or "(only this bot)"

    def message_text(self, message: Mapping[str, Any]) -> str:
        is_bot = self.sender_is_bot(message)
        kind = "?" if is_bot is None else ("bot" if is_bot else "human")
        sender = "%s [%s]" % (
            _visible(str(message.get("sender_full_name") or "unknown"), inline=True),
            kind,
        )
        if self.is_sibling(message):
            sender += " (sibling)"
        sender += " \u00b7 " + format_time(float(message.get("timestamp") or 0))
        if self.is_mention(message):
            sender += " \u00b7 @you"
        if message.get("type") == "private":
            head = "DM with %s \u00b7 id %s" % (self.dm_peers(message), message.get("id"))
        else:
            head = "#%s \u203a %s \u00b7 id %s" % (
                _visible(message_channel(message), inline=True),
                _visible(message_topic(message), inline=True),
                message.get("id"),
            )
        return "%s\n%s\n%s\n\n" % (head, sender, _visible(str(message.get("content") or "")))

    def emit(self, message: Mapping[str, Any]) -> None:
        if self.json:
            self.emit_json(self.message_json(message))
        else:
            self.out(self.message_text(message))


def _visible(text: str, *, inline: bool = False) -> str:
    """Show control characters (other than newline and tab) as \\xNN.  With `inline`, a newline is
    shown too, for names that sit inside one line of output."""
    shown = _CONTROL_RE.sub(lambda match: "\\x%02x" % ord(match.group()), text)
    return shown.replace("\n", "\\x0a") if inline else shown


# --------------------------------------------------------------------------------------------
# Scopes, delivery and backfill (shared by read --new, wait and listen)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What a cursor tracks: one topic of a channel, or (topic None) mentions of this bot."""

    channel: str
    topic: str | None

    @property
    def cursor_args(self) -> tuple[str, str]:
        return (MENTIONS_CHANNEL, "") if self.topic is None else (self.channel, self.topic)

    @property
    def narrow(self) -> list[dict[str, str]]:
        if self.topic is None:
            return [{"operator": "is", "operand": "mentioned"}]
        return topic_narrow(self.channel, self.topic)

    def matches(self, message: Mapping[str, Any]) -> bool:
        if self.topic is None:
            return "mentioned" in (message.get("flags") or [])
        return (
            message_channel(message).casefold() == self.channel.casefold()
            and message_topic(message).casefold() == self.topic.casefold()
        )


def topic_narrow(channel: str, topic: str) -> list[dict[str, str]]:
    return [{"operator": "channel", "operand": channel}, {"operator": "topic", "operand": topic}]


class Delivery:
    """Prints each deliverable message once and moves the cursors forward.

    A message is skipped when this session already saw it (its id is at or below the scope's
    cursor, or it was printed in this process), and when this session posted it.  Messages from
    sibling sessions of the same seat are delivered and labelled."""

    def __init__(
        self,
        agent: Agent,
        *,
        include_self: bool = False,
        max_messages: int | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.agent = agent
        self.include_self = include_self
        self.max_messages = max_messages
        self.stop = stop
        self.count = 0
        self.first_run: set[tuple[str, str]] = (
            set()
        )  # scopes that had no cursor when this process started
        self._recent = RecentIds()
        self._lock = threading.Lock()

    def _is_self(self, message: Mapping[str, Any], live: bool) -> bool:
        agent = self.agent
        if agent.is_self(message):
            return True
        if live and agent.sent_by_this_bot(message):
            deadline = time.monotonic() + SELF_GRACE
            while time.monotonic() < deadline:
                time.sleep(0.05)
                if message.get("id") in agent.state.posted():
                    return True
        return False

    def deliver(
        self,
        messages: Sequence[Mapping[str, Any]],
        scopes: Sequence[Scope],
        *,
        assume: Scope | None = None,
        live: bool = False,
    ) -> int:
        """Print the deliverable messages; returns how many were printed.  `assume` says every
        message belongs to that scope (it came from the scope's own server-side narrow).  `live`
        marks event-stream messages: one this bot just sent can reach the stream before `post`
        has written its id to the ledger, so it gets a short grace period before it is called a
        sibling."""
        printed = 0
        state = self.agent.state
        with self._lock:
            for message in sorted(messages, key=lambda m: m.get("id", 0)):
                message_id = message.get("id")
                if not isinstance(message_id, int):
                    continue
                matched = (
                    [assume] if assume is not None else [s for s in scopes if s.matches(message)]
                )
                if not matched:
                    continue
                fresh = False
                for scope in matched:
                    cursor = state.cursor(*scope.cursor_args)
                    # On a scope's first run the queue was registered before the baseline cursor was
                    # set, so every live event in it arrived after this command started: new, even
                    # when its id is at or below the baseline.
                    if (
                        cursor is None
                        or message_id > cursor
                        or (live and scope.cursor_args in self.first_run)
                    ):
                        fresh = True
                show = (
                    fresh
                    and message_id not in self._recent
                    and (self.include_self or not self._is_self(message, live))
                )
                if show:
                    # Print first and move the cursor after: if the print fails (a closed pipe), the
                    # message stays unseen and the next read delivers it.  At least once, never zero.
                    self.agent.emit(message)
                    self._recent.add(message_id)
                for scope in matched:
                    state.advance_cursor(*scope.cursor_args, message_id)
                if not show:
                    continue
                printed += 1
                self.count += 1
                if (
                    self.max_messages is not None
                    and self.count >= self.max_messages
                    and self.stop is not None
                ):
                    self.stop.set()
                    break
        return printed


def baseline_or_backfill(
    agent: Agent, scope: Scope, delivery: Delivery, limit: int = BACKFILL_LIMIT
) -> list[dict[str, Any]]:
    """Messages newer than the saved cursor.  With no cursor yet, sets it to the newest message and
    returns nothing (history is not replayed), and marks the scope as a first run on `delivery`."""
    cursor = agent.state.cursor(*scope.cursor_args)
    if cursor is None:
        delivery.first_run.add(scope.cursor_args)
        newest = agent.fetch(scope.narrow, anchor="newest", num_before=1)
        if newest:
            agent.state.advance_cursor(*scope.cursor_args, max(m["id"] for m in newest))
        return []
    return agent.fetch(scope.narrow, anchor=cursor, num_after=limit, include_anchor=False)


def backfill_pages(
    agent: Agent,
    scope: Scope,
    delivery: Delivery,
    scopes: Sequence[Scope],
    *,
    until_printed: bool,
    stop: threading.Event | None = None,
) -> int:
    """Deliver what is newer than the scope's cursor, one page of BACKFILL_LIMIT at a time, and keep
    going while a page is full.  A page can hold nothing deliverable (a hundred of this session's own
    posts), so stopping after one page would hide the peer message behind it.  With `until_printed`
    it stops after the first page that printed something.  Returns how many messages were printed."""
    total = 0
    while stop is None or not stop.is_set():
        before = agent.state.cursor(*scope.cursor_args)
        batch = baseline_or_backfill(agent, scope, delivery)
        printed = delivery.deliver(batch, scopes, assume=scope)
        total += printed
        if (until_printed and printed) or len(batch) < BACKFILL_LIMIT:
            break
        if agent.state.cursor(*scope.cursor_args) == before:
            break  # a full page that did not move the cursor would repeat for ever
    return total


# --------------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------------


def _topic(value: str) -> str:
    topic = value.strip()
    if not topic:
        raise argparse.ArgumentTypeError("topic must not be empty")
    if len(topic) > Z.MAX_TOPIC_LENGTH:
        raise argparse.ArgumentTypeError(
            "topic is %d characters; Zulip allows at most %d" % (len(topic), Z.MAX_TOPIC_LENGTH)
        )
    return topic


def _channel(value: str) -> str:
    channel = value.strip().lstrip("#")
    if not channel:
        raise argparse.ArgumentTypeError("channel must not be empty")
    return channel


def _resolve_channel_args(
    args: argparse.Namespace, env: Mapping[str, str] | None
) -> argparse.Namespace:
    """A channel name the caller typed resolves to the channel it means.

    The realm renamed the fleet's coordination channel and left the old name behind as a
    tombstone stream, so a caller that still names it has to land in the live channel.
    The name is resolved once, here, so every command below works with the channel the
    fleet reads.
    """
    for name in ("channel", "new_channel"):
        value = getattr(args, name, None)
        if isinstance(value, str):
            setattr(args, name, Z.resolve_channel(value, env))
        elif isinstance(value, list):
            setattr(args, name, [Z.resolve_channel(v, env) for v in value])
    return args


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a whole number" % value) from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _message_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a message id" % value) from None
    if number < 0:
        raise argparse.ArgumentTypeError("a message id cannot be negative")
    return number


def _seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number of seconds" % value) from None
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def _common_parent() -> argparse.ArgumentParser:
    """Flags every command accepts.  Defaults are SUPPRESS so the flags work both before and
    after the command name without one overwriting the other; read them with getattr."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("identity and output")
    group.add_argument(
        "--as",
        dest="as_seat",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="seat name (default: env AGENT_SEAT, then AGENT_TAG).  Under a launcher, "
        "AGENT_LAUNCH_SEAT wins and a different --as is refused",
    )
    group.add_argument(
        "--default-seat",
        dest="default_seat",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="the platform default seat for an ordinary session (an MCP registration or a wrapper "
        "passes it):  used only when --as, AGENT_SEAT and AGENT_TAG are unset, and never under a "
        "launcher (AGENT_LAUNCHER)",
    )
    group.add_argument(
        "--rc",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="zuliprc file to use (default: env ZULIP_RC, then the seat's own keys in Infisical Fleet-OPS /zulip)",
    )
    group.add_argument(
        "--session",
        metavar="ID",
        default=argparse.SUPPRESS,
        help="session id (default: env CLAUDE_CODE_SESSION_ID, then AGENT_SESSION)",
    )
    group.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output: one JSON object per message per line",
    )
    return parent


class _NullParser(argparse.ArgumentParser):
    """A parser that accepts and ignores every registration.

    `build_parser` builds one parser per command out of the same registration code, so a verb that
    belongs to another command (`simple-notes` has no `post`) is registered into one of these and
    silently dropped instead of needing an `if` at every call site."""

    def __init__(self) -> None:
        super().__init__(prog="", add_help=False)

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return _NullParser()

    def add_subparsers(self, **kwargs: Any) -> argparse.ArgumentParser:
        return self

    def add_argument(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_argument_group(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def add_mutually_exclusive_group(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def set_defaults(self, **kwargs: Any) -> None:
        return None


class _FilteringSubparsers:
    """Wraps argparse's subparsers action so only `allowed` top-level verbs survive.

    A verb outside the set is registered into a `_NullParser`, so the shared registration code in
    `build_parser` needs no per-verb branching and each command's `--help` lists only its own
    verbs."""

    def __init__(self, sub: Any, allowed: frozenset[str]) -> None:
        self._sub = sub
        self._allowed = allowed

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        if name not in self._allowed:
            return _NullParser()
        return self._sub.add_parser(name, **kwargs)

    def add_subparsers(self, **kwargs: Any) -> Any:
        return self._sub.add_subparsers(**kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._sub, item)


def build_parser(
    prog: str = "simple-chat",
    verbs: frozenset[str] | None = None,
    *,
    listener_verbs: frozenset[str] | None = None,
    notes_verbs: frozenset[str] | None = None,
    channel_default: str = Z.DEFAULT_CHANNEL,
) -> argparse.ArgumentParser:
    common = _common_parent()
    parser = argparse.ArgumentParser(
        prog=prog,
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Coordinate with the fleet on Zulip.  Every post goes to a channel and a topic, "
        "and the default channel is %s." % channel_default,
        epilog="Exit codes: 0 ok, 2 usage, 3 credentials or realm, 4 wait timeout, 5 Zulip API error, "
        "6 network error, 7 verify-owner-msg did not verify.  See scripts/agent_sync/README.md.",
    )
    parser.add_argument("--version", action="version", version="%s %s" % (prog, __version__))
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    # The wrapper spans every source that registers onto `sub`, so a verb this command owns from
    # any of them survives; each source narrows its own registration further where it needs to.
    owned = [v for v in (verbs, listener_verbs, notes_verbs) if v is not None]
    if owned:
        sub = _FilteringSubparsers(sub, frozenset().union(*owned))

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        # The chat verbs narrow to their own set:  `read` is a verb of simple-notes too (AFC-486), and the
        # filter above is the union of every source's verbs.
        if verbs is not None and name not in verbs:
            return _NullParser()
        return sub.add_parser(name, parents=[common], help=summary, description=summary)

    def channel_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--channel",
            type=_channel,
            default=channel_default,
            metavar="C",
            help="channel name (default: %(default)s)",
        )

    def topic_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--topic",
            type=_topic,
            required=True,
            metavar="T",
            help="topic (required, at most 60 characters)",
        )

    def body_file_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--body-file",
            dest="body_file",
            metavar="PATH",
            default=None,
            help="read the message from PATH instead of TEXT (not both):  a regular file you own, mode 0600 "
            "or 0400, UTF-8, at most %d KB.  For a caller that cannot pipe stdin"
            % (bodyfile.MAX_BYTES // 1024),
        )

    add("whoami", "show the bot, seat, session tag, credential source and realm (never the key)")
    add(
        "identities",
        "list the bots this host may act as (every agent bot whose credential file is here, or the "
        "launcher's seat alone) and the default;  no network, never a key",
    )
    p = add("channels", "list subscribed channels (--all:  every channel this bot can see)")
    p.add_argument(
        "--all",
        action="store_true",
        help="every channel this bot can see, public ones it is not in included",
    )

    p = add("topics", "list recent topics of a channel, newest first")
    channel_opt(p)
    p.add_argument(
        "--limit",
        type=_positive_int,
        default=30,
        metavar="N",
        help="topics to show (default: %(default)s)",
    )

    p = add("subscribe", "subscribe the bot to channels")
    p.add_argument(
        "--channel",
        type=_channel,
        action="append",
        required=True,
        metavar="C",
        help="channel to join (repeatable)",
    )
    p.add_argument(
        "--must-exist",
        action="store_true",
        help="refuse a channel that cannot be found (or is not visible to the bot), so a typo cannot create one",
    )

    p = add("post", "post to a channel topic")
    topic_opt(p)
    channel_opt(p)
    p.add_argument(
        "--to",
        action="append",
        default=[],
        metavar="NAME",
        help="address a user or bot by exact full name, email, seat tag (CODEX, MA) or user id (repeatable); adds the @-mention that wakes it",
    )
    p.add_argument(
        "--fleet",
        action="store_true",
        help="fleet-wide wake:  also add @**all**.  Only in #Work-In-Progress, topic 'fleet'.  It notifies every "
        "seat and the owner, so use it only when every seat must act.  Zulip refuses it from a bot the "
        "realm's can_mention_many_users_group does not include;  then use --to for each bot that "
        "must act",
    )
    p.add_argument(
        "--to-owner",
        dest="to_owner",
        action="store_true",
        help="address the owner pinned in listener.toml (daemon.owner_user_id):  adds the mention that wakes "
        "Jay, written from that id's live full name, never a name you type.  Exits 2 when no owner is "
        "pinned",
    )
    p.add_argument(
        "--no-tag", action="store_true", help="do not prepend the [SEAT\u00b7session] tag"
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="prefix the eyes marker, so the message stays readable in Zulip and wakes nobody (AFC-468)",
    )
    body_file_opt(p)
    p.add_argument(
        "text",
        nargs="*",
        metavar="TEXT",
        help="message text; a single '-' reads it from stdin;  omit it with --body-file",
    )

    p = add("reply", "reply in the channel and topic of an existing message")
    p.add_argument(
        "--id", type=_message_id, required=True, metavar="MSGID", help="message to answer"
    )
    p.add_argument(
        "--to",
        action="append",
        default=[],
        metavar="NAME",
        help="address a user or bot (repeatable)",
    )
    p.add_argument(
        "--to-owner",
        dest="to_owner",
        action="store_true",
        help="address the owner pinned in listener.toml (daemon.owner_user_id);  exits 2 when none is pinned",
    )
    p.add_argument("--no-tag", action="store_true", help="do not prepend the tag")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="prefix the eyes marker, so the message stays readable in Zulip and wakes nobody (AFC-468)",
    )

    body_file_opt(p)
    p.add_argument(
        "text",
        nargs="*",
        metavar="TEXT",
        help="message text; a single '-' reads it from stdin;  omit it with --body-file",
    )

    p = add(
        "dm",
        "send a direct message from this seat's bot:  to the owner (the default), to named peers, or to a group of up to %d"
        % DM_MAX_RECIPIENTS,
    )
    p.add_argument(
        "--to",
        action="append",
        default=[],
        metavar="NAME",
        help="a recipient by exact full name, email, seat tag (CODEX, MA, GB-Compiler) or user id "
        "(repeatable, at most %d, one conversation for all of them).  Refused: webhook and "
        "integration bots, deactivated users, this bot itself" % DM_MAX_RECIPIENTS,
    )
    p.add_argument(
        "--owner",
        action="store_true",
        help="send to the owner pinned in listener.toml (daemon.owner_user_id), never a name you type.  "
        "The default when no --to is given;  with --to it adds the owner to the group",
    )
    p.add_argument(
        "--clarifying-question",
        dest="clarifying_question",
        action="store_true",
        help="Operator only:  true when this DM is the one clarifying question it may open about a message "
        "whose addressee could not be resolved to a single seat (owner decision, Oct 22 2026).  Every other "
        "seat is refused when it opens a new DM;  a reply inside a DM Operator opened needs no flag.",
    )
    p.add_argument(
        "--no-tag", action="store_true", help="do not prepend the [SEAT\u00b7session\u2192TO] tag"
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="prefix the eyes marker, so the message stays readable in Zulip and wakes nobody (AFC-468)",
    )
    body_file_opt(p)
    p.add_argument(
        "text",
        nargs="*",
        metavar="TEXT",
        help="message text; a single '-' reads it from stdin;  omit it with --body-file",
    )

    p = add(
        "dm-read",
        "read one direct-message conversation of this bot (bodies are untrusted data, like any message)",
    )
    p.add_argument(
        "--with",
        dest="with_",
        action="append",
        required=True,
        metavar="NAME",
        help="the other person in the conversation, named like dm --to (repeatable for a group;  "
        "'owner' is the owner pinned in listener.toml)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--since", type=_message_id, metavar="ID", help="messages after this id")
    group.add_argument(
        "--new",
        action="store_true",
        help="messages after this session's saved cursor, then advance it",
    )
    p.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="messages to show (default: %(default)s)",
    )
    p.add_argument(
        "--exclude-self",
        action="store_true",
        help="hide the messages this session posted "
        "(they are shown by default:  a conversation reads badly without them)",
    )

    p = add(
        "verify-owner-msg",
        "check that one message really is from the owner (exit 0 verified, 7 not verified)",
    )
    p.description = (
        "Verified only if ALL of these hold:  the sender is the owner pinned in the installed listener.toml "
        "(daemon.owner_user_id);  the message's client is one of that file's daemon.owner_clients (website, "
        "ZulipMobile, ZulipFlutter, ZulipElectron, ZulipDesktop by default);  the message is where you expect it "
        "(--topic in --channel, or --dm-with, a direct message between exactly the owner and that peer);  and its id "
        "is greater than --after.  The client name is a heuristic that keeps an API script that holds the owner's "
        "account out;  it is not authentication.  Prints the verdict and the reasons, never the message text.  "
        "Exits 7 when not verified, 2 when no owner is pinned."
    )
    p.add_argument(
        "message_id", type=_message_id, metavar="MESSAGE_ID", help="the message to check"
    )
    p.add_argument(
        "--channel",
        type=_channel,
        default=channel_default,
        metavar="C",
        help="channel the topic is in (default: %(default)s)",
    )
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--topic",
        type=_topic,
        metavar="T",
        help="the message must be in this topic of --channel (a resolved topic, with its check mark, counts)",
    )
    where.add_argument(
        "--dm-with",
        dest="dm_with",
        metavar="NAME",
        help="the message must be a direct message between exactly the owner and this peer (a seat tag "
        "such as CODEX, a name, an email or a user id);  a group conversation never verifies",
    )
    p.add_argument(
        "--after",
        type=_message_id,
        default=None,
        metavar="ID",
        help="the message must be newer than this message id (an id, never a timestamp)",
    )

    p = add("read", "read the history of one topic")
    topic_opt(p)
    channel_opt(p)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--since", type=_message_id, metavar="ID", help="messages after this id")
    group.add_argument(
        "--new",
        action="store_true",
        help="messages after this session's saved cursor, then advance it",
    )
    p.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="messages to show (default: %(default)s)",
    )
    p.add_argument(
        "--include-self", action="store_true", help="also show messages this session posted"
    )

    p = add("wait", "block until a new message from someone else arrives in a topic")
    topic_opt(p)
    channel_opt(p)
    p.add_argument(
        "--timeout",
        type=_seconds,
        default=300.0,
        metavar="SECONDS",
        help="give up after this long and exit 4 (default: %(default)s)",
    )

    p = add("listen", "stream new messages until killed (for a Monitor tool)")
    p.add_argument(
        "--topic",
        type=_topic,
        action="append",
        default=[],
        metavar="T",
        help="topic to follow (repeatable)",
    )
    channel_opt(p)
    p.add_argument(
        "--mentions", action="store_true", help="also print @-mentions of this bot anywhere"
    )
    p.add_argument(
        "--max-messages",
        type=_positive_int,
        default=None,
        metavar="N",
        help="exit after printing N messages",
    )

    p = add(
        "inbox",
        "show @-mentions of this bot and direct messages sent to it, newer than the seat-wide inbox cursor",
    )
    p.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="messages to show (default: %(default)s)",
    )
    p.add_argument("--peek", action="store_true", help="do not advance the cursor")
    p.add_argument(
        "--local",
        action="store_true",
        help="read the listener's seat inbox file and owner queue instead of Zulip (no network)",
    )

    for name, summary in (
        ("follow", "follow a topic (notifications on)"),
        ("mute", "mute a topic"),
        ("unmute", "return a topic to the channel's default visibility"),
    ):
        p = add(name, summary)
        topic_opt(p)
        channel_opt(p)

    p = add("resolve", "mark a topic resolved by prefixing it with a check mark")
    topic_opt(p)
    channel_opt(p)

    p = add("react", "add an emoji reaction to a message")
    p.add_argument(
        "--id", type=_message_id, required=True, metavar="MSGID", help="message to react to"
    )
    p.add_argument("emoji", metavar="EMOJI", help="emoji name, for example eyes or thumbs_up")

    p = add(
        "mcp",
        "serve the agent-sync tools over MCP on stdin and stdout (an MCP client's config runs this, not a person)",
    )
    p.add_argument(
        "--codex-thread-meta",
        action="store_true",
        help="CODEX-only: attribute calls from native MCP _meta.threadId; no automatic nosession fallback",
    )

    zulip_cli.add_parsers(sub, common, channel_default)
    listener_cli.add_parsers(sub, common, verbs=listener_verbs)
    notes_cli.add_parsers(sub, common, verbs=notes_verbs)
    return parser


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------

MAX_REACTIONS_SHOWN = 100


def message_reactions(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The reactions on a message as ids and emoji codes:  who reacted (`user_id`), and which emoji
    (`reaction_type`, `emoji_code`, `emoji_name`).  Nothing else Zulip sends with a reaction is passed on.  A
    reaction is a client-free record, so it proves who tapped but not through which app;  a consumer that
    counts one as the owner's compares `user_id` with the pinned owner (`whoami --json` `owner_user_id`), and
    matches an emoji by `reaction_type` `unicode_emoji` plus `emoji_code`, because a realm can add a custom
    emoji named like a standard one."""
    shown: list[dict[str, Any]] = []
    for reaction in message.get("reactions") or []:
        if not isinstance(reaction, Mapping):
            continue
        user_id = reaction.get("user_id")
        if not _is_int(user_id):
            user = reaction.get("user")  # servers before Zulip 3.0 carry only the nested user
            user_id = user.get("id") if isinstance(user, Mapping) else None
        if not _is_int(user_id):
            continue
        shown.append(
            {
                "user_id": user_id,
                "reaction_type": reaction.get("reaction_type")
                if isinstance(reaction.get("reaction_type"), str)
                else None,
                "emoji_code": reaction.get("emoji_code")
                if isinstance(reaction.get("emoji_code"), str)
                else None,
                "emoji_name": reaction.get("emoji_name")
                if isinstance(reaction.get("emoji_name"), str)
                else None,
            }
        )
        if len(shown) >= MAX_REACTIONS_SHOWN:
            break
    return shown


def pinned_owner_id(agent: Agent) -> int | None:
    """`daemon.owner_user_id` of the installed listener.toml, or None when it is not pinned or cannot be read.
    The same file `dm --owner`, `post --to-owner` and `verify-owner-msg` read."""
    try:
        owner = int(listener_cli._load(agent.rt).owner_user_id)
    except Exception:  # noqa: BLE001 - a missing or damaged config is "not pinned" here, never a crash of whoami
        return None
    return owner if owner > 0 else None


def pinned_operator_id(agent: Agent) -> int | None:
    """`daemon.operator_user_id` of the installed listener.toml, or None when it is not pinned.

    The Operator service bot's own Zulip user id, pinned in the host's own config the same way the
    owner's id is:  a production user id is private inventory, so none is committed and the host
    decides.  It is the only proof that the Operator opened a direct message, so a host that pins
    it lets a seat answer inside one (Kody review on AFC-473).

    **A damaged setting is not an unpinned one.**  A listener.toml that cannot be read, or whose
    `operator_user_id` is not a user id, used to come back as None, so the refusal named an
    environment variable that is not the one at fault and the recoverable setup error stayed
    hidden.  It fails closed here instead, with a UsageError that names the setting the operator
    has to repair and no id value in the message (Kody review on AFC-473)."""
    try:
        cfg = listener_cli._load(agent.rt)
    except Exception as exc:  # noqa: BLE001 - a damaged config is a named failure, never a guess
        raise UsageError(
            "%s is unavailable:  the listener config could not be read (%s)"
            % (OPERATOR_ID_SETTING, type(exc).__name__)
        ) from None
    for problem in cfg.errors:
        # The two ways the setting itself is unusable:  a value that is not a user id, and a
        # listener.toml that could not be read or parsed.  A host that simply has not pinned the
        # id (0, or no listener.toml at all) is not a failure and comes back as None.
        if "operator_user_id" in problem or problem.startswith("cannot read "):
            raise UsageError("%s is unavailable:  %s" % (OPERATOR_ID_SETTING, problem)) from None
    operator = cfg.operator_user_id
    return operator if operator > 0 else None


def cmd_whoami(agent: Agent, args: argparse.Namespace) -> int:
    """Both identities:  the seat this session resolved, and the bot its key authenticates as.
    Exits 3 when they differ (the key is printed by no path)."""
    me = agent.me()
    problem = agent.identity_problem()
    info = {
        "full_name": me.get("full_name"),
        "email": me.get("email") or agent.creds.email,
        "user_id": me.get("user_id"),
        "is_bot": me.get("is_bot"),
        "seat": agent.seat,
        "bot_seat": seat_tag_for(me),
        "seat_source": agent.seat_source,
        "launcher": agent.launcher,
        "verified": problem is None,
        "problem": problem,
        "session": agent.tag,
        "session_id": agent.session_id,
        "credential": agent.creds.source,
        "realm": agent.realm,
        "owner_user_id": pinned_owner_id(agent),
        "registry_problem": agent.registry_problem,
    }
    if agent.registry_problem:
        info["verified"] = False
    if agent.json:
        agent.emit_json(info)
    else:
        rows = [
            ("bot", info["full_name"]),
            ("email", info["email"]),
            ("user id", info["user_id"]),
            ("bot seat", info["bot_seat"]),
            ("seat", "%s (%s)" % (agent.seat, agent.seat_source)),
            ("launcher", agent.launcher or "none (an ordinary session)"),
            (
                "verified",
                "yes:  the key is this seat's own bot"
                if problem is None and not agent.registry_problem
                else "NO",
            ),
            ("session", agent.tag or "none (state is shared under nosession)"),
            ("credential", agent.creds.source),
            ("realm", agent.realm),
            ("owner id", info["owner_user_id"] or "not pinned in listener.toml"),
        ]
        if agent.registry_problem:
            rows.append(("registry", agent.registry_problem))
        agent.out("".join("%-11s %s\n" % (label + ":", value) for label, value in rows))
    if problem or agent.registry_problem:
        agent.err(problem or agent.registry_problem)
        return 3
    return 0


def cmd_channels(agent: Agent, args: argparse.Namespace) -> int:
    if getattr(args, "all", False):
        rows = zulip_ops.channels(agent, include_all=True)
        if agent.json:
            agent.emit_json(rows)
            return 0
        for row in rows:
            description = str(row.get("description") or "").strip()
            agent.out(
                "#%s%s%s\n"
                % (
                    _visible(str(row.get("name")), inline=True),
                    "" if row["public"] else " (private)",
                    "" if row["subscribed"] else " (not subscribed)",
                )
                + (
                    ""
                    if not description
                    else "  \u2014 " + _visible(description, inline=True) + "\n"
                )
            )
        return 0
    subs = sorted(
        agent.client.get("users/me/subscriptions").get("subscriptions") or [],
        key=lambda s: str(s.get("name", "")).casefold(),
    )
    if agent.json:
        agent.emit_json(
            [
                {
                    "name": s.get("name"),
                    "description": s.get("description"),
                    "stream_id": s.get("stream_id"),
                }
                for s in subs
            ]
        )
        return 0
    for sub in subs:
        description = str(sub.get("description") or "").strip()
        agent.out(
            "#%s%s\n"
            % (
                _visible(str(sub.get("name")), inline=True),
                (" \u2014 " + _visible(description, inline=True)) if description else "",
            )
        )
    return 0


def cmd_topics(agent: Agent, args: argparse.Namespace) -> int:
    stream_id = agent.stream_id(args.channel)
    topics = agent.client.get("users/me/%d/topics" % stream_id).get("topics") or []
    topics = sorted(topics, key=lambda t: t.get("max_id", 0), reverse=True)[: args.limit]
    if agent.json:
        agent.emit_json([{"name": t.get("name"), "max_id": t.get("max_id")} for t in topics])
        return 0
    agent.out(
        "".join(
            "%10s  %s\n" % (t.get("max_id"), _visible(str(t.get("name")), inline=True))
            for t in topics
        )
    )
    return 0


def cmd_subscribe(agent: Agent, args: argparse.Namespace) -> int:
    channels = list(dict.fromkeys(args.channel))
    if args.must_exist:
        for channel in channels:
            try:
                agent.stream_id(channel)
            except ApiError as exc:
                if exc.status == 400:
                    raise UsageError(
                        "channel %r was not found, or is not visible to this bot; --must-exist refuses to "
                        "subscribe (and so to create) it" % channel
                    ) from None
                raise
    result = agent.client.post(
        "users/me/subscriptions", {"subscriptions": [{"name": c} for c in channels]}
    )
    joined = sorted({n for names in (result.get("subscribed") or {}).values() for n in names})
    already = sorted(
        {n for names in (result.get("already_subscribed") or {}).values() for n in names}
    )
    denied = list(result.get("unauthorized") or [])
    if agent.json:
        agent.emit_json(
            {"subscribed": joined, "already_subscribed": already, "unauthorized": denied}
        )
    else:
        for name in joined:
            agent.out("subscribed to #%s\n" % _visible(name, inline=True))
        for name in already:
            agent.out("already subscribed to #%s\n" % _visible(name, inline=True))
        for name in denied:
            agent.out("not allowed to join #%s\n" % _visible(str(name), inline=True))
    return 0 if not denied else 5


def _check_text_args(args: argparse.Namespace) -> None:
    """Exactly one source for the message:  TEXT (or `-` for stdin) or `--body-file`.  Runs before any
    credential or network use, so the usage error is the one a caller sees.  An object that does not carry
    `body_file` (the MCP layer's hand-built namespaces) has none."""
    parts = list(getattr(args, "text", None) or [])
    body_file = getattr(args, "body_file", None)
    if body_file is not None and parts:
        raise UsageError(
            "--body-file and the message TEXT are exclusive:  give the text or the file, not both"
        )
    if body_file is None and not parts:
        raise UsageError("no message text:  give TEXT, '-' to read stdin, or --body-file PATH")


def _scan_refusal(agent: Agent, **fields: str) -> None:
    """Refuse caller text that looks like a secret, naming the field and the kind, never the value.

    The MCP side scans every field it posts;  the CLI scans the message body, and the topic and the
    channel are caller text as well, so they are scanned here too (Kody review on AFC-473).  The
    message names the kind, never a prefix of the value and never the value itself, because the
    refusal can reach a log."""
    known = secretscan.basic_forms(agent.creds.email, agent.creds.key)
    for field, value in fields.items():
        kind = secretscan.scan(value or "", known)
        if kind:
            raise UsageError("refusing to post:  the %s looks like it contains %s" % (field, kind))


def _read_text(agent: Agent, parts: Sequence[str], body_file: str | None = None) -> str:
    _check_text_args(argparse.Namespace(text=list(parts), body_file=body_file))
    if body_file is not None:
        text = bodyfile.read_body_file(body_file)
    else:
        text = agent.rt.stdin.read() if list(parts) == ["-"] else " ".join(parts)
    text = text.strip()
    if not text:
        raise UsageError("the message text is empty")
    _scan_refusal(agent, text=text)
    return text


def _match_peer(users: Sequence[Mapping[str, Any]], raw: str) -> list[Mapping[str, Any]]:
    """The users that `raw` names, in the order the CLI tries:  an exact full name or an email, then a
    seat tag (`MA` finds muse-assist-bot@, `GB-Compiler` finds compiler-grok-bot@), then a user id."""
    needle = raw.strip().casefold()
    found = [
        u
        for u in users
        if str(u.get("full_name") or "").casefold() == needle
        or needle
        in (str(u.get("email") or "").casefold(), str(u.get("delivery_email") or "").casefold())
    ]
    if not found:
        # Every spelling of the retired GROK-BUILD tag (GROK-BUILD, Grok-Build, "Grok (Build)" ...) is the
        # GROK seat (owner 2026-10-10), so it finds grok-build-bot@ whatever the bot's display name is now.
        seat_needle = I.canonical_seat(raw).casefold()
        found = [
            u
            for u in users
            if u.get("is_bot") and seat_tag_for(u).casefold() in (needle, seat_needle)
        ]
    if not found and needle.isascii() and needle.isdigit():
        found = [u for u in users if u.get("user_id") == int(needle)]
    return found


def _lookup_peer(agent: Agent, active: Sequence[Mapping[str, Any]], raw: str) -> Mapping[str, Any]:
    found = _match_peer(active, raw)
    if not found:
        close = difflib.get_close_matches(
            raw.strip().casefold(),
            [str(u["full_name"]).casefold() for u in active],
            n=5,
            cutoff=0.0,
        )
        by_fold = {str(u["full_name"]).casefold(): str(u["full_name"]) for u in active}
        names_hint = ", ".join(by_fold[c] for c in close) or "(no users visible)"
        raise UsageError("no user or bot named %r; the 5 closest names: %s" % (raw, names_hint))
    if len(found) > 1:
        raise UsageError(
            "%r matches %d users; use an email: %s"
            % (raw, len(found), ", ".join(str(u.get("email")) for u in found))
        )
    return found[0]


def _active_users(agent: Agent) -> list[dict[str, Any]]:
    return [u for u in agent.users() if u.get("is_active", True) and u.get("full_name")]


def _mention_for(active: Sequence[Mapping[str, Any]], user: Mapping[str, Any]) -> str:
    same_name = sum(
        1 for u in active if str(u["full_name"]).casefold() == str(user["full_name"]).casefold()
    )
    return (
        "@**%s**" % user["full_name"]
        if same_name == 1
        else "@**%s|%s**" % (user["full_name"], user["user_id"])
    )


def _resolve_peers(agent: Agent, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (@-mentions, tag labels) for `--to NAME` arguments."""
    mentions: list[str] = []
    labels: list[str] = []
    if not names:
        return mentions, labels
    active = _active_users(agent)
    for raw in names:
        user = _lookup_peer(agent, active, raw)
        mentions.append(_mention_for(active, user))
        labels.append(seat_tag_for(user))
    return mentions, labels


def _compose(
    agent: Agent,
    text: str,
    *,
    labels: Sequence[str],
    mentions: Sequence[str],
    no_tag: bool,
    quiet: bool = False,
) -> str:
    parts: list[str] = []
    if quiet:
        # The owner's per-message no-wake signal:  the message stays readable in Zulip and wakes
        # nobody.  It goes before the seat tag, because the tag is what the runtime prepends and
        # the marker has to be the first thing the reader sees (AFC-468).
        parts.append(ROUTER.NO_WAKE_MARKER)
    if not no_tag:
        parts.append(
            agent.tag_prefix + ("\u2192" + ",".join(labels) if labels else "") + "]"
        )  # → canonical, -> accepted
    parts.extend(mentions)
    parts.append(text)
    body = " ".join(parts)
    if len(body) > MAX_CONTENT_LENGTH:
        raise UsageError(
            "the message is %d characters; Zulip allows at most %d"
            % (len(body), MAX_CONTENT_LENGTH)
        )
    return body


def _check_hint(channel: str, topic: str, prog: str = "simple-chat") -> str:
    """The command that shows whether a post landed.  Channel and topic can come from another
    member's message (reply), so they are shell-quoted: an agent that runs the hint runs only that."""
    return _visible(
        "%s read --channel %s --topic %s --include-self"
        % (prog, shlex.quote(channel), shlex.quote(topic))
    )


def _reformatted(agent: Agent, body: str) -> list[str]:
    """What the outbound fixes changed in `body` (textfmt.fix_outbound, owner 2026-10-19).  In text mode each line goes
    to stderr, so the agent sees what it should have written;  JSON output carries the list as `reformatted`."""
    report = textfmt.fix_outbound(body, limit=MAX_CONTENT_LENGTH)[1]
    if not agent.json:
        for line in report:
            agent.err("reformatted:  " + line)
    return report


def _send(agent: Agent, channel: str, topic: str, body: str) -> int:
    try:
        result = agent.client.post(
            "messages", {"type": "stream", "to": channel, "topic": topic, "content": body}
        )
    except NetworkError as exc:
        if exc.maybe_sent:
            raise NetworkError(
                "%s  The message was not retried.  Check whether it posted: %s"
                % (exc, _check_hint(channel, topic, agent.rt.prog)),
                timeout=exc.timeout,
                maybe_sent=True,
            ) from None
        raise
    except ApiError as exc:
        if (
            exc.status in Z.GATEWAY_STATUSES
        ):  # a gateway timeout says nothing about whether Zulip stored the post
            raise ApiError(
                "%s  Zulip may or may not have received the message, and it was not retried.  Check whether it posted: %s"
                % (exc.msg, _check_hint(channel, topic, agent.rt.prog)),
                code=exc.code,
                status=exc.status,
            ) from None
        raise
    message_id = int(result["id"])
    agent.state.record_posted(message_id)
    try:  # a session with a listener lease leases the topic for 2 hours and remembers the id
        LIVE.note_post(
            str(state_root(agent.rt.env, agent.rt.home)),
            agent.seat,
            agent.rt.env,
            channel,
            topic,
            message_id,
        )
    except (OSError, ValueError):
        pass
    if agent.state.cursor(channel, topic) is None:
        # A fresh session that posts and then waits must see the reply, even one that arrives before
        # `wait` starts; with no cursor, `wait` would take that reply for history.
        agent.state.advance_cursor(channel, topic, message_id)
    report = _reformatted(agent, body)
    if agent.json:
        agent.emit_json(
            {"id": message_id, "channel": channel, "topic": topic}
            | ({"reformatted": report} if report else {})
        )
    else:
        agent.out(
            "posted id %d to #%s \u203a %s\n"
            % (message_id, _visible(channel, inline=True), _visible(topic, inline=True))
        )
    return 0


FLEET_WAKE_MENTION = "@**all**"
_WILDCARD_REFUSED_CODES = (
    "STREAM_WILDCARD_MENTION_NOT_ALLOWED",
    "TOPIC_WILDCARD_MENTION_NOT_ALLOWED",
)


def _wildcard_refused(exc: ApiError) -> bool:
    """Zulip's refusal of a wildcard mention:  machine code `STREAM_WILDCARD_MENTION_NOT_ALLOWED`
    ("You do not have permission to use channel wildcard mentions in this channel"), with the message
    as a fallback in case a Zulip version renames the code."""
    if exc.code in _WILDCARD_REFUSED_CODES:
        return True
    text = (exc.msg or "").casefold()
    return "wildcard" in text and "mention" in text and "permission" in text


def _check_fleet_target(channel: str, topic: str) -> None:
    """`post --fleet` is the fleet-wide wake, and it is allowed in exactly one place."""
    if (
        channel.casefold() != ROUTER.FLEET_WAKE_CHANNEL
        or topic.strip().casefold() != ROUTER.FLEET_WAKE_TOPIC
    ):
        raise UsageError(
            "--fleet is the fleet-wide wake (%s) and only works in #%s \u203a %s, not in #%s \u203a %s.  "
            "Use --to NAME for a peer, or drop --fleet"
            % (
                FLEET_WAKE_MENTION,
                ROUTER.FLEET_WAKE_CHANNEL,
                ROUTER.FLEET_WAKE_TOPIC,
                _visible(channel, inline=True),
                _visible(topic, inline=True),
            )
        )


def _fleet_refused_error(exc: ApiError) -> ApiError:
    return ApiError(
        "Zulip refused %s from this bot:  %s  The realm setting that controls it is "
        'can_mention_many_users_group (Organization permissions, "Who can notify a large number of users with '
        'a wildcard mention"), which only Jay can change.  The fleet expects role:members, which includes every '
        "fleet bot, so if it has been narrowed, ask Jay to restore it.  Until then, "
        "post in #%s \u203a %s with --to NAME for each bot that must act, and use `simple-chat dm --owner` for an "
        "emergency."
        % (FLEET_WAKE_MENTION, exc.msg, ROUTER.FLEET_WAKE_CHANNEL, ROUTER.FLEET_WAKE_TOPIC),
        code=exc.code,
        status=exc.status,
        data=exc.data,
    )


def cmd_post(agent: Agent, args: argparse.Namespace) -> int:
    if args.fleet:
        _check_fleet_target(
            args.channel, args.topic
        )  # before any request, including the --to lookup
    text = _read_text(agent, args.text, getattr(args, "body_file", None))
    _scan_refusal(agent, topic=args.topic, channel=args.channel)
    mentions, labels = _resolve_targets(agent, args.to, bool(getattr(args, "to_owner", False)))
    if args.fleet:
        mentions.append(FLEET_WAKE_MENTION)
    body = _compose(
        agent,
        text,
        labels=labels,
        mentions=mentions,
        no_tag=args.no_tag,
        quiet=bool(getattr(args, "quiet", False)),
    )
    try:
        return _send(agent, args.channel, args.topic, body)
    except ApiError as exc:
        if args.fleet and _wildcard_refused(exc):
            raise _fleet_refused_error(exc) from None
        raise


def _owner_config(agent: Agent) -> Any:
    """The installed listener config with an owner pinned in it, else UsageError (exit 2).  The one way the
    CLI learns who the owner is:  `daemon.owner_user_id` in listener.toml (pinned by `simple-chat daemon
    init`, never derived at runtime and never taken from the command line)."""
    cfg = listener_cli._load(agent.rt)
    if cfg.owner_user_id <= 0:
        raise UsageError(
            "no owner to message:  daemon.owner_user_id is not pinned in %s "
            "(run %s daemon init)" % (listener_cli._config_path(agent.rt), agent.rt.prog)
        )
    return cfg


def _owner_user_id(agent: Agent) -> int:
    """The owner's Zulip user id, from `daemon.owner_user_id` in listener.toml."""
    return int(_owner_config(agent).owner_user_id)


def _owner_mention(agent: Agent, owner_id: int) -> str:
    """`@**Full Name|id**` for the pinned owner, the name read live from the realm user list.  Zulip
    resolves the id, so a display name that changed or that another user also carries cannot send the mention
    to someone else.  A pinned id that is not an active human in this realm is a usage error:  better no
    post than a mention that wakes nobody (or a bot)."""
    user = next((u for u in agent.users() if u.get("user_id") == owner_id), None)
    if user is None:
        raise UsageError(
            "the pinned owner (daemon.owner_user_id %d) is not a user in this realm;  re-run "
            "%s daemon init" % (owner_id, agent.rt.prog)
        )
    if not user.get("is_active", True):
        raise UsageError(
            "the pinned owner (daemon.owner_user_id %d) is a deactivated user" % owner_id
        )
    if user.get("is_bot"):
        raise UsageError(
            "the pinned owner (daemon.owner_user_id %d) is a bot, not the owner's own account;  "
            "re-run %s daemon init" % (owner_id, agent.rt.prog)
        )
    name = str(user.get("full_name") or "").strip()
    if not name:
        raise UsageError(
            "the pinned owner (daemon.owner_user_id %d) has no display name to mention" % owner_id
        )
    return "@**%s|%d**" % (name, owner_id)


def _resolve_targets(
    agent: Agent, names: Sequence[str], to_owner: bool
) -> tuple[list[str], list[str]]:
    """(@-mentions, tag labels) for `--to NAME ...` and `--to-owner`.  The owner comes first and is
    labelled OWNER (like `dm`), is mentioned by pinned id, and is never mentioned twice when a `--to`
    names the same person."""
    mentions: list[str] = []
    labels: list[str] = []
    owner_id: int | None = None
    if to_owner:
        owner_id = _owner_user_id(
            agent
        )  # before any request:  an unpinned owner exits 2 with nothing sent
        mentions.append(_owner_mention(agent, owner_id))
        labels.append("OWNER")
    if names:
        active = _active_users(agent)
        for raw in names:
            user = _lookup_peer(agent, active, raw)
            if owner_id is not None and user.get("user_id") == owner_id:
                continue
            mentions.append(_mention_for(active, user))
            labels.append(seat_tag_for(user))
    return mentions, labels


DM_MAX_RECIPIENTS = (
    8  # other people in one conversation, this bot not counted (the hosted dm_send's cap too)
)
DM_CURSOR_CHANNEL = "@dm"  # reserved cursor name for `dm-read --new`
# The setting a host pins the Operator's Zulip user id in, named when it cannot be read:  a
# refusal that omits it sends the operator to the wrong place (Kody review on AFC-473).
OPERATOR_ID_SETTING = "listener.toml.operator_user_id"
GENERIC_BOT = ROUTER.GENERIC_BOT


@dataclass(frozen=True)
class DmRecipients:
    users: list[Mapping[str, Any]]
    labels: list[str]

    @property
    def ids(self) -> list[int]:
        return [int(u["user_id"]) for u in self.users]

    @property
    def owner_only(self) -> bool:
        return self.labels == ["OWNER"]

    def describe(self) -> str:
        if self.owner_only:
            return "the owner"
        return ", ".join(
            _visible(str(u.get("full_name") or u.get("user_id")), inline=True) for u in self.users
        )


def _dm_check_user(agent: Agent, user: Mapping[str, Any], raw: str) -> None:
    """Refuse a recipient a DM cannot or must not reach.  The text names what was typed, never a
    member list."""
    me = agent.me().get("user_id")
    if user.get("user_id") == me:
        raise UsageError("%r is this bot itself; a direct message needs someone else" % raw)
    if not user.get("is_active", True):
        raise UsageError(
            "%r is a deactivated user; a direct message to them would never be read" % raw
        )
    if user.get("is_bot") and user.get("bot_type", GENERIC_BOT) != GENERIC_BOT:
        cfg = listener_cli._load(agent.rt)
        allowed = getattr(cfg, "outgoing_webhook_seats", ())
        uid = user.get("user_id")
        if uid in ROUTER.fleet_bot_ids(
            {uid: user}, allowed, cfg.owner_user_id, outgoing_webhook_seats=allowed
        ):
            return
        raise UsageError(
            "%r is a webhook or integration bot, not a fleet seat; direct messages go to people and "
            "fleet bots only" % raw
        )


def _dm_user(agent: Agent, everyone: Sequence[Mapping[str, Any]], raw: str) -> Mapping[str, Any]:
    """One `--to`/`--with` argument as a realm user that can take a DM.  A name that only a
    deactivated user carries is refused as deactivated, not as unknown."""
    active = [u for u in everyone if u.get("is_active", True) and u.get("full_name")]
    try:
        user = _lookup_peer(agent, active, raw)
    except UsageError:
        gone = [
            u
            for u in _match_peer(
                [u for u in everyone if not u.get("is_active", True) and u.get("full_name")], raw
            )
        ]
        if gone:
            raise UsageError(
                "%r is a deactivated user; a direct message to them would never be read" % raw
            ) from None
        raise
    _dm_check_user(agent, user, raw)
    return user


def _owner_id_pinned(agent: Agent) -> int | None:
    try:
        return _owner_user_id(agent)
    except UsageError:
        return None


def _dm_recipients(agent: Agent, args: argparse.Namespace) -> DmRecipients:
    """Who a `dm` goes to.  No `--to` means the owner pinned in listener.toml (the original behavior);
    `--owner` adds the owner to the named people.  At most DM_MAX_RECIPIENTS people, this bot not counted."""
    names = [n for n in (getattr(args, "to", None) or [])]
    if len(names) > DM_MAX_RECIPIENTS:
        raise UsageError(
            "a direct message goes to at most %d people; %d were named"
            % (DM_MAX_RECIPIENTS, len(names))
        )
    want_owner = bool(getattr(args, "owner", False)) or not names
    owner_id = _owner_user_id(agent) if want_owner else _owner_id_pinned(agent)
    users: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    if want_owner:
        users.append({"user_id": owner_id, "full_name": "the owner"})
        seen.add(int(owner_id))  # type: ignore[arg-type]
    if names:
        everyone = list(agent.users())
        for raw in names:
            user = _dm_user(agent, everyone, raw)
            if int(user["user_id"]) not in seen:
                seen.add(int(user["user_id"]))
                users.append(user)
    if len(users) > DM_MAX_RECIPIENTS:
        raise UsageError(
            "a direct message goes to at most %d people; %d were named"
            % (DM_MAX_RECIPIENTS, len(users))
        )
    labels = [
        "OWNER" if owner_id is not None and u["user_id"] == owner_id else seat_tag_for(u)
        for u in users
    ]
    return DmRecipients(users, labels)


def _int_or_none(value: Any) -> int | None:
    """The value as an int, or None when it is missing or not one (never a guess)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dm_rule(agent: Agent, args: argparse.Namespace, to: DmRecipients) -> None:
    """The DM rule (owner decision, Oct 22 2026):  `simple-chat dm` may not open a new direct message.

    A reply inside a conversation Operator already opened is still allowed, so the existing messages
    are read first.  A refusal is audited (ids and the reason only) and then raised as the policy's own
    DmRefused, which exits 3 (a refusal) rather than 2 (a malformed command).

    A host that cannot say who the Operator is fails closed by name:  a damaged listener config
    raises a UsageError that says which setting to repair, instead of a refusal that points at an
    environment variable that is not the one at fault (Kody review on AFC-473)."""
    env = getattr(getattr(agent, "rt", None), "env", None) or os.environ
    # The rule's own switch is read first, exactly as the MCP reads it, because with the block off
    # there is nothing to decide:  the Operator's id is only needed to tell a conversation Operator
    # opened from one a seat opened, and with the rule off that judgement is never made, so a host
    # without the id is not broken by it.  Reading the conversation first would also cost a network
    # round-trip per direct-message write for a result that is then thrown away, and would turn an
    # outage into a retryable failure for a send the rule does not govern.
    if not DMP.rule_enabled(env):
        return
    clarifying = bool(getattr(args, "clarifying_question", None))
    if DMP.clarifying_exempt(agent.seat, clarifying=clarifying):
        # Operator's own clarifying question is the one message the rule permits without knowing
        # who the Operator is, so the id is never resolved and the conversation is never read:  a
        # host whose listener config is damaged can still send it (Kody review on AFC-473).
        return
    bots = {int(u["user_id"]): bool(u.get("is_bot")) for u in agent.users()}
    recipients = [
        {"user_id": u["user_id"], "is_bot": bots.get(int(u["user_id"]), False)} for u in to.users
    ]
    existing: list[Mapping[str, Any]] = []
    try:
        # Anchored at the head of the conversation, like the hosted dmOpener:  the rule asks whether
        # the OLDEST message came from Operator, so a conversation older than the window must be read
        # from its first message.  The newest window cannot see that opener and would refuse a seat's
        # legitimate reply inside a long-running Operator-opened DM.
        existing = agent.fetch(
            [{"operator": "dm", "operand": to.ids}], anchor="oldest", num_after=200
        )
    except Z.ApiError as exc:
        # Same discrimination as tools.py _dm_exists, so both transports decide alike.  A rejected
        # credential (401/403) and a rate limit (429) are definitive answers from Zulip and propagate,
        # because treating them as "no such conversation" would make the CLI refuse the message with a
        # terminal dm_not_allowed instead of the credential or retryable error the caller can act on.
        # A transient failure (408, any 5xx) becomes a NetworkError for the same reason; only an
        # ordinary "no messages" answer falls through to the empty list.
        status = exc.status
        if exc.code == "RATE_LIMIT_HIT" or status == 429:
            raise
        if status == 408 or (isinstance(status, int) and status >= 500):
            raise NetworkError(
                "Zulip answered HTTP %s for the direct message narrow" % status
            ) from exc
        if status in (401, 403):
            raise Z.ApiError(
                "Zulip refused the bot credential (%s);  verify the configured API key"
                % Z.masked_credential(getattr(getattr(agent, "creds", None), "key", "")),
                code=exc.code,
                status=status,
            ) from exc
        existing = []
    try:
        DMP.check_new_dm(
            env,
            identity=agent.seat,
            recipients=recipients,
            operator_opened=DMP.operator_opened_dm(
                recipients,
                existing,
                # The pinned lookup is a callable, so the host's listener config is read only
                # when the environment has no id:  a host that exports the id is never broken by
                # a config file it does not consult (Kody review on AFC-473).
                operator_user_id=DMP.operator_user_id(env, lambda: pinned_operator_id(agent)),
                self_id=_int_or_none(agent.me().get("user_id")),
            ),
            clarifying=clarifying,
            owner_user_id=_owner_id_pinned(agent) or None,
        )
    except DMP.DmRefused as refused:
        DMP.audit_refusal(
            env,
            None,
            transport="cli",
            identity=agent.tag,
            recipients=recipients,
            reason=refused.reason,
            message=str(refused),
        )
        raise


def cmd_dm(agent: Agent, args: argparse.Namespace) -> int:
    """A private message from this seat's own bot to the owner (the default), to named fleet peers or
    people, or to a group of up to 8.  The same path as `post`:  the secret scanner, the seat tag
    (`[SEAT\u00b7session\u2192PEER]`, `OWNER` for the pinned owner), the sentence gap and the length cap.
    Recipients are resolved like `post --to` (full name, email, seat tag, user id); webhook and
    integration bots, deactivated users and this bot itself are refused before anything is sent."""
    text = _read_text(agent, args.text, getattr(args, "body_file", None))
    to = _dm_recipients(agent, args)
    _dm_rule(agent, args, to)
    body = _compose(
        agent,
        text,
        labels=to.labels,
        mentions=[],
        no_tag=args.no_tag,
        quiet=bool(getattr(args, "quiet", False)),
    )
    where = to.describe()
    check = (
        "Check your direct messages with %s in Zulip (or `simple-chat dm-read`) before sending it again."
        % (where if to.owner_only else "them")
    )
    try:
        result = agent.client.post("messages", {"type": "direct", "to": to.ids, "content": body})
    except NetworkError as exc:
        if exc.maybe_sent:
            raise NetworkError(
                "%s  The message was not retried.  %s" % (exc, check),
                timeout=exc.timeout,
                maybe_sent=True,
            ) from None
        raise
    except ApiError as exc:
        if exc.status in Z.GATEWAY_STATUSES:
            raise ApiError(
                "%s  Zulip may or may not have received the message, and it was not retried.  %s"
                % (exc.msg, check),
                code=exc.code,
                status=exc.status,
            ) from None
        raise
    message_id = int(result["id"])
    agent.state.record_posted(message_id)
    report = _reformatted(agent, body)
    if agent.json:
        agent.emit_json(
            {"id": message_id, "type": "direct", "to": to.ids}
            | ({"reformatted": report} if report else {})
        )
    else:
        agent.out("sent id %d to %s (direct message)\n" % (message_id, where))
    return 0


def _dm_with_ids(agent: Agent, names: Sequence[str]) -> list[int]:
    if len(names) > DM_MAX_RECIPIENTS:
        raise UsageError(
            "a conversation has at most %d other people; %d were named"
            % (DM_MAX_RECIPIENTS, len(names))
        )
    everyone = list(agent.users())
    ids: list[int] = []
    for raw in names:
        if raw.strip().casefold() == "owner":  # the owner pinned in listener.toml
            ids.append(_owner_user_id(agent))
            continue
        user = _lookup_peer(
            agent, [u for u in everyone if u.get("full_name")], raw
        )  # a deactivated user's history can be read
        if user.get("user_id") == agent.me().get("user_id"):
            raise UsageError(
                "%r is this bot itself; name the other people in the conversation" % raw
            )
        ids.append(int(user["user_id"]))
    return sorted(set(ids))


def cmd_dm_read(agent: Agent, args: argparse.Namespace) -> int:
    """Read one direct-message conversation of this bot, oldest first, named by the other people
    (`--with`, repeatable for a group).  Bodies are untrusted data like any message."""
    ids = _dm_with_ids(agent, args.with_)
    narrow: list[dict[str, Any]] = [{"operator": "dm", "operand": ids}]
    cursor_args = (DM_CURSOR_CHANNEL, ",".join(str(i) for i in ids))
    cursor = agent.state.cursor(*cursor_args) if args.new else None

    def deliverable(batch: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [m for m in batch if not args.exclude_self or not agent.is_self(m)]

    newest_seen: int | None = None
    if args.since is not None or (args.new and cursor is not None):
        anchor = args.since if args.since is not None else cursor
        messages = agent.fetch(narrow, anchor=anchor, num_after=args.limit, include_anchor=False)
    else:
        messages = agent.fetch(narrow, anchor="newest", num_before=args.limit)
    shown = deliverable(messages)
    newest_seen = max((m["id"] for m in messages), default=None)
    for message in shown:
        agent.emit(message)
    if args.new and newest_seen is not None:
        agent.state.advance_cursor(*cursor_args, newest_seen)
    if not shown and not agent.json:
        agent.err("no messages")
    return 0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def cmd_verify_owner_msg(agent: Agent, args: argparse.Namespace) -> int:
    """Is message MESSAGE_ID really the owner's, and in the place the caller expects?  Every one of these
    must hold, and the verdict says which did not:

      * the message exists and this bot can read it (a missing or unreadable id is a verdict, not an error);
      * `sender_id` is the owner pinned in the INSTALLED listener.toml, the same file `dm --owner` reads;
      * its `client` is in that file's `daemon.owner_clients` (a person's Zulip app, never an API script that
        holds the owner's account).  The client name is a heuristic that Zulip fills in from the request, not
        authentication:  it narrows the owner's own tokens, it does not prove who typed the words;
      * for `--topic`, it is a channel message in `--channel` and that topic (the same topic with the resolved
        check mark in front counts);  for `--dm-with`, it is a direct message between exactly the owner and
        that peer, so a group conversation never verifies;
      * its id is greater than `--after`.

    The message text is never read into the verdict.  An unpinned owner is a usage error (exit 2) like
    `dm --owner`, and Zulip or network failures keep their own exit codes (5, 6);  a message that simply is
    not the owner's exits 7 with ok false."""
    cfg = _owner_config(agent)
    owner_id = int(cfg.owner_user_id)
    peer: Mapping[str, Any] | None = None
    if args.dm_with is not None:
        peer = _lookup_peer(agent, _active_users(agent), args.dm_with)
        if peer.get("user_id") == owner_id:
            raise UsageError(
                "--dm-with names the owner;  name the other person in the conversation"
            )
    try:
        message = agent.client.get("messages/%d" % args.message_id, {"apply_markdown": False}).get(
            "message"
        )
    except ApiError as exc:
        if exc.status not in (
            400,
            404,
        ):  # Zulip answers 400 for an id that does not exist or is not visible
            raise
        message = None
    checks = {
        "found": isinstance(message, Mapping),
        "sender_is_owner": False,
        "client_is_human": False,
        "place": False,
        "newer": False,
    }
    reasons: list[str] = []
    verdict: dict[str, Any] = {
        "ok": False,
        "message_id": args.message_id,
        "owner_user_id": owner_id,
        "checks": checks,
        "reasons": reasons,
        "sender_id": None,
        "client": None,
        "place": None,
        "channel": None,
        "topic": None,
        "edited": None,
    }
    if not isinstance(message, Mapping):
        reasons.append("message %d was not found, or this bot cannot read it" % args.message_id)
    else:
        sender_id = message.get("sender_id")
        client = message.get("client")
        verdict["sender_id"] = sender_id if _is_int(sender_id) else None
        verdict["client"] = client if isinstance(client, str) else None
        verdict["edited"] = message.get("last_edit_timestamp") is not None
        checks["sender_is_owner"] = _is_int(sender_id) and sender_id == owner_id
        if not checks["sender_is_owner"]:
            reasons.append("the sender is not the owner pinned in listener.toml")
        checks["client_is_human"] = isinstance(client, str) and client in cfg.owner_clients
        if not checks["client_is_human"]:
            reasons.append(
                "the message's client is not one of daemon.owner_clients (a person's Zulip app)"
            )
        if message.get("type") == "private":
            verdict["place"] = "dm"
            recipients = message.get("display_recipient")
            ids = (
                {r.get("id") for r in recipients if isinstance(r, dict)}
                if isinstance(recipients, list)
                else set()
            )
            expected = {owner_id, peer["user_id"]} if peer is not None else None
            checks["place"] = expected is not None and ids == expected
            if peer is None:
                reasons.append("this is a direct message, but --topic was given")
            elif not checks["place"]:
                reasons.append(
                    "the direct message is not between exactly the owner and %s" % args.dm_with
                )
        else:
            verdict["place"] = "stream"
            channel, topic = message_channel(message), message_topic(message)
            verdict["channel"], verdict["topic"] = channel, topic
            if peer is not None:
                reasons.append("this is a channel message, but --dm-with was given")
            else:
                wanted = args.topic.casefold()
                checks["place"] = (
                    message.get("type") == "stream"
                    and channel.casefold() == args.channel.casefold()
                    and topic.casefold() in (wanted, (RESOLVED_PREFIX + args.topic).casefold())
                )
                if not checks["place"]:
                    reasons.append(
                        "the message is not in #%s \u203a %s" % (args.channel, args.topic)
                    )
        message_id = message.get("id")
        checks["newer"] = args.after is None or (_is_int(message_id) and message_id > args.after)
        if not checks["newer"]:
            reasons.append("the message is not newer than message %d" % args.after)
    verdict["ok"] = not reasons
    if agent.json:
        agent.emit_json(verdict)
    elif verdict["ok"]:
        agent.out(
            "verified:  message %d is from the owner (%s)%s\n"
            % (
                args.message_id,
                _visible(str(verdict["client"]), inline=True),
                "  [edited after it was sent]" if verdict["edited"] else "",
            )
        )
    else:
        agent.out(
            "NOT verified:  message %d\n%s"
            % (args.message_id, "".join("  - %s\n" % _visible(r, inline=True) for r in reasons))
        )
    return 0 if verdict["ok"] else EXIT_NOT_VERIFIED


def cmd_reply(agent: Agent, args: argparse.Namespace) -> int:
    text = _read_text(agent, args.text, getattr(args, "body_file", None))
    to_owner = bool(getattr(args, "to_owner", False))
    if to_owner:
        _owner_user_id(
            agent
        )  # an unpinned owner exits 2 before the original message is even fetched
    original = (
        agent.client.get("messages/%d" % args.id, {"apply_markdown": False}).get("message") or {}
    )
    if original.get("type") != "stream":
        raise UsageError("message %d is a direct message; reply is channel-only in v1" % args.id)
    channel, topic = message_channel(original), message_topic(original)
    _scan_refusal(agent, topic=topic, channel=channel)
    mentions, labels = _resolve_targets(agent, args.to, to_owner)
    body = _compose(
        agent,
        text,
        labels=labels,
        mentions=mentions,
        no_tag=args.no_tag,
        quiet=bool(getattr(args, "quiet", False)),
    )
    return _send(agent, channel, topic, body)


def cmd_read(agent: Agent, args: argparse.Namespace) -> int:
    scope = Scope(args.channel, args.topic)
    cursor = agent.state.cursor(*scope.cursor_args) if args.new else None

    def deliverable(batch: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [m for m in batch if args.include_self or not agent.is_self(m)]

    newest_seen: int | None = (
        None  # the highest id on any page fetched: where --new moves the cursor to
    )
    if args.since is not None:
        messages = agent.fetch(
            scope.narrow, anchor=args.since, num_after=args.limit, include_anchor=False
        )
        shown = deliverable(messages)
        newest_seen = max((m["id"] for m in messages), default=None)
    elif args.new and cursor is not None:
        anchor = cursor
        while True:
            messages = agent.fetch(
                scope.narrow, anchor=anchor, num_after=args.limit, include_anchor=False
            )
            shown = deliverable(messages)
            if messages:
                anchor = newest_seen = max(m["id"] for m in messages)
            if shown or len(messages) < args.limit:
                break  # else a whole page of this session's own posts: look further
    else:
        messages = agent.fetch(scope.narrow, anchor="newest", num_before=args.limit)
        shown = deliverable(messages)
        newest_seen = max((m["id"] for m in messages), default=None)
    for message in shown:
        agent.emit(message)
    if args.new and newest_seen is not None:
        agent.state.advance_cursor(*scope.cursor_args, newest_seen)
    if not shown and not agent.json:
        agent.err("no messages")
    return 0


def cmd_wait(agent: Agent, args: argparse.Namespace) -> int:
    scope = Scope(args.channel, args.topic)
    delivery = Delivery(agent)
    queue = EventQueue(agent.client, [["channel", args.channel], ["topic", args.topic]])
    deadline = time.monotonic() + args.timeout
    try:
        queue.register()  # first: nothing posted from here on can fall between backfill and events
        while True:
            if backfill_pages(agent, scope, delivery, [scope], until_printed=True):
                return 0
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        agent.err(
                            "timed out after %s seconds with no new messages in #%s \u203a %s"
                            % (_trim(args.timeout), args.channel, args.topic)
                        )
                        return 4
                    if delivery.deliver(
                        queue.poll(min(agent.rt.events_timeout, remaining)), [scope], live=True
                    ):
                        return 0
            except QueueExpired:
                queue.register()
    finally:
        _close_queue(agent, queue)


def _trim(number: float) -> str:
    return ("%d" % number) if float(number).is_integer() else ("%g" % number)


def _close_queue(agent: Agent, queue: EventQueue) -> None:
    problem = queue.close()
    if problem:
        agent.err("warning: could not delete the event queue: %s" % problem)


class Watcher:
    """One event queue plus the scopes whose messages it carries."""

    def __init__(self, agent: Agent, narrow: list[list[str]] | None, scopes: list[Scope]) -> None:
        self.queue = EventQueue(agent.client, narrow)
        self.scopes = scopes
        self.needs_backfill = False


def cmd_listen(agent: Agent, args: argparse.Namespace) -> int:
    channel = args.channel
    topics = list(dict.fromkeys(args.topic))
    if not topics and not args.mentions:
        raise UsageError("listen needs at least one --topic, or --mentions")
    watchers: list[Watcher] = []
    if len(topics) <= MAX_TOPICS_PER_QUEUE_SET:
        # Zulip ANDs narrow terms, so two topics cannot share one narrow: one queue per topic.
        for topic in topics:
            watchers.append(
                Watcher(agent, [["channel", channel], ["topic", topic]], [Scope(channel, topic)])
            )
    else:
        watchers.append(Watcher(agent, [["channel", channel]], [Scope(channel, t) for t in topics]))
    if args.mentions:
        # No narrow: the queue carries everything the bot receives and the "mentioned" flag filters.
        watchers.append(Watcher(agent, None, [Scope(channel, None)]))

    stop = agent.rt.stop or threading.Event()
    delivery = Delivery(agent, max_messages=args.max_messages, stop=stop)
    fatal: list[Exception] = []

    def drain(watcher: Watcher) -> None:
        for scope in watcher.scopes:
            backfill_pages(agent, scope, delivery, watcher.scopes, until_printed=False, stop=stop)

    def run(watcher: Watcher) -> None:
        failures = 0
        while not stop.is_set():
            try:
                if watcher.queue.queue_id is None:
                    watcher.queue.register()
                    watcher.needs_backfill = True
                if watcher.needs_backfill:
                    drain(watcher)
                    watcher.needs_backfill = False
                delivery.deliver(
                    watcher.queue.poll(agent.rt.events_timeout), watcher.scopes, live=True
                )
                failures = 0
            except QueueExpired:
                continue  # the loop registers a new queue and backfills from the cursor
            except (NetworkError, ApiError) as exc:
                transient = (
                    isinstance(exc, NetworkError)
                    or exc.code == "RATE_LIMIT_HIT"
                    or (exc.status or 0) >= 500
                )
                if not transient:
                    fatal.append(exc)
                    stop.set()
                    return
                delay = LISTEN_BACKOFF[min(failures, len(LISTEN_BACKOFF) - 1)]
                failures += 1
                agent.err("listen: %s; retrying in %d seconds" % (exc, delay))
                stop.wait(delay)
            except Exception as exc:  # noqa: BLE001 - a dead worker must not leave listen hanging
                fatal.append(exc)
                stop.set()
                return

    what = ", ".join(topics + (["@-mentions"] if args.mentions else []))
    agent.err("listening on #%s: %s" % (channel, what))
    threads = [
        threading.Thread(target=run, args=(w,), name="listen-%d" % i, daemon=True)
        for i, w in enumerate(watchers)
    ]
    try:
        # Starting the threads is inside the try on purpose: on a busy machine a SIGTERM can land while
        # the main thread is still waking up from Thread.start(), and that must still end cleanly.
        for thread in threads:
            thread.start()
        while not stop.wait(0.25):
            pass
    except KeyboardInterrupt:
        pass  # Ctrl-C and SIGTERM are how listen is meant to end
    finally:
        stop.set()
        for watcher in watchers:
            _close_queue(agent, watcher.queue)
        for thread in threads:
            if thread.ident is not None:  # a thread that never started cannot be joined
                thread.join(timeout=0.5)
        for watcher in watchers:  # a queue a worker registered while the first pass ran
            _close_queue(agent, watcher.queue)
    if fatal:
        raise fatal[0]
    return 0


def cmd_inbox(agent: Agent, args: argparse.Namespace) -> int:
    """@-mentions of this bot and direct messages sent to it, oldest first, from one seat-wide cursor.
    A DM this bot sent itself is not in it."""
    cursor = agent.state.inbox_cursor()
    merged: dict[int, dict[str, Any]] = {}
    for narrow in (
        [{"operator": "is", "operand": "mentioned"}],
        [{"operator": "is", "operand": "dm"}],
    ):
        if cursor is None:
            batch = agent.fetch(narrow, anchor="newest", num_before=args.limit)
        else:
            batch = agent.fetch(narrow, anchor=cursor, num_after=args.limit, include_anchor=False)
        for message in batch:
            if message.get("type") == "private" and agent.sent_by_this_bot(message):
                continue
            merged.setdefault(message["id"], message)
    ordered = [merged[i] for i in sorted(merged)]
    messages = ordered[-args.limit :] if cursor is None else ordered[: args.limit]
    for message in messages:
        agent.emit(message)
    if messages and not args.peek:
        agent.state.advance_inbox(max(m["id"] for m in messages))
    if not messages and not agent.json:
        agent.err("no new mentions or direct messages")
    return 0


def cmd_topic_policy(policy: int, verb: str) -> Callable[[Agent, argparse.Namespace], int]:
    def run(agent: Agent, args: argparse.Namespace) -> int:
        stream_id = agent.stream_id(args.channel)
        agent.client.post(
            "user_topics",
            {"stream_id": stream_id, "topic": args.topic, "visibility_policy": policy},
        )
        if agent.json:
            agent.emit_json(
                {"channel": args.channel, "topic": args.topic, "visibility_policy": policy}
            )
        else:
            agent.out("%s #%s \u203a %s\n" % (verb, args.channel, args.topic))
        return 0

    return run


def _resolved_name(topic: str) -> str:
    if topic.startswith(RESOLVED_PREFIX.strip()):
        raise UsageError("topic %r is already resolved" % topic)
    new_topic = RESOLVED_PREFIX + topic
    if len(new_topic) > Z.MAX_TOPIC_LENGTH:
        raise UsageError(
            "the resolved topic would be %d characters; Zulip allows at most %d"
            % (len(new_topic), Z.MAX_TOPIC_LENGTH)
        )
    return new_topic


def cmd_resolve(agent: Agent, args: argparse.Namespace) -> int:
    _resolved_name(args.topic)  # refuse the obvious cases before any request
    newest = agent.fetch(topic_narrow(args.channel, args.topic), anchor="newest", num_before=1)
    if not newest:
        raise UsageError("no messages found in #%s \u203a %s" % (args.channel, args.topic))
    latest = max(newest, key=lambda m: m["id"])
    # Zulip matches topics without regard to case, and change_all renames every message to the name
    # given, so build the new name from the topic's real spelling, not from what was typed.
    new_topic = _resolved_name(message_topic(latest))
    message_id = latest["id"]
    agent.client.patch(
        "messages/%d" % message_id, {"topic": new_topic, "propagate_mode": "change_all"}
    )
    if agent.json:
        agent.emit_json({"channel": args.channel, "topic": new_topic, "id": message_id})
    else:
        agent.out(
            "resolved: #%s \u203a %s\n"
            % (_visible(args.channel, inline=True), _visible(new_topic, inline=True))
        )
    return 0


def cmd_react(agent: Agent, args: argparse.Namespace) -> int:
    emoji = args.emoji.strip().strip(":")
    if not emoji:
        raise UsageError("the emoji name is empty")
    agent.client.post("messages/%d/reactions" % args.id, {"emoji_name": emoji})
    if agent.json:
        agent.emit_json({"id": args.id, "emoji_name": emoji})
    else:
        agent.out("reacted to %d with :%s:\n" % (args.id, emoji))
    return 0


COMMANDS: dict[str, Callable[[Agent, argparse.Namespace], int]] = {
    "whoami": cmd_whoami,
    "channels": cmd_channels,
    "topics": cmd_topics,
    "subscribe": cmd_subscribe,
    "post": cmd_post,
    "dm": cmd_dm,
    "dm-read": cmd_dm_read,
    "verify-owner-msg": cmd_verify_owner_msg,
    "reply": cmd_reply,
    "read": cmd_read,
    "wait": cmd_wait,
    "listen": cmd_listen,
    "inbox": cmd_inbox,
    "follow": cmd_topic_policy(3, "following"),
    "mute": cmd_topic_policy(1, "muted"),
    "unmute": cmd_topic_policy(0, "unmuted"),
    "resolve": cmd_resolve,
    "react": cmd_react,
    **zulip_cli.COMMANDS,
}


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def _on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def cmd_identities(rt: Runtime, args: argparse.Namespace) -> int:
    """`simple-chat identities`:  the allowed set and the default (AFC-331), from the files on this host.
    A launcher's seat narrows the set to itself."""
    from . import identities as X

    resolved = I.resolve_seat(
        rt.env, flag=getattr(args, "as_seat", None), default=getattr(args, "default_seat", None)
    )
    if resolved.problem:
        raise Z.CredentialError(resolved.problem)
    default = Z.normalise_seat(resolved.seat) if resolved.seat else None
    home = rt.home if rt.home is not None else Path(rt.env.get("HOME") or os.path.expanduser("~"))
    registry_problem = None
    try:
        registry = I.load_seat_registry(env=rt.env)
    except I.SeatRegistryError as exc:
        registry, registry_problem = (
            None,
            (
                "cannot read the seat registry, so no identity can be checked and none is "
                "allowed (%s);  fix FLEET_APPS_JSON or fleet-apps.json" % exc
            ),
        )
    found = X.allowed_identities(rt.env, home, registry=registry) if registry is not None else {}
    names = set(found)
    # The default identity's explicit credential (--rc or ZULIP_RC, which may sit outside the secrets directory) is
    # part of the set, as at stdio startup, with the same exclusion:  only an agent identity qualifies.
    rc_arg = getattr(args, "rc", None) or rt.env.get("ZULIP_RC")
    if (
        default
        and rc_arg
        and registry is not None
        and X.agent_identity_problem(default, registry=registry) is None
    ):
        names.add(default)
    allowed = sorted(names)
    if resolved.source == "launcher":
        allowed = [default] if default in allowed else []
    if getattr(args, "json", False):
        rt.out(
            json.dumps(
                {
                    "default_identity": default,
                    "transport": "cli",
                    "registry_problem": registry_problem,
                    "identities": [{"identity": name, "zulip": "configured"} for name in allowed],
                }
            )
            + "\n"
        )
    else:
        rt.out("default identity:  %s\n" % (default or "none"))
        for name in allowed:
            rt.out("%s\n" % name)
    if registry_problem:
        rt.err("%s: %s" % (rt.prog, registry_problem))
        return 3
    return 0


# The CLI commands that write (the CLI side of tools.py WRITE_TOOLS, AFC-331).  Each run of one appends a row
# to the identity audit log (identity_log.py).  `listener` writes only local records and is not here.
WRITE_COMMANDS = frozenset(
    {
        "post",
        "reply",
        "react",
        "dm",
        "edit",
        "delete",
        "move",
        "resolve",
        "unresolve",
        "unreact",
        "mark-read",
        "mark-unread",
        "star",
        "unstar",
        "unsubscribe",
        "follow",
        "mute",
        "unmute",
        "set-status",
        "upload",
        "scheduled",
        "drafts",
        "typing",
        "subscribe",
    }
)
READ_ONLY_ACTIONS = frozenset({"list"})  # `scheduled list` and `drafts list` read


def _check_agent_identity(rt: Runtime, agent: Agent, command: str = "") -> None:
    """AFC-331:  the CLI acts only as an agent bot (a live or extra seat, a BF-* bot or an enabled GB-*
    persona), never as the owner's account or an integration bot, whatever credential files exist.  Without a
    readable registry nothing proves that, so every identity is refused (fail closed), as on the stdio and Notes
    paths.  `whoami` is the one diagnostic that goes on and reports the registry problem."""
    from . import identities as X

    try:
        registry = I.load_seat_registry(env=rt.env)
    except I.SeatRegistryError as exc:
        problem = (
            "cannot read the seat registry, so %s cannot be checked as an agent identity (%s);  "
            "fix FLEET_APPS_JSON or fleet-apps.json" % (agent.seat, exc)
        )
        if command == "whoami":
            agent.registry_problem = problem
            return
        raise Z.CredentialError(problem) from None
    problem = X.agent_identity_problem(agent.seat, registry=registry)
    if problem:
        raise Z.CredentialError(problem)


def _audit_write(
    rt: Runtime, args: argparse.Namespace, agent: Agent, code: int, outcome: str | None = None
) -> None:
    from . import identity_log as AUDIT

    if args.command == "api":
        # Classified by method:  a non-GET call is a write, audited with the method and path, never the body.
        if args.method == "GET":
            return
        path = args.path.strip().strip("/")
        path = path.removeprefix("api/v1/")
        AUDIT.append(
            rt.env,
            rt.home,
            transport="cli",
            default_identity=agent.seat,
            identity=agent.seat,
            tool="api %s %s" % (args.method, path),
            outcome=outcome or ("ok" if code == 0 else "exit_%d" % code),
        )
        return
    if args.command not in WRITE_COMMANDS:
        return
    action = next(
        (
            getattr(args, name)
            for name in ("scheduled_action", "drafts_action")
            if isinstance(getattr(args, name, None), str)
        ),
        None,
    )
    if action in READ_ONLY_ACTIONS:
        return
    AUDIT.append(
        rt.env,
        rt.home,
        transport="cli",
        default_identity=agent.seat,
        identity=agent.seat,
        tool=args.command + (" " + action if action else ""),
        outcome=outcome or ("ok" if code == 0 else "exit_%d" % code),
    )


LOCAL_COMMANDS: dict[str, Callable[[Runtime, argparse.Namespace], int]] = {
    "daemon": listener_cli.cmd_daemon,
    "status": listener_cli.cmd_status,
    "wakes": listener_cli.cmd_wakes,
    "listener": listener_cli.cmd_listener,
    "identities": cmd_identities,
    "notes": notes_cli.cmd_notes,
    "guide": notes_cli.cmd_guide,
}

# `simple-notes` verbs (AFC-486:  one per notes_* tool, plus the Mac runner).  They are top-level commands, so they
# land in LOCAL_COMMANDS beside the listener verbs, except `read`, which `simple-chat` owns too (a topic's history)
# and which only the simple-notes override below sends here.
NOTES_VERBS = ("create", "update", "pin", "unpin", "read", "list", "status", "log", "runner")
for _action in NOTES_VERBS:
    if _action != "read":
        LOCAL_COMMANDS[_action] = notes_cli.cmd_notes

# `status` and `read` are verbs of two commands:  `simple-chat status` is the listener's and `simple-chat read` a
# topic's history, `simple-notes status` and `simple-notes read` are the notes ones.  A bare name cannot pick one,
# so each command overrides them for its own verbs.
DISPATCH_OVERRIDES: dict[str, dict[str, Callable[[Runtime, argparse.Namespace], int]]] = {
    "simple-notes": {"status": notes_cli.cmd_notes, "read": notes_cli.cmd_notes},
    "simple-chat": {"status": listener_cli.cmd_status},
}

# Every top-level verb `simple-chat` owns:  the Zulip/chat verbs, the stdio MCP server, the tools
# guide, identity listing and the attach/detach hooks.  The listener verbs (daemon, listener,
# status, wakes) are held here until the Operator/eavesdropper workstream gives them their own
# command; they are not renamed in this change.
CHAT_VERBS = frozenset(
    {
        "whoami",
        "channels",
        "topics",
        "subscribe",
        "unsubscribe",
        "post",
        "reply",
        "dm",
        "dm-list",
        "dm-read",
        "read",
        "wait",
        "listen",
        "inbox",
        "follow",
        "mute",
        "unmute",
        "resolve",
        "unresolve",
        "react",
        "unreact",
        "star",
        "unstar",
        "verify-owner-msg",
        "mcp",
        "guide",
        "identities",
    }
    | set(zulip_cli.COMMANDS)
)

# The listener/daemon verbs.  They stay on `simple-chat` until the Operator/eavesdropper
# workstream gives them their own command; this change does not rename them.
LISTENER_VERBS = frozenset({"attach", "detach", "daemon", "listener", "status", "wakes"})

# The two verbs the Claude Code hooks run on every prompt and Stop.  They are dispatched before the
# parser is built so the hook path loads only light modules;  that fast path is per-command.
HOOK_VERBS = frozenset({"attach", "detach"})

# Which verb set each entry point owns, per registration source.  `main(prog=...)` and
# `build_parser_for` read this, so the four CLIs stay four commands with four separate helps.
COMMAND_SPECS: dict[str, dict[str, frozenset[str]]] = {
    "simple-chat": {"chat": CHAT_VERBS, "listener": LISTENER_VERBS, "notes": frozenset()},
    "simple-notes": {"chat": frozenset(), "listener": frozenset(), "notes": frozenset(NOTES_VERBS)},
}


def build_parser_for(
    prog: str, channel_default: str = Z.DEFAULT_CHANNEL
) -> argparse.ArgumentParser:
    """The parser for one entry point: only that command's verbs, under its own name."""
    spec = COMMAND_SPECS.get(prog)
    if spec is None:
        return build_parser(prog, channel_default=channel_default)
    return build_parser(
        prog,
        spec["chat"],
        listener_verbs=spec["listener"],
        notes_verbs=spec["notes"],
        channel_default=channel_default,
    )


# ---------------------------------------------------------------- remote read transport (opt-in)
#
# SIMPLE_CHAT_REMOTE=1 sends a few READ verbs (whoami, channels, topics, read, inbox) to the hosted ST-MCP
# endpoint with the bot's own bearer (SIMPLE_TOOLS_BEARER, process environment only).  The hosted server decides
# the seat from the bearer; this client never names one, so --as and --default-seat are refused.  The endpoint is
# the fixed constant below (no override), no Zulip key or rc file is read, and a failure never falls back to the
# direct path.  Every other verb and every flag with no hosted equivalent is refused.  The hosted bearer holds
# every hosted tool of its seat; calling only these reads is a client choice, not a server restriction.
# This is the LOCAL simple-chat CLI calling the HOSTED ST-MCP server:  two different servers.
REMOTE_MCP_URL = "https://agents.simplewithus.com/mcp"
REMOTE_BEARER_ENV = "SIMPLE_TOOLS_BEARER"
REMOTE_BEARER_RE = re.compile(r"[A-Za-z0-9_\-]{20,200}")
REMOTE_TIMEOUT_S = 40
REMOTE_READ_VERBS = ("whoami", "channels", "topics", "read", "inbox")
REMOTE_WRITE_VERBS = ("post", "reply", "react", "dm")
REMOTE_VERBS = REMOTE_READ_VERBS + REMOTE_WRITE_VERBS


REMOTE_MAX_BYTES = 4_000_000
REMOTE_MAX_DEPTH = 64
REMOTE_MAX_NODES = 100_000
# The error codes the hosted server can return (the ToolError codes in scripts/agent-sync-mcp/src);  a test keeps
# this list in step with that source.  Anything else, including a code that merely looks safe, prints as "error".
_REMOTE_CODES = frozenset(
    (
        "invalid_argument",
        "unavailable",
        "upstream_error",
        "not_authorized",
        "rate_limited",
        "not_configured",
        "outcome_unknown",
        "paused",
        "linear_error",
        "internal",
        "response_too_large",
        "refused_secret",
        "not_found",
        "channel_not_allowed",
        "claim_held",
        "zulip_error",
        "writes_disabled",
        "unknown_tool",
        "session_required",
        "relay_refused",
        "identity_required",
        "identity_not_allowed",
        "forbidden",
        "fleetlink_seat_disabled",
        "fleetlink_refused",
        "dm_not_allowed",
        "budget_exhausted",
    )
)
_REMOTE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_REMOTE_UNKNOWN = "; the write may or may not have been applied, check before sending it again"


def _remote_visible(text: str) -> str:
    """Hosted text with control characters (other than newline and tab) shown as \\xNN, so it cannot drive the terminal."""
    return _REMOTE_CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), text)


class _TransportError(Exception):
    """The request did not produce an HTTP answer.  `sent` is False only when it provably never left
    (name lookup, refused connection, certificate check);  every other stage is treated as sent."""

    def __init__(self, sent: bool, reason: str = "") -> None:
        super().__init__(reason)
        self.sent = sent
        self.reason = reason


def _remote_post(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, str, bytes]:
    """The one network call of the remote transport:  (status, content type, body).  Tests replace this.
    Redirects are never followed (the Authorization header must not leave the fixed origin), the body is
    read under one total deadline and one byte cap."""
    deadline = time.monotonic() + timeout
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RemoteNoRedirect())
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with op.open(req, timeout=timeout) as r:
            chunks, total = [], 0
            sock = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
            if sock is None:
                # Without the socket the remaining budget cannot be applied to each read, so a stalled body could
                # outlast the deadline:  fail closed rather than read unbounded.
                raise _TransportError(True, "transport")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _TransportError(True, "deadline")
                sock.settimeout(remaining)
                chunk = r.read1(65536)
                if time.monotonic() > deadline:
                    raise _TransportError(True, "deadline")
                if not chunk:
                    break
                total += len(chunk)
                if total > REMOTE_MAX_BYTES:
                    raise _TransportError(True, "too large")
                chunks.append(chunk)
            return r.status, r.headers.get("Content-Type", ""), b"".join(chunks)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, "", b""
    except urllib.error.URLError as exc:
        unsent = isinstance(
            exc.reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)
        )
        raise _TransportError(not unsent, "unreachable") from None
    except _TransportError:
        raise
    except (http.client.HTTPException, OSError, ValueError):
        raise _TransportError(True, "transport") from None


class _RemoteNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _remote_depth_ok(text: str) -> bool:
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > REMOTE_MAX_DEPTH:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def _remote_nodes_ok(value) -> bool:
    stack, count = [value], 0
    while stack:
        item = stack.pop()
        count += 1
        if count > REMOTE_MAX_NODES:
            return False
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def _remote_events(ctype: str, raw: bytes) -> list[str]:
    """The JSON texts of one answer:  the body itself, or each Server-Sent Event (its `data:` lines joined by a
    newline, as the SSE format defines, comments and other fields ignored)."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" not in ctype.lower():
        return [text]
    events, current = [], []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if current:
                events.append("\n".join(current))
                current = []
        elif line.startswith("data:"):
            value = line[5:]
            current.append(value.removeprefix(" "))
    if current:
        events.append("\n".join(current))
    return events


def _remote_code(value) -> str:
    """A server error code, only when it is one of the codes the hosted server defines;  anything else is "error"."""
    return value if isinstance(value, str) and value in _REMOTE_CODES else "error"


def _remote_error_object(result: dict, sc) -> dict:
    """The hosted server's error object.  The server answers a tool error with isError and NO structuredContent: the
    object (code, retry_after_s, ...) is in `_meta["agent-sync/error"]` and, as JSON, in the first text part.  A
    structuredContent that carries a code still wins.  Only the code and the retry-after are ever shown, and
    the code only when it is allow-listed, so nothing the server wrote is copied."""
    if isinstance(sc, dict) and "code" in sc:
        return sc
    meta = result.get("_meta")
    cand = meta.get("agent-sync/error") if isinstance(meta, dict) else None
    if not isinstance(cand, dict):
        cand = None
        for part in result.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                try:
                    obj = json.loads(part["text"])
                except (ValueError, RecursionError):
                    obj = None
                cand = obj if isinstance(obj, dict) else None
                break
    return cand if isinstance(cand, dict) else (sc if isinstance(sc, dict) else {})


def _remote_answer(ctype: str, raw: bytes, tool: str, write: bool):
    """The tools/call result of one answer, or (None, message).  Returns (result, "") on success.  Only an
    allow-listed code and a bounded retry-after leave this function:  the server's own message text is
    untrusted and can echo the caller's input, so it is never copied into an error."""
    unknown = _REMOTE_UNKNOWN if write else ""
    msg = None
    for text in _remote_events(ctype, raw):
        if not _remote_depth_ok(text):
            return None, "the hosted endpoint sent an answer that nests too deeply" + unknown
        try:
            candidate = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("jsonrpc") == "2.0"
            and type(candidate.get("id")) is int
            and candidate["id"] == 1
            and ("result" in candidate) != ("error" in candidate)
        ):
            msg = candidate
            break
    if msg is None:
        return None, "the hosted endpoint sent no readable answer to the call" + unknown
    if not _remote_nodes_ok(msg):
        return None, "the hosted endpoint sent an oversized answer" + unknown
    if "error" in msg:
        err = msg["error"]
        code = _remote_code(err.get("code")) if isinstance(err, dict) else "error"
        return None, "the hosted endpoint refused the call" + (
            "" if code == "error" else f" ({code})"
        )
    result = msg.get("result")
    if not isinstance(result, dict):
        return None, "the hosted endpoint sent no result" + unknown
    content = result.get("content")
    sc = result.get("structuredContent")
    if (
        (content is not None and not isinstance(content, list))
        or any(
            not isinstance(p, dict) or ("text" in p and not isinstance(p["text"], str))
            for p in content or []
        )
        or (sc is not None and not isinstance(sc, dict))
        or not isinstance(result.get("isError", False), bool)
    ):
        return None, "the hosted endpoint sent a result in an unexpected shape" + unknown
    if result.get("isError"):
        eo = _remote_error_object(result, sc)
        code = _remote_code(eo.get("code"))
        retry = eo.get("retry_after_s", eo.get("retry_after"))
        after = (
            f", retry after {retry}s"
            if isinstance(retry, int) and not isinstance(retry, bool) and 0 <= retry <= 86400
            else ""
        )
        note = _REMOTE_UNKNOWN if write and code == "outcome_unknown" else ""
        return None, f"{tool} failed on the hosted server: {code}{after}{note}"
    return result, ""


def _remote_failure(exc: _TransportError, write: bool) -> str:
    if write and exc.sent:
        return (
            "the hosted endpoint did not answer"
            + _REMOTE_UNKNOWN
            + " (no direct fallback in remote mode)"
        )
    return "the hosted endpoint could not be reached (no direct fallback in remote mode)"


def _remote_status_message(status: int, write: bool) -> str:
    if status in (401, 403):
        return "the hosted endpoint refused this bearer (not valid, expired, paused or not accepted for MCP)"
    return f"the hosted endpoint answered HTTP {status}" + (
        _REMOTE_UNKNOWN if write and (status >= 500 or status == 408) else ""
    )


def _remote_scan_arguments(scanner, arguments, bearer: str) -> str | None:
    """The first argument field the fleet secret scanner flags, as `secret_scan:<field>` (the field name only,
    never the text or any part of it), or None.  Every string at any depth is checked, and the bearer itself
    always matches."""
    stack = [("", arguments)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, str):
            if scanner.scan(value, known=[bearer]) is not None:
                return "secret_scan:" + (path or "arguments")
        elif isinstance(value, dict):
            stack.extend((f"{path}.{k}" if path else str(k), v) for k, v in value.items())
        elif isinstance(value, (list, tuple)):
            stack.extend((f"{path}[{i}]", v) for i, v in enumerate(value))
    return None


class RemoteError(Exception):
    pass


def _remote_call(
    env: Mapping[str, str], tool: str, arguments: dict[str, Any], write: bool = False
) -> dict[str, Any]:
    bearer = str(env.get(REMOTE_BEARER_ENV, "")).strip()
    if not bearer:
        raise RemoteError(
            f"remote mode needs this bot's own bearer in {REMOTE_BEARER_ENV}; there is no fallback "
            "to direct credentials"
        )
    if not REMOTE_BEARER_RE.fullmatch(bearer):
        raise RemoteError(
            f"{REMOTE_BEARER_ENV} is not a bearer token (unexpected characters or length)"
        )
    try:
        from . import secretscan
    except (ImportError, OSError):
        raise RemoteError(
            "the secret scanner is not available, so nothing was sent (remote mode has no unscanned route)"
        ) from None
    hit = _remote_scan_arguments(secretscan, arguments, bearer)
    if hit:
        raise RemoteError(f"refused before sending: {hit}")
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ).encode()
    try:
        status, ctype, raw = _remote_post(
            REMOTE_MCP_URL,
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": "Bearer " + bearer,
                "User-Agent": "simple-chat-remote/1",
            },
            body,
            REMOTE_TIMEOUT_S,
        )
    except _TransportError as exc:
        raise RemoteError(_remote_failure(exc, write)) from None
    if status != 200:
        raise RemoteError(_remote_status_message(status, write))
    result, problem = _remote_answer(ctype, raw, tool, write)
    if problem:
        raise RemoteError(problem)
    return result


def _remote_write_args(
    rt: Runtime, cmd: str, args: argparse.Namespace
) -> tuple[str, dict[str, Any]]:
    """post / reply / react / dm to the hosted zulip_* tool of the same meaning.  A write needs its own switch
    (SIMPLE_CHAT_REMOTE_WRITES=1) on top of SIMPLE_CHAT_REMOTE=1;  the hosted server still applies its own
    tagging, secret scan, recipient rules and rate limits.  Options with no hosted equivalent are refused."""
    if str(rt.env.get("SIMPLE_CHAT_REMOTE_WRITES", "")).strip() != "1":
        raise RemoteError(
            f"`{cmd}` is a write:  set SIMPLE_CHAT_REMOTE_WRITES=1 as well as SIMPLE_CHAT_REMOTE=1 "
            "to send it to the hosted server (it is not sent directly instead)"
        )
    for flag, bad in (
        ("--fleet", "fleet"),
        ("--to-owner", "to_owner"),
        ("--no-tag", "no_tag"),
        ("--quiet", "quiet"),
        ("--owner", "owner"),
        ("--body-file", "body_file"),
    ):
        if getattr(args, bad, False):
            raise RemoteError(f"{flag} has no hosted equivalent in remote mode")
    if cmd == "react":
        return "zulip_react", {"message_id": args.id, "emoji": args.emoji}
    words = list(getattr(args, "text", None) or [])
    text = rt.stdin.read() if words == ["-"] else " ".join(words)
    if not text.strip():
        raise RemoteError(f"`{cmd}` needs message text (an argument, or - for stdin)")
    if cmd == "post":
        targs: dict[str, Any] = {"channel": args.channel, "topic": args.topic, "text": text}
        if args.to:
            targs["to"] = list(args.to)
        return "zulip_post", targs
    if cmd == "reply":
        targs = {"message_id": args.id, "text": text}
        if args.to:
            targs["to"] = list(args.to)
        return "zulip_reply", targs
    ids = [t for t in args.to if re.fullmatch(r"[0-9]+", t)]
    emails = [t for t in args.to if "@" in t]
    if not args.to or len(ids) + len(emails) != len(args.to):
        raise RemoteError(
            "remote dm needs --to with numeric user ids or emails (names, seat tags and the "
            "owner default are not resolved in remote mode)"
        )
    targs = {"text": text}
    if ids:
        targs["user_ids"] = [int(i) for i in ids]
    if emails:
        targs["emails"] = emails
    if getattr(args, "clarifying_question", False):
        targs["clarifying_question"] = True
    return "zulip_dm_send", targs


_REMOTE_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")


def _remote_write_identity(env: Mapping[str, str]) -> str:
    """The identity a hosted write names.  The server requires every write to say which bot it acts as
    (docs/protocols/per-call-identity.md) and answers `identities` with the connection's own default, fixed by the
    bearer;  that value is named, so no seat is guessed locally and a bearer can only act as its own allowed set.
    A server that has no `identities` tool predates per-call identity and gets the write unchanged.  Any other
    failure, or an answer with no usable default, refuses the write before it is sent."""
    try:
        res = _remote_call(env, "identities", {}, write=False)
    except RemoteError as exc:
        if "unknown_tool" in str(exc):
            return ""
        raise
    info = res.get("structuredContent")
    info = info if isinstance(info, dict) else {}
    who, seat, listed = (
        info.get("default_identity"),
        info.get("authenticated_seat"),
        info.get("identities"),
    )
    names = (
        [r.get("identity") for r in listed if isinstance(r, dict)]
        if isinstance(listed, list)
        else []
    )
    ok = (
        isinstance(who, str)
        and _REMOTE_IDENTITY_RE.fullmatch(who)
        and who == seat
        and isinstance(listed, list)
        and len(names) == len(listed)
        and all(isinstance(n, str) for n in names)
        and names.count(who) == 1
    )
    if not ok:
        raise RemoteError(
            "the hosted server's identity answer did not match this bearer's own seat and allowed "
            "set, so the write was not sent"
        )
    return who


def _remote_main(rt: Runtime, args: argparse.Namespace) -> int:
    cmd = getattr(args, "command", None) or getattr(args, "cmd", None)
    try:
        if cmd not in REMOTE_VERBS:
            raise RemoteError(
                f"`{cmd}` is not available in remote mode (SIMPLE_CHAT_REMOTE=1 supports only "
                f"{', '.join(REMOTE_VERBS)}); it is not run directly either"
            )
        for flag, bad in (
            ("--as", getattr(args, "as_seat", None)),
            ("--default-seat", getattr(args, "default_seat", None)),
            ("--rc", getattr(args, "rc", None)),
            ("--session", getattr(args, "session", None)),
        ):
            if bad:
                raise RemoteError(
                    f"{flag} has no meaning in remote mode:  the hosted server fixes the seat from the bearer"
                )
        if cmd == "whoami":
            tool, targs = "whoami", {}
        elif cmd == "channels":
            tool, targs = "zulip_channels", {"all": bool(getattr(args, "all", False))}
        elif cmd == "topics":
            tool, targs = "zulip_topics", {"channel": args.channel, "limit": args.limit}
        elif cmd == "read":
            if getattr(args, "new", False):
                raise RemoteError(
                    "--new uses a local cursor and has no hosted equivalent in remote mode"
                )
            tool, targs = (
                "zulip_read_topic",
                {"channel": args.channel, "topic": args.topic, "limit": args.limit},
            )
            if getattr(args, "since", None) is not None:
                targs["since_id"] = args.since
            if getattr(args, "include_self", False):
                targs["include_self"] = True
        elif cmd in REMOTE_WRITE_VERBS:
            tool, targs = _remote_write_args(rt, cmd, args)
        else:
            if getattr(args, "local", False):
                raise RemoteError(
                    "--local reads the listener's local files and has no hosted equivalent"
                )
            if not getattr(args, "peek", False):
                raise RemoteError(
                    "remote inbox cannot advance the local cursor:  pass --peek to read without it"
                )
            tool, targs = "zulip_inbox", {"limit": args.limit}
        if cmd in REMOTE_WRITE_VERBS:
            who = _remote_write_identity(rt.env)
            if who:
                targs = {**targs, "identity": who}
        result = _remote_call(rt.env, tool, targs, write=cmd in REMOTE_WRITE_VERBS)
    except RemoteError as exc:
        print(f"{rt.prog}: {exc}", file=rt.stderr)
        return 2
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"structured": result.get("structuredContent"), "content": result.get("content")},
                indent=2,
            ),
            file=rt.stdout,
        )
        return 0
    printed = False
    for part in result.get("content") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            print(_visible(part["text"]), file=rt.stdout)
            printed = True
    if not printed:
        print(json.dumps(result.get("structuredContent"), indent=2), file=rt.stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "simple-chat",
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    home: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    stop: threading.Event | None = None,
    timeout: float = 30.0,
    events_timeout: float = 100.0,
) -> int:
    rt = Runtime(
        env=os.environ if env is None else env,
        stdin=stdin or sys.stdin,
        stdout=stdout or sys.stdout,
        stderr=stderr or sys.stderr,
        prog=prog,
        home=home,
        sleep=sleep or time.sleep,
        stop=stop,
        timeout=timeout,
        events_timeout=events_timeout,
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    # The hook fast path belongs to `simple-chat` alone:  attach/detach are listener verbs, so a
    # command that does not own them (simple-notes) must fall through to the parser and be refused.
    spec = COMMAND_SPECS.get(prog)
    if (
        prog == "simple-chat"
        and str(rt.env.get("SIMPLE_CHAT_REMOTE", "")).strip() == "1"
        and argv
        and argv[0] in HOOK_VERBS
    ):
        print(
            f"{prog}: `{argv[0]}` is a local listener verb and is not available in remote mode "
            "(SIMPLE_CHAT_REMOTE=1); it is not run locally either",
            file=rt.stderr,
        )
        return 2
    if argv and argv[0] in HOOK_VERBS and spec is not None and argv[0] in spec["listener"]:
        from . import attach

        return attach.main(
            argv[1:],
            command=argv[0],
            env=rt.env,
            stdin=rt.stdin,
            stdout=rt.stdout,
            stderr=rt.stderr,
        )
    parser = build_parser_for(prog, Z.default_channel(rt.env))
    try:
        with contextlib.redirect_stdout(rt.stdout), contextlib.redirect_stderr(rt.stderr):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)

    args = _resolve_channel_args(args, rt.env)
    if prog == "simple-chat" and str(rt.env.get("SIMPLE_CHAT_REMOTE", "")).strip() == "1":
        return _remote_main(rt, args)

    previous = None
    if threading.current_thread() is threading.main_thread():
        with contextlib.suppress(ValueError, OSError):
            previous = signal.signal(signal.SIGTERM, _on_sigterm)
    agent: Agent | None = None
    # The write audit (identity_log) happens exactly once, in the `finally` below, whatever the exit:  each path only
    # records what to write in `audit` (the exit code and an optional outcome).  `_audit_write` itself decides whether
    # the command is a write.  Only a verified key counts for the failure paths:  before that nothing was sent.
    audit: tuple[int, str | None] | None = None
    try:
        overrides = DISPATCH_OVERRIDES.get(prog, {})
        handler = overrides.get(args.command) or LOCAL_COMMANDS.get(args.command)
        if handler is not None:
            return handler(rt, args)
        if args.command == "inbox" and args.local:
            return listener_cli.cmd_inbox_local(rt, args)
        if (
            args.command == "mcp"
        ):  # same credential order, but the seat is pinned and checked against users/me
            from .mcp import stdio as mcp_stdio

            return mcp_stdio.run(rt, args)
        if args.command in ("post", "reply", "dm", "edit"):
            _check_text_args(args)  # a usage error needs no credential
        agent = Agent(
            rt, args
        )  # every request after users/me waits for agent.verify(); whoami reports it
        _check_agent_identity(rt, agent, args.command)
        code = COMMANDS[args.command](agent, args)
        audit = (code, None)
        return code
    except Z.AgentSyncError as exc:
        rt.err("%s: %s" % (rt.prog, exc))
        if agent is not None and agent._verified:
            audit = (exc.exit_code, None)
        return exc.exit_code
    except KeyboardInterrupt:
        if agent is not None and agent._verified:
            audit = (130, "interrupted")  # a write that was in flight may have landed
        return 130
    except BrokenPipeError:
        # The reader went away while the result was being printed.  A write has landed by then.
        if agent is not None and agent._verified:
            audit = (0, None)
        return 0
    except Exception as exc:  # noqa: BLE001 - last resort: say what failed, never print a traceback
        text = "%s: %s" % (type(exc).__name__, exc)
        rt.err("%s: internal error: %s" % (rt.prog, agent.client.scrub(text) if agent else text))
        if agent is not None and agent._verified:
            # Anything unexpected after the key was verified (a failed print, a full disk) may have followed a write that
            # landed:  the outcome is unknown, and only the exception's type name is recorded.
            audit = (1, "outcome_unknown:%s" % type(exc).__name__)
        return 1
    finally:
        if audit is not None and agent is not None:
            with contextlib.suppress(
                Exception
            ):  # an audit failure never changes the command's result
                _audit_write(rt, args, agent, audit[0], audit[1])
        if previous is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
