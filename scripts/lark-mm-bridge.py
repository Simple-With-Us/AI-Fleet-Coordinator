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
context, and each keeps its own conversation.  Turns inside one chat are
serialized, so a second message waits for the first answer instead of racing
it.  In a group chat the bot answers only when it is mentioned, so it does not
talk over the humans; in a one-to-one chat every message is for the bot.

Runs on the Mac's /usr/bin/python3 (3.9): stdlib only, no 3.10+ syntax.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

LARK_EVENT_KEY = "im.message.receive_v1"
DEFAULT_LARK_CLI = os.path.expanduser("~/.local/npm-global/bin/lark-cli")
DEFAULT_MCODE = os.path.expanduser("~/.local/bin/mcode")
DEFAULT_STATE_DIR = os.path.expanduser("~/.minimax/lark-mm-bridge")
DEFAULT_CWD = os.path.expanduser("~/apps")
DEFAULT_TIMEOUT = 600
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
SECRET_PATTERN = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xoxb-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|bearer[ ]+[A-Za-z0-9._~+/=-]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
# The MiniMax Code ACP server has to start a runtime before it answers
# initialize.  On a loaded Mac that handshake is slow, and a tight timeout
# turns a working seat into "could not answer" (AFC-417).
ACP_HANDSHAKE_TIMEOUT = 300

