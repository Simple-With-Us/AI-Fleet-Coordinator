#!/usr/bin/env python3
"""lark-mm-bridge.py -- put the MiniMax Code seat MM on a Lark chat surface.

The MiniMax Code Desktop app can bind a Lark/Feishu bot on its own, but that
path is scoped to a projectKey and it drives a device-code app registration
that has to be finished by hand in a browser.  When the registration is not
completed the connection sits in state "pending" forever, no physical binding
is written, no Feishu WebSocket client starts, and the bot never answers.
Changing the project folder forces a new bot, because the binding is keyed on
the project.

This bridge sidesteps both problems.  It talks to Lark with the lark-cli
(Open Platform) WebSocket client and it talks to the seat with the Agent
Client Protocol server that MiniMax Code already exposes, so:

  * one Lark bot, one Lark conversation, one MiniMax Code session that keeps
    its history between messages, and nothing to re-create when the owner
    moves to another folder;
  * no second credential and no browser step, because lark-cli already holds
    the app credentials it needs.

The loop is:

  lark-cli event consume im.message.receive_v1   (WebSocket, NDJSON on stdout)
        -> one MiniMax Code session per Lark chat, turns run in order
        -> lark-cli im +messages-reply              (reply to the same message)

One session per chat means a group chat and a one-to-one chat do not share
context, and each keeps its own conversation.  One global FIFO worker serializes
turns across chats, deliberately bounding model concurrency to one.  A queued
message waits for the previous answer, including turns in another chat.  In a group chat the bot answers only when it is mentioned, so it does not
talk over the humans; in a one-to-one chat every message is for the bot.

Runs on the Mac's /usr/bin/python3 (3.9): stdlib only, no 3.10+ syntax.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

# The secret scanner lives in the managed agent-sync runtime, which is a
# separate checkout and is not on the interpreter's path.  Add it before the
# import, so the bridge does not depend on how it was launched.
_AGENT_SYNC_SCRIPTS = os.path.expanduser("~/apps/lanes/_managed/fleet/agent-sync-runtime/scripts")
if os.path.isdir(_AGENT_SYNC_SCRIPTS) and _AGENT_SYNC_SCRIPTS not in sys.path:
    sys.path.insert(0, _AGENT_SYNC_SCRIPTS)

from agent_sync.secretscan import scan as scan_secret

LARK_EVENT_KEY = "im.message.receive_v1"
DEFAULT_LARK_CLI = os.path.expanduser("~/.local/npm-global/bin/lark-cli")
DEFAULT_MCODE = os.path.expanduser("~/.local/bin/mcode")
DEFAULT_STATE_DIR = os.path.expanduser("~/.minimax/lark-mm-bridge")
DEFAULT_CWD = os.path.expanduser("~/apps")
DEFAULT_TIMEOUT = 1200
# A Lark write is one HTTP call, but lark-cli is a node process and the Mac
# has been at a load of 190, where it took 54 seconds and then lost the
# acknowledgement.  A short timeout here loses the reply.
REPLY_TIMEOUT = 300
# The owner watches Zulip, so every Lark exchange is mirrored there with the
# MiniMax session id, which is the thread the answer came from.
AGENT_SYNC = os.path.expanduser("~/.local/bin/simple-chat")
AGENT_SYNC_ROOT = os.path.expanduser("~/.agent-sync")
ZULIP_TOPIC_PREFIX = "Lark"
MAX_REPLY_CHARS = 4000
# How many delivered message ids the dedup state remembers.  Rows that have
# not reached a final state are kept beyond this window, because dropping one
# is what makes a restart replay it.
DEDUP_WINDOW = 2000
# How many times a reply that provably never left the Mac is retried in one
# process lifetime before the message is left pending for a restart.
MAX_REPLY_ATTEMPTS = 3
# A reply is the one place model output leaves the machine, and the seat has
# whatever the owner's conversations and its retrieved context hold.  Anything
# shaped like a credential is refused rather than forwarded.
# The MiniMax Code ACP server has to start a runtime before it answers
# initialize.  On a loaded Mac that handshake is slow, and a tight timeout
# turns a working seat into "could not answer" (AFC-417).
ACP_HANDSHAKE_TIMEOUT = 300

# The owner's marker for "do not wake the seat for this one".  A message whose
# very first character is the eyes emoji is read and dropped:  no turn, no
# reply, no record of it.  The owner asked for it on Oct 22, 2026;  Jet's
# WhatsApp bridge takes the same marker.
SILENT_MARKER = "\U0001f440"

# The Operator (the agent-sync listener) can put a message into the Lark chat.
# It drops a JSON file into the signal directory and the bridge picks it up,
# sends it to Lark as the bot, and runs the seat turn on it, so the owner gets
# the signal and the answer in one place.  The bridge is the only process that
# holds the Lark bot identity, so nothing else may write into the chat.
SIGNAL_DIRNAME = "signal"
SIGNAL_SUFFIX = ".json"
# The filed signals the done directory keeps.  A long-lived installation would
# otherwise keep every signal it ever handled, and the disk use would grow
# without bound.
SIGNAL_DONE_WINDOW = 200
# A signal that keeps failing is filed rather than retried forever.
MAX_SIGNAL_ATTEMPTS = 5
# Seconds;  the wait between a signal's attempts doubles from here.
SIGNAL_RETRY_BASE = 5
# Seconds;  the longest wait a retry-after value in the CLI's error can ask for.
# A malformed value must not park a signal for hours.
SIGNAL_RETRY_AFTER_MAX = 600
# A signal is not the owner's own message, so it carries its own sender id and
# the seat inbox records the exchange as the Operator's.
OPERATOR_SENDER = "operator"

# The refusal code for a credential-shaped text.  It names the field and
# nothing else:  the text is the thing being kept out of the chat, the disk
# and the log, so it is never echoed with the code.
SECRET_IN_TEXT = "secret_in_text"

# The fields of a third-party error that may be written down.  The raw text of
# a CLI error can carry a response body, a request payload or an
# authorization header, and the detail of a signal is persisted, so every
# field that survives is a scalar from this list and is length-capped.
_ERROR_STATUS_RE = re.compile(r"\b([1-5][0-9]{2})\b")
_ERROR_CODE_RE = re.compile(
    r"\"(?:error_)?code\"\s*:\s*\"?([A-Za-z0-9_.:-]{1,32})\"?", re.IGNORECASE
)
_ERROR_REQUEST_ID_RE = re.compile(
    r"\"(?:request_?id|log_?id|x-tt-logid)\"\s*:\s*\"?([A-Za-z0-9_-]{1,64})\"?",
    re.IGNORECASE,
)
# The wait a rate limit asked for, in seconds.  The bridge runs lark-cli and
# sees only its exit status and its stderr, never the HTTP response, so a wait
# the server sent in a header (Retry-After, or a rate-limit reset header such
# as x-ogw-ratelimit-reset) reaches it only when the CLI writes the value into
# its error text.
_ERROR_RETRY_AFTER_RE = re.compile(
    r"(?:retry[-_ ]?after|x-ogw-ratelimit-reset)\"?\s*[:=]\s*\"?([0-9]{1,6})\b",
    re.IGNORECASE,
)

# The owner's house rule: a visibly wider gap between sentences, after . ! and ?
# only, never after a colon, semicolon, comma or dash.  Lark renders a plain
# text message literally, so two ASCII spaces already show as a wide gap there,
# and the owner asked for exactly that on Oct 19, 2026: "No nbsp here please
# just 2 spaces".  A real U+00A0 is the mechanism for Markdown panes that
# collapse spaces (the MiniMax Code pane, GitHub, Zulip), not for Lark.
SENTENCE_GAP = "  "
# A Lark plain-text message is rendered literally, so two ASCII spaces already
# show as a wide gap there, and the owner asked for exactly that on Oct 19,
# 2026: "No nbsp here please just 2 spaces".  A Markdown pane (Zulip, GitHub)
# collapses spaces instead, so the wide gap there is a real U+00A0 plus a space.
ZULIP_SENTENCE_GAP = "\u00a0 "
_SENTENCE_END = re.compile(r"([A-Za-z]+)([.!?])( +)(?=[\"'(\[A-Z])")
_ABBREVIATIONS = ("mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "no")


def _widen_sentence(match, gap):
    """Normalise the spaces after a sentence end to the wide gap."""
    if match.group(1).lower() in _ABBREVIATIONS:
        return match.group(0)
    return match.group(1) + match.group(2) + gap


def sentence_gap(text, gap=SENTENCE_GAP):
    """Widen the gap after a sentence end, leaving code alone.

    The seat answers in plain prose, and prose carries one space between
    sentences.  Lark shows that as a single space, so every reply the bridge
    sends would break the owner's house rule.  Fenced code blocks and inline
    code spans are left exactly as they are.
    """
    if not text:
        return text
    out = []
    in_fence = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        pieces = re.split(r"(`[^`]*`)", line)
        rebuilt = []
        for i, piece in enumerate(pieces):
            if i % 2 == 0:
                piece = _SENTENCE_END.sub(lambda m: _widen_sentence(m, gap), piece)
            rebuilt.append(piece)
        out.append("".join(rebuilt))
    return "\n".join(out)


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write("[%s] %s\n" % (stamp, message))
    sys.stderr.flush()


class CommandError(Exception):
    pass


class SecretRefusal(Exception):
    """The outbound text looked like a credential, so it was not sent."""


class DeliveryError(Exception):
    """A Lark reply that did not land, with what is known about the write.

    outcome is "retryable" when the request provably never reached Lark,
    "terminal" when Lark refused it outright, and "outcome_unknown" when the
    write may or may not have been delivered -- the three cases cannot share a
    handler, because replaying the third can double-post a reply the chat
    already has.
    """

    def __init__(self, outcome, detail, retry_after=None):
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        # Seconds the server asked the caller to wait, when the error said.
        self.retry_after = retry_after


# Durable per-message delivery states.  "pending" means claimed but not yet
# delivered; "ok" is a delivered reply; "refused" is a definite refusal that
# will not improve; "unknown" is a write whose outcome we cannot establish.
DELIVERY_STATES = ("pending", "ok", "refused", "unknown")

# How long a peer signal waits behind the owner's own turns before it is
# served next.  The owner's message goes first, because the owner is the one
# waiting, but the Operator's signal is never starved by a busy owner.
PEER_TURN_MAX_WAIT = 60.0


def run(command, stdin_text=None, timeout=120, env=None):
    """Run a command and return its stdout.  Raises CommandError on failure."""
    try:
        completed = subprocess.run(
            command,
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CommandError("%s timed out after %ss" % (command[0], timeout))
    except OSError as exc:
        raise CommandError("%s could not start: %s" % (command[0], exc))
    if completed.returncode != 0:
        # The whole stderr, not just its last line: lark-cli prints a JSON
        # error, and the last line of that is "}", which told the reader
        # nothing about what actually went wrong.
        detail = (completed.stderr or "").strip()
        if not detail:
            detail = "exit %s" % completed.returncode
        raise CommandError("%s failed: %s" % (command[0], detail[-500:]))
    return completed.stdout


def lark_env():
    """Put the lark-cli install prefix on PATH so its own helpers resolve."""
    env = dict(os.environ)
    prefix = os.path.expanduser("~/.local/npm-global/bin")
    if os.path.isdir(prefix):
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


def _message_id_from(output):
    """The message id in a lark-cli send answer, or None.

    lark-cli answers with one JSON object holding message_id, chat_id and
    create_time.  A shape this bridge does not know is a missing id, not a
    guess:  the id is what the reply hangs on, and a wrong one posts the
    answer into the wrong place.
    """
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for candidate in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
        for key in ("message_id", "messageId"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _signal_key(signal_id):
    """The idempotency key for one signal.

    lark-cli takes at most 50 characters, and the key must survive a restart
    unchanged, so it is built from the signal's own id.  An id that has to be
    changed keeps a digest of the original, so two ids that differ only in
    punctuation cannot end up sharing one key and one of the signals being
    swallowed by Lark.
    """
    raw = signal_id or ""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    if safe != raw or len(safe) > 50:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        safe = (safe[: 50 - len(digest) - 1] + "-" + digest).strip("-")
    return safe or "signal"


def safe_error_detail(prefix, text):
    """A normalized error name plus the allow-listed fields of the error.

    Only the name of the failure and a status, a code or a request id
    survive.  Everything else in the error text is dropped, because the
    detail of a delivery is written to the done directory and read by
    whoever opens it, and a CLI error can carry a response body, a request
    payload or an authorization header.
    """
    parts = [prefix]
    for name, pattern in (
        ("status", _ERROR_STATUS_RE),
        ("code", _ERROR_CODE_RE),
        ("request_id", _ERROR_REQUEST_ID_RE),
        ("retry_after", _ERROR_RETRY_AFTER_RE),
    ):
        found = pattern.search(text or "")
        if found:
            parts.append(f"{name}={found.group(1)}")
    return " ".join(parts)


def retry_after_from(text):
    """The wait in seconds a rate limit asked for, capped, or None.

    Only a whole number of seconds is read.  The date form of Retry-After
    is not, and a value that is missing or not read leaves the local
    backoff in charge.
    """
    found = _ERROR_RETRY_AFTER_RE.search(text or "")
    if not found:
        return None
    return min(int(found.group(1)), SIGNAL_RETRY_AFTER_MAX)


class TurnQueue:
    """Pending turns, with the owner's own message ahead of a peer's.

    The owner's message in a chat is the thing the owner is waiting for, so
    it is served before a peer signal for the same chat.  A peer is never
    starved:  once it has waited PEER_TURN_MAX_WAIT seconds it is served
    even while owners keep arriving, because a signal that never lands is
    worse than one that arrives late.

    The candidates of one chat keep their own order, so two messages of the
    same chat are answered in the order they were written.
    """

    def __init__(self, owner_senders=(), max_peer_wait=PEER_TURN_MAX_WAIT):
        self._lock = threading.Condition()
        self._owner_senders = set(owner_senders or ())
        self._max_peer_wait = max_peer_wait
        # chat id -> [owner candidates, peer candidates]
        self._chats = {}
        self._chat_order = deque()
        self._unfinished = 0

    def _is_peer(self, sender_id):
        """A peer is the Operator's signal, or anything that is not the owner."""
        return sender_id == OPERATOR_SENDER or sender_id not in self._owner_senders

    def put(self, item):
        """Queue one turn:  a (message id, chat id, text, sender, attempt) tuple."""
        slot = 1 if self._is_peer(item[3]) else 0
        chat_id = item[1]
        with self._lock:
            slots = self._chats.get(chat_id)
            if slots is None:
                slots = [deque(), deque()]
                self._chats[chat_id] = slots
                self._chat_order.append(chat_id)
            slots[slot].append((time.monotonic(), item))
            self._unfinished += 1
            self._lock.notify_all()

    def task_done(self):
        with self._lock:
            if self._unfinished <= 0:
                raise ValueError("task_done() called too many times")
            self._unfinished -= 1

    def qsize(self):
        with self._lock:
            return sum(len(slots[0]) + len(slots[1]) for slots in self._chats.values())

    def empty(self):
        with self._lock:
            return not any(slots[0] or slots[1] for slots in self._chats.values())

    def _select(self):
        """The next turn to serve, or None when there is nothing to serve.

        A peer that has waited long enough goes first, because a signal
        that never lands is worse than one that arrives late.  Otherwise
        the owner's own message goes first, and the peers wait for it.
        """
        now = time.monotonic()
        oldest_peer = None
        overdue_peer = None
        for chat_id in self._chat_order:
            slots = self._chats.get(chat_id)
            if slots is None or not slots[1]:
                continue
            enqueued = slots[1][0][0]
            if oldest_peer is None or enqueued < oldest_peer[0]:
                oldest_peer = (enqueued, chat_id)
            if now - enqueued >= self._max_peer_wait:
                if overdue_peer is None or enqueued < overdue_peer[0]:
                    overdue_peer = (enqueued, chat_id)
        if overdue_peer is not None:
            return self._take(overdue_peer[1], 1)
        for chat_id in self._chat_order:
            slots = self._chats.get(chat_id)
            if slots is not None and slots[0]:
                return self._take(chat_id, 0)
        if oldest_peer is not None:
            return self._take(oldest_peer[1], 1)
        return None

    def _take(self, chat_id, slot):
        slots = self._chats[chat_id]
        _, item = slots[slot].popleft()
        if not slots[0] and not slots[1]:
            self._chat_order.remove(chat_id)
            del self._chats[chat_id]
        return item

    def get(self, timeout=None):
        """The next turn, waiting up to `timeout` seconds for one to arrive."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while True:
                item = self._select()
                if item is not None:
                    return item
                if deadline is None:
                    self._lock.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._lock.wait(remaining)

    def get_nowait(self):
        with self._lock:
            item = self._select()
            if item is None:
                raise queue.Empty
            return item


def _write_json_atomic(path, payload):
    """Write `payload` to `path` atomically.

    A temp file in the same directory, fsynced, renamed over the target, and
    the directory fsynced.  A crash leaves either the old file or the new one
    and never a torn one, so a record that is read later is always whole.

    The temp file is named with a suffix the signal watcher does not match,
    because a temp file left behind by a crash between the two writes would
    otherwise be picked up as a signal and replayed as a second one.
    """
    path = str(path)
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class LarkEventReader:
    """Runs `lark-cli event consume` and yields decoded event objects.

    The consumer treats stdin EOF as an exit signal, which suits an AI
    subprocess caller, so this keeps a pipe open on it for the life of the
    process.  A crash in the consumer is retried, because Lark drops
    WebSocket connections on their own and the bot has to come back.
    """

    def __init__(self, lark_cli, env, log_path=None):
        self.lark_cli = lark_cli
        self.env = env
        self.log_path = log_path
        self.process = None
        self.stopping = threading.Event()

    def _start(self):
        command = [
            self.lark_cli,
            "event",
            "consume",
            LARK_EVENT_KEY,
            "--as",
            "bot",
        ]
        if self.log_path:
            command += ["--output-dir", self.log_path]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.env,
            # Its own session, so shutdown can signal the whole group: the
            # consumer spawns helpers of its own and terminating only the
            # leader leaves them holding the event pipe open.
            start_new_session=True,
        )
        log("lark event consumer started (pid %s)" % self.process.pid)

    def _drain_stderr(self):
        def reader():
            for line in self.process.stderr:
                line = line.strip()
                if line and not line.startswith("[event] ready"):
                    log("lark-cli: %s" % line)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

    def events(self):
        """Yield event dicts forever, reconnecting the consumer when it dies."""
        backoff = 2
        while not self.stopping.is_set():
            if self.process is None or self.process.poll() is not None:
                if self.process is not None:
                    log("lark event consumer exited; restarting in %ss" % backoff)
                self._start()
                self._drain_stderr()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            line = self.process.stdout.readline()
            if not line:
                if self.stopping.is_set():
                    return
                continue
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            backoff = 2
            if isinstance(event, dict) and event.get("type") == LARK_EVENT_KEY:
                yield event

    def stop(self):
        self.stopping.set()
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    pass
        log("lark event consumer stopped")


class MiniMaxSession:
    """One MiniMax Code session, driven over the Agent Client Protocol.

    The session is created lazily so an idle bridge does not hold a session
    open, and it keeps its conversation between turns, so the second message
    in a chat sees the first answer.
    """

    def __init__(self, mcode, cwd, timeout=DEFAULT_TIMEOUT, env=None):
        self.mcode = mcode
        self.cwd = cwd
        self.timeout = timeout
        self.env = env
        self.process = None
        self.session_id = None
        self.next_id = 1
        self.lock = threading.Lock()
        self.buffer = []
        self.buffer_lock = threading.Lock()
        self.spawn = None

    @staticmethod
    def _spawn(command, **kwargs):
        return subprocess.Popen(command, **kwargs)

    def _start(self):
        self.process = (self.spawn or self._spawn)(
            [self.mcode, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.env,
            # Its own process group, so close() can take the seat's helpers
            # down with it instead of leaving them behind.
            start_new_session=True,
        )
        self.next_id = 1
        self.buffer = []
        # _call holds self.lock for the whole request, which would deadlock the
        # pump against it, so stdin writes take their own lock instead.
        self.stdin_lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
            timeout=ACP_HANDSHAKE_TIMEOUT,
        )
        result = self._call(
            "session/new",
            {"cwd": self.cwd, "mcpServers": []},
            timeout=ACP_HANDSHAKE_TIMEOUT,
        )
        self.session_id = (result or {}).get("sessionId")
        if not self.session_id:
            raise CommandError("session/new returned no sessionId")
        log("miniMax session created: %s" % self.session_id)
        return self.session_id

    def _pump(self):
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            # This is the session's only reader, so a frame that raises here
            # would stall every later _call until the prompt timed out.  One
            # malformed frame is skipped; the stream carries on.
            try:
                self._handle_message(message)
            except Exception as exc:
                log("ACP frame not handled: %s" % type(exc).__name__)

    def _handle_message(self, message):
        """Route one message from the ACP server.

        A message with a method and an id is a request the agent sends to the
        client.  A message with neither is a notification, and a message with
        an id and no method is the response to one of our calls.
        """
        # A JSON scalar or a list decodes fine and is not a JSON-RPC message;
        # reading its fields would raise a TypeError with no frame in it.
        if not isinstance(message, dict):
            log("ignoring an ACP frame that is not an object")
            return
        if "method" in message and not isinstance(message["method"], str):
            log("ACP frame refused: code=invalid_method field=method")
            return
        if "id" in message and (type(message["id"]) not in (str, int)):
            return
        if "method" in message and "id" in message:
            # Buffering it instead of answering leaves the turn waiting for a
            # reply that never comes until session/prompt times out.
            self._answer_request(message)
            return
        with self.buffer_lock:
            self.buffer.append(message)

    def _answer_request(self, message):
        """Cancel permission requests unless a trusted approval path is added."""
        method = message.get("method")
        request_id = message.get("id")
        if method == "session/request_permission":
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": {"outcome": "cancelled"}},
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found: %s" % method},
            }
        try:
            with self.stdin_lock:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
        except Exception as exc:
            log("could not answer %s: %s" % (method, exc))

    def _pump_stderr(self):
        """Keep the ACP server's own errors; they are the only clue when a
        handshake times out."""
        for line in self.process.stderr:
            line = line.rstrip()
            if line:
                log("ACP stderr received: code=external_stderr field=stderr")

    def _call(self, method, params, timeout):
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            try:
                with self.stdin_lock:
                    self.process.stdin.write(json.dumps(payload) + "\n")
                    self.process.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise CommandError("miniMax session is not writable: %s" % exc)
            deadline = time.time() + timeout
            while time.time() < deadline:
                with self.buffer_lock:
                    for index, message in enumerate(self.buffer):
                        # A JSON-RPC response carries result or error and never
                        # method.  The bridge and the ACP server both number
                        # their requests from 1, so a server-initiated request
                        # can share this id; matching on id alone read it as
                        # the response and the turn ended with no content.
                        if message.get("id") == request_id and "method" not in message:
                            del self.buffer[index]
                            if "error" in message:
                                raise CommandError(
                                    "%s failed: %s"
                                    % (method, (message["error"] or {}).get("message", "unknown"))
                                )
                            return message.get("result")
                    self.buffer = [
                        m for m in self.buffer if m.get("id") != request_id or "method" in m
                    ]
                time.sleep(0.15)
            raise CommandError("%s timed out after %ss" % (method, timeout))

    def _collect_text(self, result, since=0):
        """Pull the assistant text out of a prompt result and its updates."""
        updates = self._collect_notifications(since)
        text = ""
        for block in (result or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                text += block["text"]
        return text or updates

    def _collect_notifications(self, since=0):
        """Take the text out of this turn's updates, and drop them.

        The pump appends every message the ACP server writes and _call only
        removes what matches its own request id, so the session/update
        notifications of a finished turn stayed in the buffer: the next turn
        answered with the previous turn's text as well, and the buffer grew
        for the life of the session.  They are consumed here, and taken
        whether or not the prompt result carried text of its own.

        Only the notifications that arrived after the prompt was sent count,
        and only the ones carrying agent text: an ACP session/prompt returns
        only stopReason, so the text is normally collected from the updates,
        and a thought chunk is private reasoning the owner must not read.
        """
        text = ""
        with self.buffer_lock:
            pending = list(self.buffer[since:])
            # The entries before `since` belong to a turn that has already
            # been answered, and the ones after it are consumed here, so the
            # whole prefix goes: the buffer never outlives the turn.
            del self.buffer[: since + len(pending)]
        for message in pending:
            if message.get("method") != "session/update":
                continue
            # The discriminator is spelled both ways and the update is not
            # always nested, so the strict form turned every deviation into an
            # empty reply instead of a partial answer.  Mirrors
            # extract_agent_text in scripts/grok-acp-runtime/leader-client.py.
            params = message.get("params")
            if not isinstance(params, dict):
                log("ACP update refused: code=invalid_type field=params")
                continue
            if "update" in params:
                update = params["update"]
                if not isinstance(update, dict):
                    log("ACP update refused: code=invalid_type field=update")
                    continue
            else:
                update = params
            kind = update.get("sessionUpdate")
            if kind is None:
                kind = update.get("session_update")
            if not isinstance(kind, str):
                log("ACP update refused: code=invalid_type field=sessionUpdate")
                continue
            if kind != "agent_message_chunk":
                continue
            content = update.get("content")
            if isinstance(content, str):
                text += content
            elif isinstance(content, dict):
                chunk = content.get("text")
                if content.get("type", "text") == "text" and isinstance(chunk, str):
                    text += chunk
            elif content is not None:
                raise ValueError("ACP agent-message content must be text or an object")
        return text

    def prompt(self, message):
        if self.session_id is None:
            self._start()
        with self.buffer_lock:
            since = len(self.buffer)
        result = self._call(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": message}],
            },
            timeout=self.timeout,
        )
        return self._collect_text(result, since)

    def close(self):
        process = self.process
        self.process = None
        self.session_id = None
        if process is None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=10)
        except Exception:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                pass


class Bridge:
    def __init__(self, args):
        self.args = args
        self.env = lark_env()
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.turns = TurnQueue(getattr(args, "allowed_sender", None))
        # message_id -> delivery state.  A plain dict keeps insertion order,
        # which is what the dedup window needs: ids are opaque strings, so
        # sorting them evicts by spelling rather than by age.
        self.rows = {}
        self.jobs = {}
        self.seen_lock = threading.Lock()
        self.active_ids = set()
        self.state_lock = threading.Lock()
        self.state_dir = Path(args.state_dir)
        if self.state_dir.is_symlink():
            raise ValueError("refusing a symlinked state directory: %s" % self.state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.signal_dir = self.state_dir / SIGNAL_DIRNAME
        self.signal_done_dir = self.signal_dir / "done"
        for path in (self.signal_dir, self.signal_done_dir):
            if path.is_symlink():
                raise ValueError("refusing a symlinked signal directory: %s" % path)
            # No parents:  the state directory above already exists, and a
            # permissive parent would put the signals somewhere other eyes
            # can read.
            path.mkdir(mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(str(path), 0o700)
        # Signal file name -> (attempts, next attempt).  The signal file holds
        # both, but a write of it can fail, and the count on the disk would
        # then stay where it was:  the signal would be tried again on every
        # pass, and the attempt budget would never run out.
        self.signal_retry = {}
        # The chat the owner last wrote in.  A signal that names no chat of its
        # own goes there, so the Operator does not have to know the chat id.
        self.last_chat_id = None
        self._load_state()
        self.stopping = threading.Event()

    @staticmethod
    def _parse_state(data):
        if not isinstance(data, dict):
            raise RuntimeError("Lark bridge state must be a JSON object")
        rows = data.get("messages")
        if rows is None:
            # The first release persisted a plain list of delivered ids.
            legacy = data.get("seen_message_ids") or []
            if not isinstance(legacy, list) or not all(isinstance(item, str) for item in legacy):
                raise RuntimeError("Lark bridge state seen_message_ids must be a list of strings")
            rows = {message_id: "ok" for message_id in legacy}
        if not isinstance(rows, dict) or not all(
            isinstance(key, str) and value in DELIVERY_STATES for key, value in rows.items()
        ):
            raise RuntimeError(
                "Lark bridge state messages must map message ids to %s" % (DELIVERY_STATES,)
            )
        return dict(rows)

    def _load_state(self):
        try:
            with open(self.state_file) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.rows = {}
            return
        except (OSError, ValueError) as exc:
            # A torn or unreadable state file is not "nothing was delivered".
            # Reading it as empty would replay every recent message on the
            # next start and double-post the replies.
            raise RuntimeError(
                "Lark bridge state %s is unreadable (%s); refusing to start and "
                "treat corrupt state as authoritative" % (self.state_file, exc)
            )
        self.rows = self._parse_state(data)
        last_chat = data.get("last_chat_id")
        if last_chat is not None and not isinstance(last_chat, str):
            raise RuntimeError("Lark bridge state last_chat_id must be a string")
        self.last_chat_id = last_chat
        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict):
            raise RuntimeError("invalid durable job inventory")
        self.jobs = {}
        for message_id, job in jobs.items():
            if not isinstance(job, dict) or job.get("stage") not in ("queued", "running", "reply"):
                raise RuntimeError("invalid durable job")
            if not all(
                isinstance(job.get(key), str) for key in ("chat_id", "content", "sender_id")
            ):
                raise RuntimeError("invalid durable job fields")
            if scan_secret(job["content"]) or (
                "reply" in job and (not isinstance(job["reply"], str) or scan_secret(job["reply"]))
            ):
                raise RuntimeError("secret-shaped durable job refused")
            if (
                type(job.get("attempt", 0)) is not int
                or not 0 <= job.get("attempt", 0) <= MAX_REPLY_ATTEMPTS
            ):
                raise RuntimeError("invalid durable attempt budget")
            if not isinstance(job.get("cwd"), str):
                raise RuntimeError("invalid durable working directory")
            self.jobs[message_id] = job
            if (
                self.rows.get(message_id) == "pending"
                and job["stage"] in ("queued", "reply")
                and (
                    job["sender_id"] in self.args.allowed_sender
                    or job["sender_id"] == OPERATOR_SENDER
                )
                and job.get("cwd") == self.args.cwd
            ):
                job["attempt"] = 0  # A new process gets a bounded reply-only retry budget.
                self.active_ids.add(message_id)
                self.turns.put(
                    (
                        message_id,
                        job["chat_id"],
                        job["content"],
                        job["sender_id"],
                        job.get("attempt", 0),
                    )
                )

    def _save_state(self):
        if not hasattr(self, "state_lock"):
            self.state_lock = threading.Lock()
        with self.state_lock:
            with self.seen_lock:
                rows = list(self.rows.items())
                jobs = {key: dict(job) for key, job in getattr(self, "jobs", {}).items()}
            # The rows are filtered against the snapshot and not self.rows read
            # again: between the two, another thread can record a delivery and
            # the persisted messages and jobs would disagree.
            rows_by_id = dict(rows)
            kept = [pair for pair in rows if pair[1] != "ok"]
            room = DEDUP_WINDOW - len(kept)
            if room > 0:
                kept.extend([pair for pair in rows if pair[1] == "ok"][-room:])
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=str(self.state_file.parent), delete=False
                ) as handle:
                    tmp = handle.name
                    json.dump(
                        {
                            "messages": dict(kept),
                            "last_chat_id": getattr(self, "last_chat_id", None),
                            "jobs": {
                                key: job
                                for key, job in jobs.items()
                                if rows_by_id.get(key) == "pending"
                            },
                        },
                        handle,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.state_file)
                # The rename itself is a directory entry: without this the
                # replace can still be lost on a crash and the old state comes
                # back, which is what replays a delivered message.
                dir_fd = os.open(str(self.state_file.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                return True
            except Exception as exc:
                log("could not persist bridge state: %s" % type(exc).__name__)
                return False
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        log("could not remove temporary bridge state")

    def _claim(self, message_id):
        """Claim a message id for a turn, or report why it must not run.

        A delivered or refused id is finished.  An unknown one is never
        replayed on our own initiative: its reply may already be in the chat,
        and a second send would post it twice.  A pending one is claimable,
        because its last reply provably never reached Lark -- running the turn
        again costs a model run, but it cannot double-post.
        """
        with self.seen_lock:
            if not hasattr(self, "active_ids"):
                self.active_ids = set()
            if message_id in self.active_ids:
                return False
            state = self.rows.get(message_id)
            if state == "pending" and getattr(self, "jobs", {}).get(message_id):
                return False
            if state == "pending" and (
                getattr(self, "jobs", {}).get(message_id, {}).get("stage")
                not in ("queued", "reply")
                or getattr(self, "jobs", {}).get(message_id, {}).get("attempt", 0)
                >= MAX_REPLY_ATTEMPTS
                or getattr(self, "jobs", {}).get(message_id, {}).get("cwd") != self.args.cwd
            ):
                return False
            if state == "unknown":
                log("not replaying %s: its delivery outcome is unknown" % message_id)
                return False
            if state in ("ok", "refused"):
                return False
            self.rows[message_id] = "pending"
            self.active_ids.add(message_id)
            return True

    def _record_delivery(self, message_id, state):
        with self.seen_lock:
            self.rows[message_id] = state

    def _session_for(self, chat_id):
        with self.sessions_lock:
            session = self.sessions.get(chat_id)
            if session is not None and session.process is not None:
                # The ACP child can die between turns.  Handing back a dead
                # session spends this turn on a broken pipe instead of a new
                # one, and keeps the chat pinned to it.
                if session.process.poll() is not None:
                    log("miniMax session for %s exited; starting a new one" % chat_id)
                    session.close()
                    session = None
            if session is None:
                session = MiniMaxSession(
                    self.args.mcode, self.args.cwd, self.args.timeout, self.env
                )
                self.sessions[chat_id] = session
            return session

    @staticmethod
    def _classify_reply_failure(detail):
        """Decide what a failed Lark write means for the delivery state.

        A timeout is the ambiguous case: lark-cli may have delivered the
        reply and lost the acknowledgement.  A 4xx is Lark refusing the
        message, which no retry will fix.  What provably never left the Mac
        -- a missing binary, a refused connection -- is safe to send again.
        """
        if "timed out" in detail:
            return "outcome_unknown"
        if re.search(r"\b429\b", detail):
            return "retryable"
        if re.search(r"\binvalid_grant\b", detail):
            # A token grant that was refused is a definite auth failure, with
            # or without a status in the text:  nothing was sent, and no
            # retry works until the credential is fixed.
            return "terminal"
        if re.search(r"\b4\d\d\b", detail):
            return "terminal"
        if "could not start" in detail:
            return "retryable"
        if re.search(
            r"(?i)(connection refused|network (is )?unreachable)",
            detail,
        ):
            return "retryable"
        return "outcome_unknown"

    def _reply(self, message_id, text):
        text = (text or "").strip()
        if not text:
            text = "(MM produced no reply.)"
        text = sentence_gap(text)
        if len(text) > MAX_REPLY_CHARS:
            text = text[:MAX_REPLY_CHARS].rstrip() + "\n\n(reply truncated)"
        if scan_secret(text):
            # Log the id and the code only; the text is the thing being kept
            # out of the chat, so it does not belong in the log either.
            log("reply refused for %s: code=secret field=text" % message_id)
            raise SecretRefusal("code=secret field=text")
        command = [
            self.args.lark_cli,
            "im",
            "+messages-reply",
            "--as",
            "bot",
            # --flag=value in one token: a message id or a reply body that
            # begins with "-" would otherwise be read as another option.
            "--message-id=" + message_id,
            "--text=" + text,
        ]
        try:
            run(command, timeout=REPLY_TIMEOUT, env=self.env)
        except CommandError as exc:
            raise DeliveryError(self._classify_reply_failure(str(exc)), str(exc))

    def _mirror(self, chat_id, session_id, question, answer, failed=False, sender_id=None):
        """Post the Lark exchange to Zulip, where the owner is watching.

        Lark is a second surface, not a second seat: the answer comes from
        the MiniMax session named in the post, so the owner can see which
        thread it was and can find the same session in Lark.  Without this
        the Lark conversation is invisible from the fleet side.
        """
        if not getattr(getattr(self, "args", None), "zulip_mirror", True):
            return
        topic = "%s %s" % (ZULIP_TOPIC_PREFIX, chat_id[-8:])
        kind = "failed turn" if failed else "answered"
        # The Operator's signal is not the owner's own message, and the post
        # says who sent it.
        author = "Operator" if sender_id == OPERATOR_SENDER else "Owner"
        body = sentence_gap(
            "Lark chat %s (%s)  |  MiniMax session %s  |  %s\n\n"
            "%s: %s\n\nMM: %s"
            % (chat_id, chat_id, session_id or "none", kind, author, question, answer),
            gap=ZULIP_SENTENCE_GAP,
        )
        env = dict(self.env)
        env["AGENT_SEAT"] = "MM"
        if isinstance(session_id, str) and session_id:
            # The MiniMax session id is a real session id, so the post carries
            # the same thread the Lark reply came from.
            env["AGENT_SESSION"] = session_id
        try:
            run(
                [AGENT_SYNC, "post", "--topic", topic, body],
                timeout=REPLY_TIMEOUT,
                env=env,
            )
            log("mirrored the Lark exchange to Zulip topic %s" % topic)
        except CommandError as exc:
            # A mirror that fails must not fail the reply: the owner has the
            # answer in Lark either way.
            log("could not mirror the Lark exchange to Zulip: %s" % exc)

    def _record_in_seat_inbox(
        self, chat_id, message_id, session_id, question, answer, sender_id=None
    ):
        """Put the exchange in the seat inbox, the queue a Zulip DM lands in.

        This is how the seat hears about Lark traffic at all.  The listener
        writes the seat inbox for a Zulip event, and nothing wrote it for
        Lark, so a Lark chat was invisible to the seat that answered it even
        though the answer was correct.  The listener's own primitives are
        used, under its own lock, so the two writers cannot interleave.
        """
        try:
            from agent_sync import live as L
        except ImportError as exc:
            log("could not record the Lark exchange in the seat inbox: %s" % exc)
            return
        try:
            paths = L.SeatPaths(self.args.agent_sync_root, self.args.seat)
            now = time.time()
            # The owner flag and the wake flags follow who sent it:  an
            # Operator signal is not the owner asking, so it must not wake the
            # seat as if it were.
            owner = sender_id != OPERATOR_SENDER
            author = "Jay" if owner else "Operator"
            row = {
                "channel": "lark",
                "classes": ["direct"],
                "content": "%s: %s\n\nMM: %s\n\n(MiniMax session %s)"
                % (author, question, answer, session_id or "unknown"),
                "delivered_to": None,
                "flags": ["mentioned"],
                "id": "lark:%s" % message_id,
                "is_bot": False,
                "kind": "message",
                "muse_bridge": False,
                "owner": owner,
                "owner_api": False,
                "recipients": [],
                "sender": "Jay Wedgeworth" if owner else sender_id,
                "sender_email": "",
                "sender_id": sender_id,
                "stream_id": 0,
                "time": time.strftime("%a, %b %-d, %-I:%M%p", time.localtime(now)),
                "topic": "%s %s" % (ZULIP_TOPIC_PREFIX, chat_id[-8:]),
                "ts": int(now),
                "type": "stream",
                "wake_owner": owner,
                "wake_worthy": owner,
            }
            with L.locked(paths.seat_inbox_meta):
                meta = L.read_json(paths.seat_inbox_meta)
                seq = int(meta.get("next_seq") or 1)
                L.append_jsonl(paths.seat_inbox, [dict(row, seq=seq)])
                meta["next_seq"] = seq + 1
                L.write_json(paths.seat_inbox_meta, meta)
            log("recorded the Lark exchange in the %s seat inbox" % self.args.seat)
        except Exception as exc:
            # The seat inbox is a convenience, not the reply path: a failure
            # here must never cost the answer.
            log("could not record the Lark exchange in the seat inbox: %s" % exc)

    def _post_signal(self, chat_id, text, signal_id):
        """Send the signal into the Lark chat and return the message id.

        The outcome is raised, not returned:  a caller that cannot tell a
        definite refusal from an uncertain one would retry a write that may
        already be on the issue, and the Operator's message would be in the
        chat twice.  The idempotency key is the signal's own id, so a bridge
        that dies after Lark accepted the message does not post it twice when
        it comes back.
        """
        command = [
            self.args.lark_cli,
            "im",
            "+messages-send",
            "--as",
            "bot",
            "--chat-id=" + chat_id,
            "--text=" + text,
            "--idempotency-key=" + _signal_key(signal_id),
        ]
        try:
            output = run(command, timeout=REPLY_TIMEOUT, env=self.env)
        except CommandError as exc:
            # The raw text of the error can carry a response body, a request
            # payload or an authorization header, and this detail is written
            # to the done directory, so only the name of the failure and the
            # allow-listed fields of it survive.
            raise DeliveryError(
                self._classify_reply_failure(str(exc)),
                safe_error_detail("command_error", str(exc)),
                retry_after=retry_after_from(str(exc)),
            ) from exc
        message_id = _message_id_from(output)
        if not message_id:
            # The message may be on the issue without an id to answer on, so
            # the outcome is uncertain rather than a failure to send.
            raise DeliveryError("outcome_unknown", "lark accepted the signal without a message id")
        return message_id

    def _read_signal(self, path):
        """The signal's own record with its steps, and why it was refused.

        The file is the done-set:  it records the steps that are already
        done, so a retry after a crash neither sends the message again nor
        queues the turn again.  The reason is None when the record was
        read, and otherwise names the one thing that made it unusable, so
        the filed outcome says why the signal was refused.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            log("could not read a signal file: %s" % type(exc).__name__)
            return None, "unreadable"
        try:
            record = json.loads(raw)
        except ValueError:
            return None, "invalid_json"
        if not isinstance(record, dict):
            return None, "not_an_object"
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, "empty_text"
        if scan_secret(text):
            # The text is the thing being kept out of the chat, so it must
            # not be named in the log either.
            log(f"signal refused: code={SECRET_IN_TEXT} field=text")
            return None, SECRET_IN_TEXT
        # The shape of every field read from an external file is checked
        # before any of it is used.  A malformed "queued" of "false" is a
        # truthy string, and a truthy "queued" would suppress the queueing
        # and report a success that never happened.
        queued = record.get("queued")
        if queued is not None and not isinstance(queued, bool):
            log("signal refused: code=invalid_field field=queued")
            return None, "invalid_field:queued"
        sent = record.get("sent")
        if sent is not None and (not isinstance(sent, str) or not sent):
            log("signal refused: code=invalid_field field=sent")
            return None, "invalid_field:sent"
        record["text"] = text.strip()
        signal_id = record.get("signal_id")
        if not isinstance(signal_id, str) or not signal_id:
            record["signal_id"] = path.stem
        return record, None

    def _handle_signal(self, path):
        """Deliver one Operator signal into the Lark chat and wake the seat for it.

        Exactly one outcome is written for every signal, in a finally block,
        so a signal never ends with no record at all.  A signal that cannot be
        delivered yet stays where the watcher will find it again, with the
        attempt count and the wait before the next try.
        """
        outcome = "unknown"
        detail = "no_outcome"
        record = None
        try:
            record, reason = self._read_signal(path)
            if record is None:
                outcome, detail = "refused", reason
                return
            attempts = record.get("attempts")
            attempts = attempts if isinstance(attempts, int) and attempts >= 0 else 0
            next_attempt = float(record.get("next_attempt") or 0)
            remembered = self.signal_retry.get(path.name)
            if remembered is not None:
                # A pending write that failed left an older count in the
                # file, so the count in memory is the true one, and the next
                # write of the record carries it.
                attempts = max(attempts, remembered[0])
                next_attempt = max(next_attempt, remembered[1])
                record["attempts"] = attempts
                record["next_attempt"] = next_attempt
            if time.time() < next_attempt:
                # The wait before the next try has not passed yet.  The
                # signal is not finished, so it stays pending and the
                # watcher finds it again when the wait has passed.
                outcome, detail = "pending", "retry_not_due"
                return
            chat_id = record.get("chat_id") or self.last_chat_id
            if not isinstance(chat_id, str) or not chat_id:
                outcome, detail = "refused", "no_chat"
                return
            message_id = record.get("sent")
            if not isinstance(message_id, str) or not message_id:
                if attempts >= MAX_SIGNAL_ATTEMPTS:
                    # The budget was spent on an earlier pass whose filing did
                    # not reach the disk.  Sending again would spend more.
                    outcome, detail = "unknown", "retries exhausted"
                    return
                try:
                    message_id = self._post_signal(chat_id, record["text"], record["signal_id"])
                except DeliveryError as exc:
                    if exc.outcome == "retryable":
                        attempts += 1
                        record["attempts"] = attempts
                        if attempts >= MAX_SIGNAL_ATTEMPTS:
                            # Remembered too, so a filing that fails does not
                            # put the signal back on a fresh budget.
                            self.signal_retry[path.name] = (attempts, next_attempt)
                            outcome, detail = "unknown", "retries exhausted: %s" % exc.detail
                            return
                        wait = min(SIGNAL_RETRY_BASE * (2 ** (attempts - 1)), 300)
                        if exc.retry_after:
                            # The server said how long to wait, and asking
                            # sooner only spends an attempt on another refusal.
                            wait = max(wait, exc.retry_after)
                        next_attempt = time.time() + wait
                        record["next_attempt"] = next_attempt
                        # Remembered before the record is written, so a write
                        # that fails cannot reset the count or the wait.
                        self.signal_retry[path.name] = (attempts, next_attempt)
                        outcome, detail = "pending", "retryable: %s" % exc.detail
                        return
                    if exc.outcome == "terminal":
                        outcome, detail = "refused", "terminal: %s" % exc.detail
                        return
                    outcome, detail = "unknown", "unknown: %s" % exc.detail
                    return
                record["sent"] = message_id
                if not self._write_signal(path, record):
                    # The message is in the chat but the marker is not on the
                    # disk, so the record cannot claim it:  a crash here
                    # would send the message a second time.
                    outcome, detail = "unknown", "signal_write_failed"
                    return
            # The shape of "queued" was checked when the record was read, so
            # this is exactly "the turn is not queued yet".
            if record.get("queued") is not True:
                if not self._queue_turn(message_id, chat_id, record["text"], OPERATOR_SENDER):
                    outcome, detail = "unknown", "state_unavailable"
                    return
                record["queued"] = True
                if not self._write_signal(path, record):
                    outcome, detail = "unknown", "signal_write_failed"
                    return
            outcome, detail = "ok", message_id
        except Exception as exc:
            outcome, detail = "unknown", "unexpected: %s" % type(exc).__name__
        finally:
            # Every result goes through the one recorder, so a pending
            # signal carries its outcome and its detail too.
            self._record_signal(path, outcome, detail, record)

    def _write_signal(self, path, record):
        """Write the signal's record back, atomically, so a crash cannot tear it.

        The flag says whether the write reached the disk:  a step that was
        not written is not done, and a signal that is recorded as sent or
        queued when it is not would be replayed after a crash.
        """
        try:
            _write_json_atomic(path, record)
            return True
        except OSError as exc:
            log("could not write a signal record: %s" % type(exc).__name__)
            return False

    def _record_signal(self, path, outcome, detail, record=None):
        """File one signal with its outcome, and keep the done directory small.

        A terminal signal moves to the done directory and carries the outcome
        with it;  a pending one stays where the watcher will find it again.
        The outcome is written atomically, so a crash leaves either the old
        record or the new one and never a torn one.
        """
        payload = dict(record or {})
        payload["outcome"] = outcome
        payload["detail"] = detail
        payload["at"] = time.time()
        if outcome == "pending":
            self._write_signal(path, payload)
            return
        target = self.signal_done_dir / path.name
        # The file is scrubbed before it is filed:  a refused signal may
        # still carry the text that was refused, and moving it into the done
        # directory first would persist that text under a filed name.
        try:
            _write_json_atomic(path, payload)
        except OSError as exc:
            log("could not record a signal outcome: %s" % type(exc).__name__)
            return
        try:
            os.replace(str(path), str(target))
        except OSError as exc:
            log("could not file a signal: %s" % type(exc).__name__)
            return
        # Filed, so the watcher will not see it again.  Until then the count
        # stays in memory, so a filing that failed is not a fresh budget.
        self.signal_retry.pop(path.name, None)
        self._prune_done()

    def _prune_done(self):
        """Keep the done directory to its newest records.

        A long-lived installation would otherwise keep every signal it ever
        handled, and the disk use would grow without bound.
        """
        try:
            entries = sorted(self.signal_done_dir.glob("*" + SIGNAL_SUFFIX))
        except OSError:
            return
        for path in entries[:-SIGNAL_DONE_WINDOW]:
            with contextlib.suppress(OSError):
                path.unlink()

    def _signal_worker_once(self):
        """One pass over the signal directory.

        Each file is handled on its own:  one file that raises must not leave
        the later files of the same pass undelivered.
        """
        try:
            paths = sorted(self.signal_dir.glob("*" + SIGNAL_SUFFIX))
        except OSError as exc:
            log("could not read the signal directory: %s" % type(exc).__name__)
            return
        # A count is kept only while its file is still waiting:  a file that
        # was taken away must not hand its count to a later file of that name.
        present = {path.name for path in paths}
        for name in [name for name in self.signal_retry if name not in present]:
            del self.signal_retry[name]
        for path in paths:
            if path.name.startswith("."):
                # A dot-prefixed file is a temporary one, not a signal:  the
                # atomic write leaves one behind when it is interrupted.
                continue
            try:
                self._handle_signal(path)
            except Exception as exc:
                # One signal must not take the watcher down:  the owner would
                # stop getting signals and nothing would say why.
                log("signal file %s failed: %s" % (path.name, type(exc).__name__))

    def _signal_worker(self):
        """Watch the signal directory and deliver what the Operator drops in it."""
        while not self.stopping.is_set():
            self._signal_worker_once()
            self.stopping.wait(1.0)

    def _handle(self, event):
        # Every field below is read off a frame the Lark socket delivered, and
        # the event loop has no try around this call: a content that is an
        # object rather than text raised out of here, and the finally in run()
        # took the whole bridge down with it.  Types are checked at the
        # boundary so a malformed frame is dropped instead.
        if not isinstance(event, dict):
            log("ignoring an event that is not an object")
            return
        message_id = event.get("message_id") or event.get("id")
        chat_id = event.get("chat_id")
        content = event.get("content") or ""
        sender_id = event.get("sender_id")
        if not isinstance(message_id, str) or not isinstance(chat_id, str):
            log("ignoring an event whose message id or chat id is not a string")
            return
        if not isinstance(content, str):
            log("ignoring message %s: content is not text" % (message_id,))
            return
        content = content.strip()
        if not message_id or not chat_id or not content:
            return
        if event.get("sender_type") == "bot":
            return
        # Fail closed.  An empty allowlist means no owner is pinned, and the
        # seat answers whoever sends it a message; main() refuses to start in
        # that state, and this check refuses even if it is reached anyway.
        if sender_id not in self.args.allowed_sender:
            log("ignoring message %s from unallowed sender %s" % (message_id, sender_id))
            return
        # The owner marks a message with the eyes emoji at the very start to
        # say "do not wake the seat for this one".  The check is on the first
        # character of the trimmed text and it runs before the message is
        # claimed, so a marked message never becomes a turn, never gets a
        # reply, and leaves no delivery row behind.
        if content.startswith(SILENT_MARKER):
            log(
                "ignoring message %s in %s: the silent marker is at the start"
                % (message_id, chat_id)
            )
            return
        if not self._mentioned(event):
            log("ignoring message %s in %s: bot was not mentioned" % (message_id, chat_id))
            return
        if scan_secret(content):
            # The id and the field only: the content is the thing being kept
            # out of the log, so it must not be named here either.
            log("incoming message refused for %s: code=secret field=content" % message_id)
            return
        # The pointer to the owner's chat moves only when the turn was
        # claimed:  a redelivered event from an older chat must not send the
        # next signal into the wrong chat.
        if self._queue_turn(message_id, chat_id, content, sender_id):
            self.last_chat_id = chat_id
            # The pointer is saved with the turn, not after it.  The turn's
            # own save happens inside the queue, so a crash between the two
            # left the previous chat in the file and the next signal that
            # named no chat of its own was sent to that previous chat.
            if not self._save_state():
                log("could not persist the owner's chat pointer")

    def _queue_turn(self, message_id, chat_id, content, sender_id):
        """Put one message on the turn queue and persist the job.

        The durable job is what keeps a restart from replaying the message, so
        a job that could not be persisted is not answered:  the turn would run
        with no receipt and the next start would answer it again.  The row is
        claimed first, because the saved state keeps a job only while its row
        says pending, so an unclaimed turn would be lost on the next start.
        """
        if not self._claim(message_id):
            return False
        if not hasattr(self, "jobs"):
            self.jobs = {}
        self.jobs[message_id] = {
            "stage": "queued",
            "chat_id": chat_id,
            "content": content,
            "sender_id": sender_id,
            "cwd": self.args.cwd,
            "attempt": 0,
        }
        if not self._save_state():
            with self.seen_lock:
                self.active_ids.discard(message_id)
            return False
        self.turns.put((message_id, chat_id, content, sender_id, 0))
        return True

    def _mentioned(self, event):
        """In a group chat the bot answers only when it is mentioned.

        In a one-to-one chat every message is for the bot, so mentioning is
        not required there.
        """
        if event.get("chat_type") != "group":
            return True
        bot_open_id = self.args.bot_open_id
        if not bot_open_id:
            return False
        mentions = event.get("mentions")
        # A mentions field that is not a list of objects is a frame this bridge
        # does not understand, and reading it as "not mentioned" is the safe
        # answer: the bot stays quiet rather than answering a group chat it was
        # not addressed in.
        if not isinstance(mentions, list):
            return False
        for mention in mentions:
            if isinstance(mention, dict) and mention.get("id") == bot_open_id:
                return True
        return False

    def _worker(self):
        while not self.stopping.is_set():
            try:
                item = self.turns.get(timeout=1)
            except queue.Empty:
                continue
            message_id, chat_id, content, sender_id, attempt = item
            log("message %s from %s in %s" % (message_id, sender_id, chat_id))
            # One try/finally around the whole turn: the failure path used to
            # `continue` past the task_done() and the state save, so a turn
            # that errored left the queue counter drifting and its dedup row
            # unwritten -- the next start would answer the same message again.
            retry = False
            try:
                retry = not self._turn(message_id, chat_id, content, sender_id, attempt)
                if retry:
                    getattr(self, "jobs", {}).get(message_id, {}).update(attempt=attempt + 1)
                    self.stopping.wait(min(2**attempt, 8))
                    self.turns.put((message_id, chat_id, content, sender_id, attempt + 1))
            except Exception as exc:
                # One message must not take the worker down with it: the loop
                # serves every later message, and an exception escaping here
                # ended the thread and silently stopped the whole surface.
                # Only the class reaches the log; the message may hold a
                # conversation.  The outcome stays unknown, so the turn is
                # never replayed into the chat on our own initiative.
                log("turn dispatch failed for %s: %s" % (message_id, type(exc).__name__))
                self._record_delivery(message_id, "unknown")
                retry = False
            finally:
                if not retry:
                    with self.seen_lock:
                        getattr(self, "active_ids", set()).discard(message_id)
                        if self.rows.get(message_id) != "pending":
                            getattr(self, "jobs", {}).pop(message_id, None)
                self.turns.task_done()
                self._save_state()

    def _turn(self, message_id, chat_id, content, sender_id, attempt):
        """Run the model once; delivery retries reuse a durable safe reply."""
        jobs = getattr(self, "jobs", {})
        job = jobs.get(message_id)
        if job and job.get("stage") == "reply":
            return self._send(message_id, job["reply"], attempt)
        if job:
            job["stage"] = "running"
            if not self._save_state():
                return True
        session = self._session_for(chat_id)
        session_id = session.session_id
        try:
            answer = session.prompt(content)
        except Exception as exc:
            log("turn failed for %s: %s" % (message_id, exc))
            note = "MM could not answer: %s" % exc
            self._mirror(chat_id, session_id, content, note, failed=True, sender_id=sender_id)
            done = self._send(message_id, note)
            with self.sessions_lock:
                broken = self.sessions.pop(chat_id, None)
            if broken is not None:
                broken.close()
            return done
        if scan_secret(answer or ""):
            self._record_delivery(message_id, "refused")
            return True
        if len(answer or "") > MAX_REPLY_CHARS:
            answer = answer[:MAX_REPLY_CHARS].rstrip() + "\n\n(reply truncated)"
        if job:
            job.update(stage="reply", reply=answer or "(MM produced no reply.)")
            if not self._save_state():
                return True
        # Name the thread in the chat itself, so the owner can tell which
        # MiniMax session answered even when he only has the Lark window.
        answer = "%s\n\n(MiniMax session %s)" % (answer, session_id or "unknown")
        self._mirror(chat_id, session_id, content, answer, sender_id=sender_id)
        # The seat inbox is the queue a wake lands in, so the exchange is
        # attributed to whoever sent it:  the owner's own message is the
        # owner's, and the Operator's signal is the Operator's, so a signal
        # cannot wake the seat as if the owner had asked.
        self._record_in_seat_inbox(
            chat_id,
            message_id,
            session_id,
            content,
            answer,
            sender_id=sender_id if sender_id == OPERATOR_SENDER else None,
        )
        return self._send(message_id, answer, attempt)

    def _send(self, message_id, text, attempt=0):
        """Send a reply and record what is now known about its delivery.

        True means the message is done with: delivered, definitively refused,
        or in a state we refuse to replay.  False means the reply provably
        never reached Lark and the attempt budget is not spent yet.
        """
        # Persist uncertainty before the irreversible write.  A crash after Lark accepts
        # but before the terminal save must never make a pending row replayable.
        self._record_delivery(message_id, "unknown")
        if not self._save_state():
            self._record_delivery(message_id, "pending")
            log("reply held for %s: state persistence unavailable" % message_id)
            return True
        try:
            self._reply(message_id, text)
        except SecretRefusal:
            # A refused reply will be refused again; retrying it would only
            # burn the seat.
            self._record_delivery(message_id, "refused")
            return True
        except DeliveryError as exc:
            # Only the message id and the normalised outcome: exc.detail is the
            # raw tail of lark-cli's stderr and can carry a third-party
            # response body, request fields or headers into the log.
            log("reply failed for %s: outcome=%s" % (message_id, exc.outcome))
            if exc.outcome == "terminal":
                self._record_delivery(message_id, "refused")
                return True
            if exc.outcome == "outcome_unknown":
                self._record_delivery(message_id, "unknown")
                return True
            # Provably unsent, so the reply is not lost: it stays pending and
            # goes back on the queue until the attempt budget runs out, after
            # which the pending row survives in the state file.
            self._record_delivery(message_id, "pending")
            getattr(self, "jobs", {}).get(message_id, {}).update(attempt=attempt + 1)
            if not self._save_state():
                return True
            return attempt + 1 >= MAX_REPLY_ATTEMPTS
        log("replied to %s" % message_id)
        self._record_delivery(message_id, "ok")
        return True

    def run(self):
        reader = LarkEventReader(self.args.lark_cli, self.env, self.args.event_log_dir)
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()
        # The Operator's signals arrive as files, not as Lark events, so they
        # need their own watcher.
        signal_worker = threading.Thread(target=self._signal_worker, daemon=True)
        signal_worker.start()

        def handle_signal(signum, frame):
            log("signal %s received; shutting down" % signum)
            self.stopping.set()
            # The main thread is parked in readline() on the consumer's stdout
            # and a handler that returns leaves that read blocked (PEP 475), so
            # the flag alone would go unnoticed until the next event arrived and
            # the finally block would never save state.  Terminating the child
            # closes the pipe, which ends the read.
            reader.stop()

        previous_sigterm = signal.getsignal(signal.SIGTERM)
        previous_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGTERM, handle_signal)
            signal.signal(signal.SIGINT, handle_signal)
            for event in reader.events():
                if self.stopping.is_set():
                    break
                self._handle(event)
        finally:
            self.stopping.set()
            try:
                try:
                    reader.stop()
                finally:
                    worker.join(timeout=min(self.args.timeout + 130, 300))
                    signal_worker.join(timeout=5)
                    with self.sessions_lock:
                        sessions = list(self.sessions.values())
                        self.sessions.clear()
                    for session in sessions:
                        try:
                            session.close()
                        except Exception as exc:
                            log("session cleanup failed: %s" % type(exc).__name__)
                    self._save_state()
                    log("bridge stopped")
            finally:
                try:
                    signal.signal(signal.SIGTERM, previous_sigterm)
                finally:
                    signal.signal(signal.SIGINT, previous_sigint)


