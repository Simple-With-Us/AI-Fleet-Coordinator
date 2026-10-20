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
BUSY_MARK = "(MM is still working on the previous message.)"
# The MiniMax Code ACP server has to start a runtime before it answers
# initialize.  On a loaded Mac that handshake is slow, and a tight timeout
# turns a working seat into "could not answer" (AFC-417).
ACP_HANDSHAKE_TIMEOUT = 300

# The owner's house rule: a visibly wider gap between sentences, after . ! and ?
# only, never after a colon, semicolon, comma or dash.  In a Markdown chat pane
# the gap is a real U+00A0 plus a normal space; two ASCII spaces collapse and the
# &nbsp; entity renders as six literal characters.
SENTENCE_GAP = "\u00a0 "
_SENTENCE_END = re.compile(r"([A-Za-z]+)([.!?])( +)(?=[\"'(\[A-Z])")
_ABBREVIATIONS = ("mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "no")


def _widen_sentence(match):
    """Replace the spaces after a sentence end with the wide gap."""
    if match.group(1).lower() in _ABBREVIATIONS:
        return match.group(0)
    return match.group(1) + match.group(2) + SENTENCE_GAP


def sentence_gap(text):
    """Widen the gap after a sentence end, leaving code alone.

    The seat answers in plain prose, and prose carries one space between
    sentences.  Lark renders that as a single space, so every reply the bridge
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
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
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
        )
        self.next_id = 1
        self.buffer = []
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
            with self.buffer_lock:
                self.buffer.append(message)

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
                        m
                        for m in self.buffer
                        if m.get("id") != request_id or "method" in m
                    ]
                time.sleep(0.15)
            raise CommandError("%s timed out after %ss" % (method, timeout))

    def _collect_text(self, result):
        """Pull the assistant text out of a prompt result and its updates."""
        updates = self._collect_notifications()
        text = ""
        for block in (result or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                text += block["text"]
        return text or updates

    def _collect_notifications(self):
        """Take the text out of this turn's updates, and drop them.

        The pump appends every message the ACP server writes and _call only
        removes what matches its own request id, so the session/update
        notifications of a finished turn stayed in the buffer: the next turn
        answered with the previous turn's text as well, and the buffer grew
        for the life of the session.  They are consumed here, and taken
        whether or not the prompt result carried text of its own.
        """
        text = ""
        with self.buffer_lock:
            pending = list(self.buffer)
            self.buffer = [m for m in pending if m.get("method") != "session/update"]
        for message in pending:
            update = (message.get("params") or {}).get("update") or {}
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                text += content["text"]
        return text

    def prompt(self, message):
        if self.session_id is None:
            self._start()
        result = self._call(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": message}],
            },
            timeout=self.timeout,
        )
        return self._collect_text(result)

    def close(self):
        process = self.process
        self.process = None
        self.session_id = None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


class Bridge:
    def __init__(self, args):
        self.args = args
        self.env = lark_env()
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.turns = queue.Queue()
        self.seen = set()
        self.seen_lock = threading.Lock()
        self.state_dir = Path(args.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self._load_state()
        self.stopping = threading.Event()

    def _load_state(self):
        try:
            with open(self.state_file) as handle:
                data = json.load(handle)
            self.seen = set(data.get("seen_message_ids") or [])
        except Exception:
            self.seen = set()

    def _save_state(self):
        with self.seen_lock:
            ids = sorted(self.seen)[-2000:]
        try:
            tmp = str(self.state_file) + ".tmp"
            with open(tmp, "w") as handle:
                json.dump({"seen_message_ids": ids}, handle)
            os.replace(tmp, self.state_file)
        except Exception as exc:
            log("could not write state: %s" % exc)

    def _mark_seen(self, message_id):
        with self.seen_lock:
            if message_id in self.seen:
                return False
            self.seen.add(message_id)
            return True

    def _session_for(self, chat_id):
        with self.sessions_lock:
            session = self.sessions.get(chat_id)
            if session is None:
                session = MiniMaxSession(
                    self.args.mcode, self.args.cwd, self.args.timeout, self.env
                )
                self.sessions[chat_id] = session
            return session

    def _reply(self, message_id, text):
        text = (text or "").strip()
        if not text:
            text = "(MM produced no reply.)"
        text = sentence_gap(text)
        if len(text) > MAX_REPLY_CHARS:
            text = text[:MAX_REPLY_CHARS].rstrip() + "\n\n(reply truncated)"
        command = [
            self.args.lark_cli,
            "im",
            "+messages-reply",
            "--as",
            "bot",
            "--message-id",
            message_id,
            "--text",
            text,
        ]
        run(command, timeout=120, env=self.env)

    def _handle(self, event):
        message_id = event.get("message_id") or event.get("id")
        chat_id = event.get("chat_id")
        content = (event.get("content") or "").strip()
        sender_id = event.get("sender_id")
        if not message_id or not chat_id or not content:
            return
        if event.get("sender_type") == "bot":
            return
        if self.args.allowed_sender and sender_id not in self.args.allowed_sender:
            log("ignoring message %s from unallowed sender %s" % (message_id, sender_id))
            return
        if not self._mentioned(event):
            log("ignoring message %s in %s: bot was not mentioned" % (message_id, chat_id))
            return
        if not self._mark_seen(message_id):
            return
        self.turns.put((message_id, chat_id, content, sender_id))

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
        for mention in event.get("mentions") or []:
            if mention.get("id") == bot_open_id:
                return True
        return False

    def _worker(self):
        while not self.stopping.is_set():
            try:
                item = self.turns.get(timeout=1)
            except queue.Empty:
                continue
            message_id, chat_id, content, sender_id = item
            log("message %s from %s in %s" % (message_id, sender_id, chat_id))
            session = self._session_for(chat_id)
            try:
                answer = session.prompt(content)
            except Exception as exc:
                log("turn failed for %s: %s" % (message_id, exc))
                try:
                    self._reply(message_id, "MM could not answer: %s" % exc)
                except Exception as reply_exc:
                    log("reply failed for %s: %s" % (message_id, reply_exc))
                with self.sessions_lock:
                    broken = self.sessions.pop(chat_id, None)
                if broken is not None:
                    broken.close()
                continue
            try:
                self._reply(message_id, answer)
                log("replied to %s" % message_id)
            except Exception as exc:
                log("reply failed for %s: %s" % (message_id, exc))
            finally:
                self.turns.task_done()
                self._save_state()

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
        help="only answer this sender open_id; repeatable (default: answer anyone)",
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

    bridge = Bridge(args)
    if args.dry_run:
        original = bridge._reply

        def no_reply(message_id, text):
            log("dry run: would reply to %s: %s" % (message_id, text[:200]))

        bridge._reply = no_reply
    log("bridge starting (cwd=%s, timeout=%ss)" % (args.cwd, args.timeout))
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
