"""The Mac listener's consumer for `#notes-relay` (AFC-331, docs/protocols/per-call-identity.md section 5).

Agents that cannot run apple-notes-coding.sh (the server, a hosted client, a phone) post a notes envelope
(notes.py) to #notes-relay as their own bot.  One listener seat on the Mac (`[notes_relay] seat`, default
CLAUDE) reads the channel, validates each request and runs the direct runner, then reacts:

    check mark   the request ran
    cross mark   the request was refused, with a reply `notes-relay refused <request_id>: <reason>`

A request runs only when ALL of these hold, checked here and nowhere else:

  1. the sender is a fleet agent bot:  a bot account whose seat tag (identity.seat_tag_for) is a live seat,
     a BF-* bot or an enabled GB-* persona (identities.agent_identity_problem).  The owner's own account,
     the integration bots (Sentry, Datadog, Linear, PagerDuty, BetterStack) and retired seats are not;
  2. the message holds one well-formed envelope (notes.parse_message);
  3. envelope.identity is the sender's own identity (not merely an allowed one);
  4. the title's Agent is the sender's own name (notes.title_problem);
  5. the request_id was never run before for that sender (a duplicate gets the earlier outcome's reaction
     and never runs twice).

Message content never reaches a shell:  the runner takes an argv list and the body goes on standard input.
Requests are queued to a worker thread, so a slow osascript (or a Notes.app dialog) cannot stall wake
delivery in the listener's seat threads.  The processed ids are kept in <state>/<SEAT>/notes-relay.json
(mode 600, the newest 2000).

Python 3.11+, standard library only.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping

from . import identities as X
from . import identity as I
from . import notes as N
from . import wakes as W
from . import zulip as Z

PROCESSED_LIMIT = 2000
QUEUE_LIMIT = 200
REASON_LIMIT = 300
STATE_FILE = "notes-relay.json"
DEFAULT_SEAT = "CLAUDE"


class Refusal(Exception):
    """A request that must not run.  The text is the reason a sender reads in the reply."""


def sender_identity(person: Mapping[str, Any] | None, owner_user_id: int = 0) -> str:
    """The fleet agent identity of a Zulip user, or Refusal.  `person` is the realm user record (is_bot,
    email, user_id)."""
    if not person:
        raise Refusal("the sender is not a user this listener knows")
    if owner_user_id and person.get("user_id") == owner_user_id:
        raise Refusal("the owner's own account does not send notes requests;  write the note directly")
    if not person.get("is_bot"):
        raise Refusal("only a fleet agent bot may send a notes request")
    tag = I.seat_tag_for(person)
    problem = X.agent_identity_problem(tag)
    if problem:
        raise Refusal("the sender is not a fleet agent identity:  %s" % problem[:120])
    return tag


def validate(message: Mapping[str, Any], person: Mapping[str, Any] | None, *, owner_user_id: int = 0,
             registry_path: str | None = None) -> tuple[str, dict[str, Any]]:
    """(sender identity, envelope) for a request that may run, else Refusal."""
    identity = sender_identity(person, owner_user_id)
    try:
        envelope = N.parse_message(message.get("content"))
    except N.EnvelopeError as exc:
        raise Refusal("not a notes request:  %s" % exc) from None
    if envelope["identity"] != identity:
        raise Refusal("envelope identity %s is not the sender, %s" % (envelope["identity"][:40], identity))
    problem = N.title_problem(envelope["title"], identity, registry_path=registry_path)
    if problem:
        raise Refusal(problem)
    return identity, envelope


class Processed:
    """request ids already run, per sender identity, in a mode-600 file.  Thread-safe."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, rows: Mapping[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump({"rows": rows}, fh, separators=(",", ":"))
        os.replace(tmp, self.path)

    @staticmethod
    def key(identity: str, request_id: str) -> str:
        return "%s:%s" % (identity, request_id)

    def begin(self, identity: str, request_id: str, message_id: int) -> str | None:
        """None when this request is new (now marked `started`), else the earlier outcome
        (started, done or refused)."""
        with self.lock:
            rows = dict(self._read().get("rows") or {})
            key = self.key(identity, request_id)
            if key in rows:
                return str(rows[key].get("outcome") or "started")
            rows[key] = {"outcome": "started", "message_id": message_id, "ts": int(self.clock())}
            if len(rows) > PROCESSED_LIMIT:
                for old in sorted(rows, key=lambda k: rows[k].get("ts", 0))[: len(rows) - PROCESSED_LIMIT]:
                    rows.pop(old, None)
            self._write(rows)
            return None

    def finish(self, identity: str, request_id: str, outcome: str) -> None:
        with self.lock:
            rows = dict(self._read().get("rows") or {})
            key = self.key(identity, request_id)
            if key in rows:
                rows[key]["outcome"] = outcome
                self._write(rows)