def build_parser():
    """Build the argument parser.

    The flags are the deployed contract (the launchd plist passes
    --allow-sender), so every flag names its dest explicitly.  argparse would
    otherwise derive allow_sender from --allow-sender and silently disagree
    with the attribute the code reads.
    """
    parser = argparse.ArgumentParser(description="Bridge a Lark chat to a MiniMax Code seat.")
    parser.add_argument("--lark-cli", dest="lark_cli", default=DEFAULT_LARK_CLI)
    parser.add_argument("--mcode", dest="mcode", default=DEFAULT_MCODE)
    parser.add_argument(
        "--cwd", dest="cwd", default=DEFAULT_CWD, help="working directory for the seat"
    )
    parser.add_argument("--state-dir", dest="state_dir", default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--event-log-dir",
        dest="event_log_dir",
        default=None,
        help="where lark-cli writes event files",
    )
    parser.add_argument("--timeout", dest="timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--allow-sender",
        dest="allowed_sender",
        action="append",
        default=[],
        help="only answer this sender open_id; repeatable, and at least one is required",
    )
    parser.add_argument(
        "--bot-open-id",
        dest="bot_open_id",
        default=None,
        help="the bot's own open_id; in a group chat the bot answers only when mentioned",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="log messages, do not answer"
    )
    parser.add_argument(
        "--no-zulip-mirror",
        dest="zulip_mirror",
        action="store_false",
        help="do not mirror the Lark exchange into Zulip",
    )
    parser.add_argument(
        "--seat",
        dest="seat",
        default="MM",
        help="the fleet seat whose inbox records the Lark exchange",
    )
    parser.add_argument(
        "--agent-sync-root",
        dest="agent_sync_root",
        default=AGENT_SYNC_ROOT,
        help="the agent-sync state root that holds the seat inbox",
    )
    parser.add_argument(
        "--signal",
        dest="signal",
        default=None,
        help="deliver this text into the Lark chat through the running bridge, then exit",
    )
    parser.add_argument(
        "--signal-chat-id",
        dest="signal_chat_id",
        default=None,
        help="the chat to signal; the chat the owner last wrote in when it is not given",
    )
    return parser


