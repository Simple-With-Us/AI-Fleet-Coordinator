"""`needs-owner notify`:  the fallback notifier, the one-tap and typed answers, the Grok Bot mirror.

It runs as CLAUDE every five minutes (com.jay.needs-owner-notify) and does these things, in this order:

  1. Reads the open items from Linear (a failed read sends nothing and exits 0) and re-validates each one (the block
     parses, the seat is verified, the question and options still hash to the uid, no secret or personal data).
  2. Replies.  For each item whose OWNER topic is open it looks for an answer, in this order, and records the first:
       - a typed message that `agent-sync verify-owner-msg` verifies (the owner's id, a human client, the topic, newer
         than the notice);  a message that is only `pause` or `resume` controls the notifier and is never an answer;
       - a tap:  a reaction on the notice from the pinned owner's user id, a standard keycap emoji, on a low-stakes option;
       - a Linear comment whose author id is OWNER_LINEAR_USER_ID and which is not an app's.
     The record is one Linear comment (`owner replied:  msg|reaction|comment ...`) written by CLAUDE.  It names where the
     reply is and never says what it was.  Nothing here closes an item or acts on an answer.
  3. Notices.  An item still `notify_pending` after 2 minutes is posted from CLAUDE "for <SEAT>" (urgent first).
  4. Notes.  One directed note wakes the filing seat ("Jay replied"), or, for CLAUDE's own items, one entry in
     pending-CLAUDE.json (a post that mentions yourself wakes nobody).  A note that cannot go out yet stays due.
  5. Nudges.  Two different reactions: "ask again" once.  A tap on an option that is not low-stakes: "needs a typed
     reply" once.
  6. The Grok Bot mirror.  An OWNER topic that a Grok Bot persona opened with a fenced `needs-owner-request` JSON block
     becomes a Linear item filed by CLAUDE "for GB-<ROLE>" (`needs-owner file --mirror-of`), and the link goes back
     into the topic.

Every Zulip post counts against the caps (urgent per hour, per hour, per day:  NEEDS_OWNER_CAPS).  The pause (the knob
NEEDS_OWNER_NOTIFY_PAUSED, or Jay's verified `pause` in an OWNER topic of an open item, for 24 hours or until his
`resume`) stops every post and every Linear item the notifier would make;  replies are still recorded, in Linear
only, and their notes wait.  One run at a time (a lock).  The log is ids and reason words, never a question, an
option, a message or an answer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io as iolib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import f3
from . import items as itemsmod
from . import ledger
from . import links as linkmod
from . import notify as notifymod
from . import records
from . import replies
from . import safety
from . import text as T
from .errors import BoardFailed, NeedsOwnerError, Refusal
from .items import Item, NOTIFIER_SEAT
from .proc import Runner

SEAT = NOTIFIER_SEAT
SESSION = notifymod.SESSION
CHANNEL = notifymod.CHANNEL
NOTICE_DELAY = 120.0  # seconds a notify_pending item waits before CLAUDE sends it
READ_LIMIT = 50  # messages read after a notice
VERIFY_PER_TOPIC = 10
MIRRORS_PER_RUN = 3
TOPIC_SCANS_PER_RUN = 10
TOPIC_LIST_LIMIT = 60
NOTE_TRIES = 12
STREAM_ID_TTL = 24 * 3600
MIRROR_APP = "AFC"  # the team a mirrored item is filed in (the persona's own `app` is not used)
_SAFE = re.compile(r"[^A-Za-z0-9_.:/=-]")
_GB_SEAT = re.compile(r"GB-[A-Z0-9]+(?:-[A-Z0-9]+)*")


class NotifierStop(NeedsOwnerError):
    """The notifier cannot run safely (its knobs are refused or missing).  Nothing was sent."""


# ---- the knobs ----------------------------------------------------------------------------------

class FleetKnobs:
    """The K1 knobs through `fleet_knobs` (Infisical, with its last-known-good copy).  The stable copy carries the package."""

    def __init__(self) -> None:
        try:
            from fleet_knobs import knobs  # noqa: PLC0415 - only the notifier needs it
        except ImportError as exc:
            raise NotifierStop("fleet_knobs is missing from this copy of needs-owner (%s);  run scripts/needs_owner/install.sh --apply"
                               % type(exc).__name__) from None
        self._knobs = knobs

    def get(self, name: str) -> Any:
        return self._knobs.get(name)

    def stale(self) -> bool:
        return bool(self._knobs.report().get("stale"))


@dataclass
class Settings:
    paused: bool
    caps: dict
    owner_linear_id: str
    stale: bool


def read_settings(knobs: Any) -> Settings:
    try:
        paused = bool(knobs.get("NEEDS_OWNER_NOTIFY_PAUSED"))
        caps = ledger.read_caps(knobs.get("NEEDS_OWNER_CAPS"))
        owner = str(knobs.get("OWNER_LINEAR_USER_ID") or "").strip().lower()
        stale = bool(knobs.stale())
    except NotifierStop:
        raise
    except Exception as exc:  # noqa: BLE001 - a refused environment or an odd knob stops the run, loudly and without a send
        raise NotifierStop("the knobs cannot be read (%s):  nothing was sent" % type(exc).__name__) from None
    return Settings(paused, caps, owner, stale)


# ---- events ---------------------------------------------------------------------------------------

class Events:
    """What happened, as ids and words.  One line each on stdout (the LaunchAgent's log), or one JSON document at the end."""

    def __init__(self, out: Any, now: Callable[[], dt.datetime], json_mode: bool) -> None:
        self.out = out
        self.now = now
        self.json_mode = json_mode
        self.rows: list[dict] = []

    @staticmethod
    def _safe(value: object) -> object:
        if isinstance(value, (bool, int)) or value is None:
            return value
        return _SAFE.sub("?", str(value))[:60]

    def add(self, word: str, ident: str | None = None, **ids: object) -> None:
        row: dict = {"event": word}
        if ident:
            row["id"] = self._safe(ident)
        row.update({k: self._safe(v) for k, v in ids.items() if v is not None})
        self.rows.append(row)
        if not self.json_mode:
            parts = [self.now().isoformat(timespec="seconds"), word] + ([row["id"]] if ident else [])
            parts += ["%s=%s" % (k, v) for k, v in row.items() if k not in ("event", "id")]
            self.out.write("  ".join(str(p) for p in parts) + "\n")
            self.out.flush()

    def count(self, word: str) -> int:
        return sum(1 for r in self.rows if r["event"] == word)


# ---- the run ---------------------------------------------------------------------------------------

def parse_time(raw: object) -> dt.datetime | None:
    try:
        stamp = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


class Notifier:
    def __init__(self, io: Any, runner: Runner, state: ledger.State, settings: Settings, now: Callable[[], dt.datetime],
                 events: Events, directory: str, owner_id: int | None, dry_run: bool, lock: ledger.Lock | None) -> None:
        self.io = io
        self.runner = runner
        self.state = state
        self.settings = settings
        self.now = now
        self.events = events
        self.directory = directory
        self.owner_id = owner_id
        self.dry_run = dry_run
        self.lock = lock
        self.caps = ledger.Caps(state, settings.caps)
        self.env = dict(io.environ)
        self.items: list[Item] = []
        self.truncated = False
        self.by_ident: dict[str, Item] = {}
        self.nudges: list[tuple[Item, str, str, str]] = []  # (item, kind, once-key, text)
        self.pending_claude: list[dict] = []
        self.pending_claude_changed = False
        self.paused_by = self._paused_by()

    # ---- small helpers ----------------------------------------------------------------------
    def ts(self) -> float:
        return self.now().timestamp()

    def _paused_by(self) -> str | None:
        if self.settings.paused:
            return "knob"
        if self.state.message_pause():
            return "owner"
        return None

    @property
    def paused(self) -> bool:
        return self.paused_by is not None

    def touch(self) -> None:
        if self.lock:
            self.lock.touch()

    def agent_sync(self, verb: str, *args: str, stdin: str | None = None):
        """`agent-sync VERB --as CLAUDE --session needsown ARGS...`:  the seat and session go right after the verb, because
        everything after a `--` is message text."""
        return self.runner.agent_sync(verb, "--as", SEAT, "--session", SESSION, *args, stdin=stdin)

    # ---- reading Zulip ----------------------------------------------------------------------
    def read(self, topic: str, *, since: int | None = None, limit: int = READ_LIMIT) -> list[dict] | None:
        argv = ["read", "--json", "--channel", CHANNEL, "--topic", topic, "--include-self", "--limit", str(limit)]
        if since:
            argv += ["--since", str(since)]
        result = self.agent_sync(*argv)
        if not result.ok:
            return None
        rows = []
        for line in result.out.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool):
                rows.append(row)
        return sorted(rows, key=lambda r: r["id"])

    def verify(self, message_id: int, topic: str, after: int) -> bool | None:
        """True when `agent-sync verify-owner-msg` verifies the message, False when it says it is not the owner's, None when
        it could not say (the network, a missing owner pin)."""
        result = self.agent_sync("verify-owner-msg", str(message_id), "--channel", CHANNEL, "--topic", topic, "--after", str(after), "--json")
        if result.code == 0 and not result.timed_out:
            return True
        if result.code == 7:
            return False
        return None

    def stream_id(self) -> int | None:
        cached = self.state.data.get("stream_id")
        if isinstance(cached, dict) and self.ts() - float(cached.get("at", 0)) < STREAM_ID_TTL and isinstance(cached.get("id"), int):
            return cached["id"]
        result = self.agent_sync("channels", "--json")
        if not result.ok:
            return None
        try:
            rows = json.loads(result.out)
        except ValueError:
            return None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("name") or "").casefold() == CHANNEL and isinstance(row.get("stream_id"), int):
                self.state.data["stream_id"] = {"id": row["stream_id"], "at": self.ts()}
                self.state.dirty = True
                return row["stream_id"]
        return None

    # ---- posting ------------------------------------------------------------------------------
    def post(self, topic: str, body: str, *, to: str | None = None) -> tuple[bool, int | None]:
        """(went out, message id).  Failures and maybe-sent answers both return False, so the caller retries."""
        argv = ["post", "--json", "--topic", topic]
        if to:
            argv += ["--to", to]
        else:
            argv += ["--to-owner"]
        result = self.agent_sync(*argv, "--", "-", stdin=body)
        if not result.ok:
            return False, None
        return True, notifymod._message_id(result)

    # ---- which items we act on --------------------------------------------------------------------
    def usable(self, item: Item) -> str | None:
        """Why we leave `item` alone (a reason word), or None when we may act on it."""
        b = item.block
        if b is None:
            return "unadopted"
        if not item.seat_verified:
            return "seat_not_verified"
        try:
            safety.check_fields({"the question": b.question, "the context": b.context,
                                 **{"option %d" % n: o for n, o in enumerate(b.options, 1)}})
        except Refusal:
            return "unsafe_text"
        if not b.hash_matches_uid():
            return "edited_after_filing"
        return None

    def target_seat(self, item: Item) -> str:
        """The seat a note wakes:  the Grok Bot persona a mirrored item is for, else the filer."""
        b = item.block
        return (b.for_seat or b.filed_by) if b else SEAT

    def ready_age(self, item: Item) -> float:
        """Seconds since the item was last made ready to announce:  filed, adopted (the block comment) or marked pending."""
        stamps = [parse_time(item.created_at)]
        for comment in item.comments:
            text = comment.get("text") or ""
            if records.is_block_comment(text) or records.parse_pending(text) is not None:
                stamps.append(parse_time(comment.get("created_at")))
        known = [s for s in stamps if s]
        return self.now().timestamp() - max(k.timestamp() for k in known) if known else 0.0

    # ---- the run ----------------------------------------------------------------------------------
    def run(self) -> None:
        if self.state.reset:
            self.events.add("state-reset")
        self.events.add("start", paused=self.paused_by, stale_knobs=self.settings.stale or None, dry_run=self.dry_run or None)
        try:
            self.items, self.truncated = itemsmod.load(self.runner)
        except (BoardFailed, NeedsOwnerError) as exc:
            self.events.add("board-read-failed", code=getattr(exc, "exit_code", 1))
            return
        self.by_ident = {i.identifier: i for i in self.items}
        self.pending_claude = ledger.read_pending_claude(self.directory)
        self.phase("replies", self.replies_phase)
        self.phase("urgent notices", lambda: self.notices_phase(urgent=True))
        self.phase("notes", self.notes_phase)
        self.phase("notices", lambda: self.notices_phase(urgent=False))
        self.phase("nudges", self.nudges_phase)
        self.phase("mirror", self.mirror_phase)
        self.phase("housekeeping", self.housekeeping)

    def phase(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except NotifierStop:
            raise
        except Exception as exc:  # noqa: BLE001 - one phase failing never stops the others, and never prints a traceback
            if self.io.environ.get("NEEDS_OWNER_DEBUG"):
                raise
            self.events.add("error", phase=name.replace(" ", "-"), kind=type(exc).__name__)
        self.touch()

    # ---- replies ------------------------------------------------------------------------------------
    def replies_phase(self) -> None:
        for item in self.items:
            if item.state != "notified":
                continue
            why = self.usable(item)
            if why:
                self.events.add("skip", item.identifier, why=why)
                continue
            try:
                self.look_for_an_answer(item)
            except NotifierStop:
                raise
            except Exception as exc:  # noqa: BLE001
                if self.io.environ.get("NEEDS_OWNER_DEBUG"):
                    raise
                self.events.add("error", item.identifier, kind=type(exc).__name__)
            self.touch()

    def look_for_an_answer(self, item: Item) -> None:
        notice_id, topic = item.notified[0], item.topic
        messages = self.read(topic, since=notice_id - 1) if topic else None
        if messages is None:
            self.events.add("read-failed", item.identifier)
        else:
            if self.typed_answer(item, topic, notice_id, messages):
                return
            if self.tap_answer(item, notice_id, messages):
                return
        self.linear_answer(item)

    def is_owner_sender(self, message: dict) -> bool:
        return self.owner_id is None or message.get("sender_id") == self.owner_id

    def typed_answer(self, item: Item, topic: str, notice_id: int, messages: list[dict]) -> bool:
        candidates = [m for m in messages if m["id"] > notice_id and m.get("is_bot") is False and self.is_owner_sender(m)]
        decided = self.state.data["decided"]
        for message in candidates[:VERIFY_PER_TOPIC]:
            key = str(message["id"])
            if key in decided:
                continue
            verdict = self.verify(message["id"], topic, notice_id)
            if verdict is None:
                self.events.add("verify-failed", item.identifier, msg=message["id"])
                return False
            if verdict is False:
                decided[key] = "rejected"
                self.state.dirty = True
                continue
            word = replies.control_word(message.get("content"))
            if word:
                decided[key] = "control"
                self.state.dirty = True
                self.apply_control(word, message["id"])
                continue
            return self.record_reply(item, "msg", message["id"])
        return False

    def apply_control(self, word: str, message_id: int) -> None:
        if message_id <= int(self.state.data["pause"].get("msg", 0)):
            return
        if self.dry_run:
            self.events.add("would-" + word, msg=message_id)
            return
        self.state.set_pause(word == "pause", message_id)
        self.paused_by = self._paused_by()
        self.events.add(word, msg=message_id)

    def tap_answer(self, item: Item, notice_id: int, messages: list[dict]) -> bool:
        b = item.block
        notice = next((m for m in messages if m["id"] == notice_id), None)
        if notice is None:
            return False
        reactions = notice.get("reactions")
        if not reactions:
            return False
        if self.owner_id is None:
            self.events.add("taps-skipped", item.identifier, why="owner_unknown")
            return False
        taps = replies.owner_taps(reactions, self.owner_id, len(b.options))
        verdict = replies.judge_taps(taps, b.low_stakes)
        if verdict.kind == "answer":
            return self.record_reply(item, "reaction", notice_id, option=taps[0])
        if verdict.kind == "conflict":
            self.nudges.append((item, "ask-again", "ask:%s:%s" % (item.identifier, ",".join(map(str, taps))), replies.ask_again_text(taps)))
        elif verdict.kind == "risky":
            self.nudges.append((item, "typed-reply", "typed:%s:%d" % (item.identifier, taps[0]), replies.typed_reply_text(taps[0])))
        return False

    def linear_answer(self, item: Item) -> bool:
        wanted = self.settings.owner_linear_id
        if not wanted:
            return False
        mine = []
        for comment in item.comments:
            text = comment.get("text") or ""
            author = str(comment.get("author_id") or "").lower()
            if author != wanted or comment.get("author_app") is not False:
                continue  # not the owner's account, or an app (an app's comment is never the owner)
            if (records.parse_notified(text) or records.parse_pending(text) or records.parse_reply(text)
                    or records.is_block_comment(text)):
                continue
            mine.append(comment)
        if not mine:
            return False
        first = min(mine, key=lambda c: (str(c.get("created_at") or ""), str(c.get("id") or "")))
        try:
            records.reply_text("comment", first.get("id"))
        except ValueError:
            self.events.add("skip", item.identifier, why="comment_id")
            return False
        return self.record_reply(item, "comment", first.get("id"))

    def record_reply(self, item: Item, kind: str, ref: object, option: int | None = None) -> bool:
        text = records.reply_text(kind, ref, option)
        if self.dry_run:
            self.events.add("would-record", item.identifier, kind=kind, ref=ref, option=option)
            return True
        # The note is due before the record is written, so a crash between the two cannot lose the wake;  a note is
        # only sent for an item that carries the record, so a record that never got written sends nothing.
        self.state.add_note(item.identifier, kind, str(ref), option)
        self.state.save()
        if not notifymod.record(self.runner, item, text):
            self.state.drop_note(item.identifier)
            self.events.add("record-failed", item.identifier, kind=kind)
            return False
        item.replied = records.parse_reply(text)
        item.replied_by = SEAT
        self.events.add("replied", item.identifier, kind=kind, ref=ref, option=option)
        return True

    # ---- notices (the fallback) ----------------------------------------------------------------------
    def notices_phase(self, *, urgent: bool) -> None:
        if self.paused:
            return
        pending = [i for i in self.items if i.state == "notify_pending" and (i.severity == "P1") == urgent]
        for item in sorted(pending, key=lambda i: (i.created_at or "", i.identifier)):
            if item.pending == "held":
                continue  # adopted with --no-notify:  the filer decided not to announce it yet
            why = self.usable(item)
            if why:
                self.events.add("skip", item.identifier, why=why)
                continue
            if self.ready_age(item) < NOTICE_DELAY:
                continue
            if self.state.backoff(item.identifier) > self.ts():
                continue
            reason = self.caps.room(urgent)
            if reason:
                self.events.add("deferred", item.identifier, cap=reason)
                if reason != "urgent_per_hour":
                    break
                continue
            self.send_notice(item, urgent)
            self.touch()

    def send_notice(self, item: Item, urgent: bool) -> None:
        b = item.block
        header = replies.pending_header(self.target_seat(item), item.reported_by)
        if self.dry_run:
            self.events.add("would-notify", item.identifier, urgent=urgent or None)
            return
        notice = notifymod.notify(self.runner, item, rescan=True, environ=self.env, header=header)
        if notice.state == "notified" and notice.already:
            self.events.add("already-notified", item.identifier, msg=notice.msg_id)
            return
        if notice.state == "notified":
            if not b.mirror_of:
                self.state.record_send("notice", item.identifier, urgent)
            self.state.succeeded(item.identifier)
            item.notified = (notice.msg_id, notice.topic)
            self.events.add("notified", item.identifier, msg=notice.msg_id, recorded=notice.recorded)
            return
        fails = self.state.failed(item.identifier)
        self.events.add("notice-failed", item.identifier, reason=notice.reason, fails=fails)

    # ---- notes ----------------------------------------------------------------------------------------
    def notes_phase(self) -> None:
        due = self.state.data["notes"]
        for ident in sorted(due):
            row = due[ident]
            item = self.by_ident.get(ident)
            if item is None or item.state != "replied" or item.block is None:
                self.state.drop_note(ident)
                self.events.add("note-dropped", ident, why="item_closed_or_unrecorded")
                continue
            target = self.target_seat(item)
            if target == SEAT:  # a mention of yourself wakes nobody:  leave it for the next CLAUDE session
                self.add_pending_claude(item, row)
                self.state.drop_note(ident)
                continue
            if self.paused or float(row.get("next", 0)) > self.ts():
                continue
            reason = self.caps.room(False)
            if reason:
                self.events.add("deferred", ident, cap=reason, note=True)
                break
            self.send_note(item, target, row)
            self.touch()

    def send_note(self, item: Item, target: str, row: dict) -> None:
        if self.dry_run:
            self.events.add("would-note", item.identifier, to=target)
            return
        body = replies.note_text(item.identifier, item.url, row["kind"], str(row["id"]))
        sent, message_id = self.post(item.topic or T.topic_for(item.identifier, item.block.question), body, to=target)
        if sent:
            self.state.record_send("note", item.identifier)
            self.state.drop_note(item.identifier)
            self.events.add("note-sent", item.identifier, to=target, msg=message_id)
            return
        row["tries"] = int(row.get("tries", 0)) + 1
        row["next"] = self.ts() + min(3600.0, 300.0 * row["tries"])
        self.state.dirty = True
        if row["tries"] >= NOTE_TRIES:
            self.state.drop_note(item.identifier)
            self.events.add("note-given-up", item.identifier, to=target)
        else:
            self.events.add("note-failed", item.identifier, to=target, tries=row["tries"])

    def add_pending_claude(self, item: Item, row: dict) -> None:
        entry = {"identifier": item.identifier, "url": item.url, "kind": row["kind"], "id": str(row["id"]),
                 "option": row.get("option"), "topic": item.topic, "recorded_at": self.now().isoformat(timespec="seconds")}
        if self.dry_run:
            self.events.add("would-file-pending-claude", item.identifier)
            return
        self.pending_claude = [r for r in self.pending_claude if r.get("identifier") != item.identifier] + [entry]
        self.pending_claude_changed = True
        self.events.add("pending-claude", item.identifier, kind=row["kind"])

    # ---- nudges ---------------------------------------------------------------------------------------
    def nudges_phase(self) -> None:
        if self.paused:
            return
        for item, kind, key, text in self.nudges:
            if item.state != "notified" or self.state.seen_once(key):
                continue
            reason = self.caps.room(False)
            if reason:
                self.events.add("deferred", item.identifier, cap=reason, nudge=kind)
                break
            if self.dry_run:
                self.events.add("would-nudge", item.identifier, kind=kind)
                continue
            sent, message_id = self.post(item.topic, text)
            if sent:
                self.state.mark_once(key)
                self.state.record_send("nudge", item.identifier)
                self.events.add("nudged", item.identifier, kind=kind, msg=message_id)
            else:
                self.events.add("nudge-failed", item.identifier, kind=kind)
            self.touch()

    # ---- the Grok Bot mirror --------------------------------------------------------------------------
    def gb_sender(self, message: dict) -> str | None:
        """The GB seat that sent `message`, only when it is a bot whose email is exactly that persona's
        (`<role>-grok-bot@<this realm>`) and the registry puts the seat in the grok-bot app."""
        seat = message.get("seat")
        if message.get("is_bot") is not True or not isinstance(seat, str) or not _GB_SEAT.fullmatch(seat):
            return None
        if message.get("seat_app") != "grok-bot":
            return None
        expected = "%s-grok-bot@%s" % (seat[3:].lower(), linkmod.realm_host(self.env))
        if str(message.get("sender_email") or "").strip().lower() != expected:
            return None
        return seat

    def mirror_phase(self) -> None:
        if self.paused:
            return
        result = self.agent_sync("topics", "--json", "--channel", CHANNEL, "--limit", str(TOPIC_LIST_LIMIT))
        if not result.ok:
            self.events.add("topics-failed")
            return
        try:
            rows = json.loads(result.out)
        except ValueError:
            self.events.add("topics-failed")
            return
        known = {i.topic.casefold() for i in self.items if i.topic}
        mirrored = {i.block.mirror_message() for i in self.items if i.block and i.block.mirror_of}
        scanned = made = 0
        for row in rows if isinstance(rows, list) else []:
            name, max_id = row.get("name"), row.get("max_id")
            if not isinstance(name, str) or not isinstance(max_id, int) or not name.startswith("OWNER "):
                continue
            if name.casefold() in known or self.state.data["topics"].get(name) == max_id:
                continue
            if scanned >= TOPIC_SCANS_PER_RUN:
                break
            scanned += 1
            messages = self.read(name, since=1, limit=30)
            if messages is None:
                self.events.add("read-failed", topic_scan=True)
                continue
            outcome = self.consider_topic(name, messages, mirrored, made)
            made += 1 if outcome == "mirrored" else 0
            if outcome != "retry":
                self.state.data["topics"][name] = max_id
                self.state.dirty = True
            self.touch()
            if made >= MIRRORS_PER_RUN:
                break

    def consider_topic(self, topic: str, messages: list[dict], mirrored: set, made: int) -> str:
        """What came of an OWNER topic nobody filed an item for:  mirrored, ignored, rejected or retry."""
        if not messages:
            return "ignored"
        opener = self.gb_sender(messages[0])
        if opener is None:
            return "ignored"
        for message in messages:
            if self.gb_sender(message) != opener:
                continue
            try:
                request = replies.parse_request(message.get("content"), opener)
            except replies.RequestProblem as problem:
                key = "badblock:%d" % message["id"]
                if not self.state.seen_once(key) and self.caps.room(False) is None and not self.dry_run:
                    sent, _ = self.post_reply(message["id"], replies.rejected_request_text(problem.reason))
                    if sent:
                        self.state.mark_once(key)
                        self.state.record_send("reject", "-")
                self.events.add("request-rejected", msg=message["id"], reason=problem.reason)
                return "rejected"
            if request is None:
                continue
            if message["id"] in mirrored or str(message["id"]) in self.state.data["mirror"]:
                return "ignored"
            if made >= MIRRORS_PER_RUN or self.caps.room(request.urgent) is not None:
                self.events.add("deferred", msg=message["id"], mirror=True)
                return "retry"
            return self.mirror(topic, message["id"], request)
        return "ignored"

    def post_reply(self, message_id: int, body: str) -> tuple[bool, int | None]:
        result = self.agent_sync("reply", "--json", "--id", str(message_id), "--", "-", stdin=body)
        return result.ok, (notifymod._message_id(result) if result.ok else None)

    def mirror(self, topic: str, message_id: int, request: replies.Request) -> str:
        link = linkmod.make_topic_link(topic, message_id, stream_id=self.stream_id(), environ=self.env)
        if self.dry_run:
            self.events.add("would-mirror", msg=message_id, seat=request.seat)
            return "mirrored"
        from . import verbs  # noqa: PLC0415 - verbs imports this module's siblings

        context = "for %s" % request.seat + (":  %s" % request.context if request.context else "")
        args = argparse.Namespace(
            by=SEAT, app=MIRROR_APP, question=request.question, option=list(request.options),
            low_stakes=[str(n) for n in request.low_stakes], recommend=request.recommend, parent=None, context=context,
            urgent=request.urgent, again=False, mirror_of=link, dry_run=False, json=True)
        out, err = iolib.StringIO(), iolib.StringIO()
        try:
            code = verbs.run_file(args, verbs.Streams(self.io.environ, out, err))
        except Refusal as exc:
            self.state.data["mirror"][str(message_id)] = "refused"
            self.state.dirty = True
            self.events.add("mirror-refused", msg=message_id, why=str(exc)[:40].split(".")[0].replace(" ", "_"))
            return "rejected"
        except NeedsOwnerError as exc:
            self.events.add("mirror-failed", msg=message_id, code=exc.exit_code)
            return "retry"
        if code != 0:
            self.events.add("mirror-failed", msg=message_id, code=code)
            return "retry"
        try:
            record = json.loads(out.getvalue())
        except ValueError:
            record = {}
        ident = str(record.get("identifier") or "")
        self.state.data["mirror"][str(message_id)] = ident or "filed"
        self.state.dirty = True
        if record.get("linkback") == "ok":
            self.state.record_send("mirror", ident or "-", request.urgent)
        self.events.add("mirrored", ident or None, msg=message_id, seat=request.seat, linkback=record.get("linkback"))
        return "mirrored"

    # ---- housekeeping ---------------------------------------------------------------------------------
    def housekeeping(self) -> None:
        if self.dry_run:
            return
        if not self.truncated:  # a partial list would drop live entries
            alive = {i.identifier for i in self.items if i.state == "replied"}
            kept = [r for r in self.pending_claude if r.get("identifier") in alive]
            if len(kept) != len(self.pending_claude):
                self.pending_claude = kept
                self.pending_claude_changed = True
        if self.pending_claude_changed:
            ledger.write_pending_claude(self.directory, self.pending_claude, self.now())
        self.state.data["last_run"] = self.ts()
        self.state.dirty = True


# ---- entry point ----------------------------------------------------------------------------------------

def make_clock(io: Any) -> Callable[[], dt.datetime]:
    if getattr(io, "clock", None):
        return io.clock
    pinned = io.environ.get("NEEDS_OWNER_TEST_NOW")
    if pinned:
        fixed = parse_time(pinned) or dt.datetime.now(dt.timezone.utc)
        return lambda: fixed
    return lambda: dt.datetime.now(dt.timezone.utc)


def run(args: argparse.Namespace, io: Any, seat: str) -> int:
    from .verbs import emit_json  # noqa: PLC0415

    now = make_clock(io)
    knobs = io.knobs if getattr(io, "knobs", None) is not None else FleetKnobs()
    settings = read_settings(knobs)
    directory = ledger.state_dir(io.environ)
    events = Events(io.out, now, bool(args.json))
    lock = None
    if not args.dry_run:
        ledger.private_dir(directory)
        lock = ledger.Lock(os.path.join(directory, ledger.LOCK_FILE))
        if not lock.acquire():
            events.add("locked")
            if args.json:
                emit_json(io, {"ok": True, "locked": True, "events": events.rows})
            return 0
    try:
        runner = Runner(seat, io.environ)
        state = ledger.State(directory, lambda: now().timestamp())
        state.load()
        probe = runner.agent_sync("whoami", "--as", SEAT, "--session", SESSION, "--json")
        owner_id = None
        if probe.ok:
            try:
                value = json.loads(probe.out).get("owner_user_id")
                owner_id = value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
            except (ValueError, AttributeError):
                owner_id = None
        notifier = Notifier(io, runner, state, settings, now, events, directory, owner_id, bool(args.dry_run), lock)
        if not probe.ok:
            events.add("whoami-failed", code=probe.code)
        notifier.run()
        if not args.dry_run and state.dirty:
            state.save()
    finally:
        if lock:
            lock.release()
    if args.json:
        emit_json(io, {"ok": True, "dry_run": bool(args.dry_run), "paused": notifier.paused_by, "stale_knobs": settings.stale,
                       "caps": settings.caps, "sent_last_hour": state.sent_within(3600), "sent_last_day": state.sent_within(86400),
                       "events": events.rows})
    return 0