# The owner's house rule: a visibly wider gap between sentences, after . ! and ?
# only, never after a colon, semicolon, comma or dash.  Lark renders a plain
# text message literally, so two ASCII spaces already show as a wide gap there,
# and the owner asked for exactly that on Oct 19, 2026: "No nbsp here please
# just 2 spaces".  A real U+00A0 is the mechanism for Markdown panes that
# collapse spaces (the MiniMax Code pane, GitHub, Zulip), not for Lark.
SENTENCE_GAP = "  "
_SENTENCE_END = re.compile(r"([A-Za-z]+)([.!?])( +)(?=[\"'(\[A-Z])")
_ABBREVIATIONS = ("mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "no")


def _widen_sentence(match):
    """Normalise the spaces after a sentence end to the wide gap."""
    if match.group(1).lower() in _ABBREVIATIONS:
        return match.group(0)
    return match.group(1) + match.group(2) + SENTENCE_GAP


def sentence_gap(text):
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
                piece = _SENTENCE_END.sub(_widen_sentence, piece)
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

    def __init__(self, outcome, detail):
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


# Durable per-message delivery states.  "pending" means claimed but not yet
# delivered; "ok" is a delivered reply; "refused" is a definite refusal that
# will not improve; "unknown" is a write whose outcome we cannot establish.
DELIVERY_STATES = ("pending", "ok", "refused", "unknown")


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
        detail = (completed.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "exit %s" % completed.returncode
        raise CommandError("%s failed: %s" % (command[0], tail))
    return completed.stdout


def lark_env():
    """Put the lark-cli install prefix on PATH so its own helpers resolve."""
    env = dict(os.environ)
    prefix = os.path.expanduser("~/.local/npm-global/bin")
    if os.path.isdir(prefix):
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


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
            self._handle_message(message)

    def _handle_message(self, message):
        """Route one message from the ACP server.

        A message with a method and an id is a request the agent sends to the
        client.  A message with neither is a notification, and a message with
        an id and no method is the response to one of our calls.
        """
        if "method" in message and "id" in message:
            # Buffering it instead of answering leaves the turn waiting for a
            # reply that never comes until session/prompt times out.
            self._answer_request(message)
            return
        with self.buffer_lock:
            self.buffer.append(message)

    def _answer_request(self, message):
        """Answer a request the agent sends to the client.

        Nobody is watching Lark to approve a tool call, so a request that goes
        unanswered stalls the turn until the prompt timeout and the chat gets
        no answer at all.  The owner has set full access for this seat, so a
        permission request is answered with its allow option, and the
        least-privileged one of those: allow once before allow always.
        """
        method = message.get("method")
        request_id = message.get("id")
        if method == "session/request_permission":
            options = (message.get("params") or {}).get("options") or []
            allow_once = None
            allow_always = None
            for option in options:
                kind = (option.get("kind") or "").lower()
                if kind == "allow_once":
                    allow_once = option.get("optionId")
                elif kind == "allow_always":
                    allow_always = option.get("optionId")
                elif kind.startswith("allow") and allow_once is None:
                    allow_once = option.get("optionId")
            allow = allow_once or allow_always
            if allow:
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "selected", "optionId": allow}},
                }
            else:
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
                log("acp: %s" % line)

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
            params = message.get("params") or {}
            update = params.get("update") if isinstance(params.get("update"), dict) else params
            kind = update.get("sessionUpdate") or update.get("session_update") or ""
            if kind != "agent_message_chunk":
                continue
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                text += content["text"]
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
        self.turns = queue.Queue()
        # message_id -> delivery state.  A plain dict keeps insertion order,
        # which is what the dedup window needs: ids are opaque strings, so
        # sorting them evicts by spelling rather than by age.
        self.rows = {}
        self.seen_lock = threading.Lock()
        self.state_dir = Path(args.state_dir)
        if self.state_dir.is_symlink():
            raise ValueError("refusing a symlinked state directory: %s" % self.state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
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

    def _save_state(self):
        with self.seen_lock:
            rows = list(self.rows.items())
        # Unresolved rows are kept whatever their age: dropping a pending or
        # unknown row is what loses a reply or replays one.  Only delivered
        # ids age out, and the newest ones are the last to go.
        kept = [pair for pair in rows if pair[1] != "ok"]
        room = DEDUP_WINDOW - len(kept)
        if room > 0:
            kept.extend([pair for pair in rows if pair[1] == "ok"][-room:])
        try:
            tmp = str(self.state_file) + ".tmp"
            with open(tmp, "w") as handle:
                json.dump({"messages": dict(kept)}, handle)
            os.replace(tmp, self.state_file)
        except Exception as exc:
            log("could not write state: %s" % exc)

    def _claim(self, message_id):
        """Claim a message id for a turn, or report why it must not run.

        A delivered or refused id is finished.  An unknown one is never
        replayed on our own initiative: its reply may already be in the chat,
        and a second send would post it twice.  A pending one is claimable,
        because its last reply provably never reached Lark -- running the turn
        again costs a model run, but it cannot double-post.
        """
        with self.seen_lock:
            state = self.rows.get(message_id)
            if state == "unknown":
                log("not replaying %s: its delivery outcome is unknown" % message_id)
                return False
            if state in ("ok", "refused"):
                return False
            self.rows[message_id] = "pending"
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
        if re.search(r"\b4\d\d\b", detail):
            return "terminal"
        if "could not start" in detail:
            return "retryable"
        if re.search(
            r"(?i)(connection (refused|reset|aborted)|broken pipe|"
            r"network (is )?(unreachable|error)|temporarily unavailable)",
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
        if SECRET_PATTERN.search(text):
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
            run(command, timeout=120, env=self.env)
        except CommandError as exc:
            raise DeliveryError(self._classify_reply_failure(str(exc)), str(exc))

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
        if not self._mentioned(event):
            log("ignoring message %s in %s: bot was not mentioned" % (message_id, chat_id))
            return
        if not self._claim(message_id):
            return
        self.turns.put((message_id, chat_id, content, sender_id, 0))

    def _mentioned(self, event):
        """In a group chat the bot answers only when it is mentioned.

        In a one-to-one chat every message is for the bot, so mentioning is
        not required there.
        """
        if event.get("chat_type") != "group":
            return True
        bot_open_id = self.args.bot_open_id
        if not bot_open_id:
            return True
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
            try:
                if not self._turn(message_id, chat_id, content, attempt):
                    self.turns.put((message_id, chat_id, content, sender_id, attempt + 1))
            finally:
                self.turns.task_done()
                self._save_state()

    def _turn(self, message_id, chat_id, content, attempt):
        """Run one turn and deliver the reply.  False means run it again."""
        session = self._session_for(chat_id)
        try:
            answer = session.prompt(content)
        except Exception as exc:
            log("turn failed for %s: %s" % (message_id, exc))
            done = self._send(message_id, "MM could not answer: %s" % exc)
            with self.sessions_lock:
                broken = self.sessions.pop(chat_id, None)
            if broken is not None:
                broken.close()
            return done
        return self._send(message_id, answer, attempt)

    def _send(self, message_id, text, attempt=0):
        """Send a reply and record what is now known about its delivery.

        True means the message is done with: delivered, definitively refused,
        or in a state we refuse to replay.  False means the reply provably
        never reached Lark and the attempt budget is not spent yet.
        """
        try:
            self._reply(message_id, text)
        except SecretRefusal:
            # A refused reply will be refused again; retrying it would only
            # burn the seat.
            self._record_delivery(message_id, "refused")
            return True
        except DeliveryError as exc:
            log("reply failed for %s: outcome=%s detail=%s" % (message_id, exc.outcome, exc.detail))
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
            return attempt + 1 >= MAX_REPLY_ATTEMPTS
        log("replied to %s" % message_id)
        self._record_delivery(message_id, "ok")
        return True

    def run(self):
        reader = LarkEventReader(self.args.lark_cli, self.env, self.args.event_log_dir)
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()

        def handle_signal(signum, frame):
            log("signal %s received; shutting down" % signum)
            self.stopping.set()
            # The main thread is parked in readline() on the consumer's stdout
            # and a handler that returns leaves that read blocked (PEP 475), so
            # the flag alone would go unnoticed until the next event arrived and
            # the finally block would never save state.  Terminating the child
            # closes the pipe, which ends the read.
            reader.stop()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        try:
            for event in reader.events():
                if self.stopping.is_set():
                    break
                self._handle(event)
        finally:
            reader.stop()
            with self.sessions_lock:
                sessions = list(self.sessions.values())
                self.sessions.clear()
            for session in sessions:
                session.close()
            self._save_state()
            log("bridge stopped")


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
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    for tool, path in (("lark-cli", args.lark_cli), ("mcode", args.mcode)):
        if not os.path.exists(path):
            parser.error("%s not found at %s" % (tool, path))

    # Without a pinned sender the seat answers any Lark user in the chat with
    # the owner's own credentials, so an unset allowlist is a refusal to
    # start, not a permissive default.
    if not args.allowed_sender:
        parser.error("--allow-sender is required: name the open_id that may drive the seat")

    bridge = Bridge(args)
    if args.dry_run:
        original = bridge._reply

        def no_reply(message_id, text):
            # The reply text is still the seat's output, so the same scan the
            # send path uses applies before it reaches the log.
            if SECRET_PATTERN.search(text or ""):
                log("dry run: would refuse reply to %s: code=secret field=text" % message_id)
                return
            log("dry run: would reply to %s: %s" % (message_id, text[:200]))

        bridge._reply = no_reply
    log("bridge starting (cwd=%s, timeout=%ss)" % (args.cwd, args.timeout))
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