def signal_out(args):
    """Hand a signal to the running bridge and exit.

    The bridge is the only process that holds the Lark bot identity, so the
    Operator leaves the message in the signal directory and the bridge sends
    it into the chat, wakes the seat for it, and puts the answer back in the
    same chat.  This process never talks to Lark itself.
    """
    text = (args.signal or "").strip()
    if not text:
        print("nothing to signal", file=sys.stderr)
        return 2
    # The scan runs before the payload is built, so a credential-shaped text
    # never reaches the disk.  The refusal names the code and the field
    # only:  the text is the thing being kept out of the chat, the disk and
    # the log, so it is not echoed.
    if scan_secret(text):
        print(f"signal refused: code={SECRET_IN_TEXT} field=text", file=sys.stderr)
        return 2
    state_dir = Path(args.state_dir)
    if state_dir.is_symlink():
        print("refusing a symlinked state directory: %s" % state_dir, file=sys.stderr)
        return 2
    # The same rules as the bridge's own start:  the state directory is
    # created private, and the signal directory refuses a symlink and is
    # created without parents, so the Operator's text cannot be carried out of
    # the private state location or land in a directory other eyes can read.
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    signal_dir = state_dir / SIGNAL_DIRNAME
    if signal_dir.is_symlink():
        print("refusing a symlinked signal directory: %s" % signal_dir, file=sys.stderr)
        return 2
    signal_dir.mkdir(mode=0o700, exist_ok=True)
    signal_id = "sig-%d-%s" % (int(time.time()), os.urandom(4).hex())
    payload = {"signal_id": signal_id, "text": text}
    if args.signal_chat_id:
        payload["chat_id"] = args.signal_chat_id
    target = signal_dir / ("%s%s" % (signal_id, SIGNAL_SUFFIX))
    tmp = signal_dir / (".tmp-%s" % signal_id)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.flush()
        # The bytes have to be on the disk before the name points at them, or
        # a crash can leave the watcher with a signal file that is half
        # written and refuse it for good.
        os.fsync(handle.fileno())
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(target))
    # The rename is a directory entry:  without this it can still be lost on
    # a crash, and the Operator's message would never be picked up.
    dir_fd = os.open(str(signal_dir), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    print("signal queued: %s" % signal_id)
    return 0


def preview_events(args):
    """Observe events without loading a ledger, starting a model or recording delivery."""
    reader = LarkEventReader(args.lark_cli, lark_env(), args.event_log_dir)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def stop(signum, frame):
        reader.stop()

    try:
        for sig in previous:
            signal.signal(sig, stop)
        for event in reader.events():
            if not isinstance(event, dict) or event.get("sender_id") not in args.allowed_sender:
                continue
            content = event.get("content")
            if isinstance(content, str):
                log(
                    "dry run: secret-shaped message withheld"
                    if scan_secret(content)
                    else "dry run: owner event observed; no model or delivery"
                )
    finally:
        try:
            reader.stop()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # The signal path never starts the bridge and never needs the seat's own
    # tools:  it only leaves a message for the running bridge.
    if args.signal is not None:
        return signal_out(args)

    for tool, path in (("lark-cli", args.lark_cli), ("mcode", args.mcode)):
        if not os.path.exists(path):
            parser.error("%s not found at %s" % (tool, path))

    # Without a pinned sender the seat answers any Lark user in the chat with
    # the owner's own credentials, so an unset allowlist is a refusal to
    # start, not a permissive default.
    if not args.allowed_sender:
        parser.error("--allow-sender is required: name the open_id that may drive the seat")

    if args.dry_run:
        return preview_events(args)
    bridge = Bridge(args)
    log("bridge starting (cwd=%s, timeout=%ss)" % (args.cwd, args.timeout))
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