class Consumer:
    """One per listener.  `offer(runner, message)` is called from the seat thread that routes the message and
    returns at once;  a worker thread does the rest."""

    def __init__(self, *, seat: str, state_dir: str, script: Callable[[], Path | None], env: Mapping[str, str],
                 owner_user_id: Callable[[], int] = lambda: 0, runner: Callable[..., Any] | None = None,
                 log: Callable[..., None] = lambda event, **fields: None, clock: Callable[[], float] = time.time,
                 registry_path: str | None = None, channel: str = N.RELAY_CHANNEL) -> None:
        self.seat = seat
        self.channel = channel
        self.script = script
        self.env = dict(env)
        self.owner_user_id = owner_user_id
        self.runner = runner
        self.log = log
        self.registry_path = registry_path
        self.processed = Processed(os.path.join(state_dir, seat, STATE_FILE), clock)
        self.jobs: "queue.Queue[tuple[Any, dict[str, Any]] | None]" = queue.Queue(QUEUE_LIMIT)
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.dropped = 0

    # ---- the seat thread's side ---------------------------------------------------------------
    def wants(self, runner: Any, message: Mapping[str, Any]) -> bool:
        """Is this a message in the relay channel that this seat's reader should consume?"""
        return (runner.seat == self.seat and message.get("type") == "stream"
                and Z.message_channel(message).casefold() == self.channel
                and message.get("sender_id") != getattr(runner.me, "user_id", None))

    def offer(self, runner: Any, message: Mapping[str, Any]) -> None:
        self.start()
        try:
            self.jobs.put_nowait((runner, dict(message)))
        except queue.Full:
            self.dropped += 1
            self.log("notes-relay-dropped", id=message.get("id"), dropped=self.dropped)

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._work, name="notes-relay", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass

    # ---- the worker ---------------------------------------------------------------------------
    def _work(self) -> None:
        while not self.stopped.is_set():
            job = self.jobs.get()
            if job is None:
                return
            runner, message = job
            try:
                self.process(runner, message)
            except Exception as exc:  # noqa: BLE001 - one bad request must not end the worker
                self.log("notes-relay-error", id=message.get("id"), error=type(exc).__name__)

    def drain(self) -> None:
        """Process every queued job on the calling thread (tests)."""
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                self.process(*job)

    def process(self, runner: Any, message: Mapping[str, Any]) -> str:
        """Handle one relay message.  Returns done, refused, duplicate or ignored."""
        message_id = int(message["id"])
        person = runner.users.get(message.get("sender_id"))
        try:
            identity, envelope = validate(message, person, owner_user_id=int(self.owner_user_id() or 0),
                                          registry_path=self.registry_path)
        except Refusal as exc:
            self.log("notes-relay-refused", id=message_id, reason=str(exc)[:120])
            self._answer(runner, message_id, None, refused=str(exc))
            return "refused"
        request_id = envelope["request_id"]
        earlier = self.processed.begin(identity, request_id, message_id)
        if earlier is not None:
            self.log("notes-relay-duplicate", id=message_id, earlier=earlier)
            if earlier == "done":
                self._react(runner, message_id, N.DONE_EMOJI)
            elif earlier == "started":
                self._answer(runner, message_id, request_id, refused="this request id was already started and may have "
                             "been interrupted;  check the note, then send it again with a new request_id")
            else:
                self._answer(runner, message_id, request_id, refused="this request id was already refused")
            return "duplicate"
        try:
            script = self.script()
            if script is None:
                raise N.NotesError("unavailable", "this Mac cannot run the notes script right now")
            full = N.resolve_upload(envelope, lambda path: self._download(runner, path))
            kwargs = {"runner": self.runner} if self.runner is not None else {}
            N.run_direct(full, script, env=self.env, **kwargs)
        except N.NotesError as exc:
            self.processed.finish(identity, request_id, "refused")
            self.log("notes-relay-failed", id=message_id, code=exc.code)
            self._answer(runner, message_id, request_id, refused=exc.message)
            return "refused"
        except Z.AgentSyncError as exc:
            self.processed.finish(identity, request_id, "refused")
            self.log("notes-relay-failed", id=message_id, code="zulip")
            self._answer(runner, message_id, request_id, refused="could not fetch the attached body:  %s" % str(exc)[:100])
            return "refused"
        self.processed.finish(identity, request_id, "done")
        self.log("notes-relay-done", id=message_id, action=envelope["action"], identity=identity)
        self._react(runner, message_id, N.DONE_EMOJI)
        return "done"

    # ---- Zulip --------------------------------------------------------------------------------
    def _react(self, runner: Any, message_id: int, names: tuple[str, ...]) -> None:
        for name in names:
            try:
                runner.client.post("messages/%d/reactions" % message_id, {"emoji_name": name})
                return
            except Z.ApiError as exc:
                if exc.code == "REACTION_ALREADY_EXISTS":
                    return
                continue
            except Z.NetworkError:
                return

    def _answer(self, runner: Any, message_id: int, request_id: str | None, *, refused: str) -> None:
        self._react(runner, message_id, N.REFUSED_EMOJI)
        reason = W.neutralize_mentions(" ".join(refused.split())[:REASON_LIMIT])
        label = request_id or "(no request id)"
        try:
            original = runner.client.get("messages/%d" % message_id, {"apply_markdown": False}).get("message") or {}
            runner.client.post("messages", {"type": "stream", "to": original.get("stream_id"),
                                            "topic": Z.message_topic(original), "content":
                                            "%s%s: %s" % (N.REFUSAL_PREFIX, label, reason)})
        except (Z.ApiError, Z.NetworkError):
            self.log("notes-relay-reply-failed", id=message_id)

    def _download(self, runner: Any, path: str) -> bytes:
        """The uploaded .md at /user_uploads/..., through the reading bot's own credential:  Zulip hands back a
        short-lived signed URL for the file, which is then fetched without credentials."""
        if not N.UPLOAD_PATH_RE.match(path) or ".." in path:
            raise N.NotesError("invalid_argument", "the attachment path is not a /user_uploads/ path")
        answer = runner.client.get("user_uploads/" + path[len("/user_uploads/"):])
        url = str(answer.get("url") or "")
        if not url:
            raise N.NotesError("unavailable", "Zulip gave no download URL for the attachment")
        base = urllib.parse.urlparse(runner.client.realm)
        target = urllib.parse.urlparse(urllib.parse.urljoin(runner.client.realm + "/", url))
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise N.NotesError("unavailable", "the attachment is not on this realm's host")
        return fetch_unauthenticated(runner.client, target.geturl())


def fetch_unauthenticated(client: Any, url: str, limit: int = N.UPLOAD_MAX) -> bytes:
    """GET `url` with no Authorization header (the URL carries its own signature), capped at `limit` bytes."""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": Z.USER_AGENT})
    try:
        with client._opener.open(request, timeout=client.timeout) as response:  # noqa: SLF001 - same redirect-free opener
            data = response.read(limit + 1)
    except OSError as exc:
        raise Z.NetworkError("fetching the attachment failed:  %s" % type(exc).__name__) from None
    if len(data) > limit:
        raise N.NotesError("invalid_argument", "the attachment is larger than %d bytes" % limit)
    return data
