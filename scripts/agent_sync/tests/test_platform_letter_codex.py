"""The Codex adapter of `platform-letter` (AFC-564): Operator session letters and "XX: Title" names for the Codex CLI and
Codex Desktop.  Codex's own store is a temporary SQLite file shaped like `state_<n>.sqlite`; the app-server is a fake
(a thread that speaks the JSON-RPC protocol over pipes, in both framings) or a stand-in that updates that store; the
registry is a temporary one in the double (pair) letter space, which main's registry has.  No test depends on which pair
the registry hands out first, only on its being a pair that the Operator, the note and the thread's name agree on."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL
from agent_sync import platform_letter_codex as C

PAIR = r"^[A-Z]{2}$"
HAS_PARENT = PL._accepts(OR.register_session, "parent_session_id")

THREADS_DDL = (
    "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'vscode', title TEXT NOT NULL DEFAULT '',"
    " first_user_message TEXT NOT NULL DEFAULT '', agent_nickname TEXT, agent_path TEXT, thread_source TEXT,"
    " name TEXT, originator TEXT, archived INTEGER NOT NULL DEFAULT 0)"
)
EDGES_DDL = (
    "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT NOT NULL PRIMARY KEY,"
    " status TEXT NOT NULL)"
)
SUB_SOURCE = '{"subagent":{"thread_spawn":{"parent_thread_id":"%s","depth":1}}}'


def secret_word() -> str:
    """A GitHub-shaped token built at run time, so the source holds no key-looking literal."""
    return "ghp_" + "A1b2C3d4" * 5


def tid(n: int) -> str:
    return "01a00000-0000-7000-8000-%012d" % n


class Base(unittest.TestCase):
    """A Mac-shaped home: a Codex home with a store, and the Operator's registry under the state folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.codex = os.path.join(self.home, ".codex")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.codex)
        self.store = os.path.join(self.codex, "state_5.sqlite")
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(THREADS_DDL)
            conn.execute(EDGES_DDL)
        conn.close()
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "CODEX_HOME": self.codex, "AGENT_SYNC_STATE_DIR": self.state}
        env.update(extra)
        return env

    def exe(self, *parts: str) -> str:
        """A program file under the temporary folder, executable."""
        path = os.path.join(self.tmp.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    # ---- the Codex store -------------------------------------------------------------------

    def add(self, n: int, **cols: object) -> str:
        cols.setdefault("originator", "Codex Desktop")
        cols.setdefault("thread_source", "user")
        sid = tid(n)
        names = ", ".join(["id", *cols])
        marks = ", ".join("?" for _ in range(len(cols) + 1))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("INSERT INTO threads (%s) VALUES (%s)" % (names, marks), [sid, *cols.values()])
        conn.close()
        return sid

    def add_child(self, n: int, parent: str, *, path: str = "/root/pong", edge: bool = True, **cols: object) -> str:
        cols.setdefault("thread_source", "subagent")
        cols.setdefault("source", SUB_SOURCE % parent)
        cols.setdefault("agent_nickname", "Harvey")
        sid = self.add(n, agent_path=path, **cols)
        if edge:
            conn = sqlite3.connect(self.store)
            with conn:
                conn.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')", (parent, sid))
            conn.close()
        return sid

    def stored_name(self, sid: str) -> str | None:
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute("SELECT name FROM threads WHERE id = ?", (sid,)).fetchone()[0]
        finally:
            conn.close()

    def set_name(self, sid: str, name: str | None) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE threads SET name = ? WHERE id = ?", (name, sid))
        conn.close()

    # ---- the registry ----------------------------------------------------------------------

    def lookup(self, sid: str, location: str = "mac") -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("codex", location, PL.SCOPE, sid)
        finally:
            reg.close_db()

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CODEX", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def has_note(self, sid: str) -> bool:
        return os.path.exists(os.path.join(self.state, "CODEX", "letters", sid + ".json"))

    def registered(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT count(*) FROM sessions WHERE platform = 'codex'").fetchone()[0]
        finally:
            conn.close()


# --------------------------------------------------------------------------------------------
# Fakes for the app-server
# --------------------------------------------------------------------------------------------


class FakeApp:
    """Stands in for an `Rpc` conversation: records each call and, for a rename, does what the app-server does, which
    is to write the name into the store."""

    def __init__(self, test: Base, log: list, fail: Exception | None = None):
        self.test, self.log, self.rpc_error = test, log, fail

    def __enter__(self) -> FakeApp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, method: str, params: dict) -> dict:
        self.log.append((method, dict(params)))
        if self.rpc_error is not None:
            raise self.rpc_error
        if method == "thread/name/set":
            self.test.set_name(params["threadId"], params["name"])
        return {}


class HookBase(Base):
    """Runs hook events in process on the Mac side, with the app-server faked and every rename kept in `renames`."""

    def setUp(self) -> None:
        super().setUp()
        self.renames: list = []
        self.rpc_error: Exception | None = None
        patcher = mock.patch.object(C, "open_rpc", side_effect=self.open_rpc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_rpc(self, env, deadline, *, daemon, popen=None) -> FakeApp:
        return FakeApp(self, self.renames, self.rpc_error)

    def run_hook(
        self,
        payload: dict,
        *,
        env: dict | None = None,
        platform: str = "darwin",
        transport=None,
        now: float | None = None,
        runner=None,
        **flags: str,
    ) -> dict | None:
        out = io.StringIO()
        extra = {"runner": runner} if runner is not None else {}
        code = PL.hook(
            C.SPEC,
            env if env is not None else self.env(),
            io.StringIO(json.dumps(payload)),
            out,
            platform=platform,
            transport=transport,
            now=time.time() if now is None else now,
            **extra,
            **flags,
        )
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def start(self, sid: str, source: str = "startup", **kw) -> dict | None:
        return self.run_hook({"hook_event_name": "SessionStart", "session_id": sid, "source": source}, **kw)

    def prompt(self, sid: str, text: str, **kw) -> dict | None:
        return self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}, **kw)

    def sub_start(self, parent: str, child: str, **kw) -> dict | None:
        event = {"hook_event_name": "SubagentStart", "session_id": parent, "agent_id": child, "agent_type": "default"}
        return self.run_hook(event, **kw)

    def sub_stop(self, parent: str, child: str, message: str = "", **kw) -> dict | None:
        """A SubagentStop, which Codex fires at the end of each turn of the sub-agent (`message` is its last words)."""
        event = {"hook_event_name": "SubagentStop", "session_id": parent, "agent_id": child, "agent_type": "default",
                 "stop_hook_active": False}
        if message:
            event["last_assistant_message"] = message
        return self.run_hook(event, **kw)


class FakeServer:
    """A Codex app-server in a thread, behind two pipes.  It is also the `Popen` the client gets: `stdin` and `stdout`
    are the client's ends, `wait` and `kill` are what `Rpc.close` calls.  `framing` is "lines" (`codex app-server`) or
    "ws" (`codex app-server proxy`, a WebSocket on stdio, with masked client frames)."""

    def __init__(self, framing: str, handler, *, bad_accept: bool = False):
        self.framing, self.handler, self.bad_accept = framing, handler, bad_accept
        self.requests: list[tuple[str, dict]] = []
        self.notes: list[str] = []
        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.stdin = os.fdopen(c2s_w, "wb")
        self.stdout = os.fdopen(s2c_r, "rb", buffering=0)
        self._rfd, self._wfd, self._buf = c2s_r, s2c_w, b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def wait(self, timeout: float | None = None) -> int:
        self.thread.join(timeout)
        return 0

    def kill(self) -> None:
        return None

    # ---- bytes -----------------------------------------------------------------------------

    def _need(self, count: int) -> None:
        while len(self._buf) < count:
            chunk = os.read(self._rfd, 65536)
            if not chunk:
                raise EOFError
            self._buf += chunk

    def _take(self, count: int) -> bytes:
        self._need(count)
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _read_message(self) -> bytes:
        if self.framing == "lines":
            while b"\n" not in self._buf:
                self._need(len(self._buf) + 1)
            line, _, self._buf = self._buf.partition(b"\n")
            return line
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4)
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(self._take(size)))
            if first & 0x0F == 8:
                raise EOFError
            if first & 0x0F == 1:
                return body

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        if self.framing == "ws":
            head = bytes([0x81])
            if len(data) < 126:
                head += bytes([len(data)])
            elif len(data) < 65536:
                head += bytes([126]) + struct.pack(">H", len(data))
            else:
                head += bytes([127]) + struct.pack(">Q", len(data))
            data = head + data
        else:
            data += b"\n"
        os.write(self._wfd, data)

    def reply(self, msg_id: int, result: dict) -> None:
        self.send({"id": msg_id, "result": result})

    def fail(self, msg_id: int, code: int = -32600) -> None:
        self.send({"id": msg_id, "error": {"code": code, "message": "no"}})

    # ---- the conversation ------------------------------------------------------------------

    def _handshake(self) -> bool:
        while b"\r\n\r\n" not in self._buf:
            self._need(len(self._buf) + 1)
        head, _, self._buf = self._buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        key = next(v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k.lower() == "sec-websocket-key")
        accept = base64.b64encode(hashlib.sha1(key.encode() + C.WS_GUID).digest()).decode()
        if self.bad_accept:
            accept = "x" + accept
        reply = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        os.write(self._wfd, (reply + "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        return not self.bad_accept

    def _serve(self) -> None:
        try:
            if self.framing == "ws" and not self._handshake():
                return
            while True:
                msg = json.loads(self._read_message())
                if "id" not in msg:
                    self.notes.append(msg.get("method"))
                    continue
                self.requests.append((msg["method"], msg.get("params")))
                self.handler(self, msg)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            for fd in (self._wfd, self._rfd):
                try:
                    os.close(fd)
                except OSError:
                    pass


def standard(server: FakeServer, msg: dict) -> None:
    if msg["method"] in ("initialize", "thread/name/set"):
        server.reply(msg["id"], {"userAgent": "fake"} if msg["method"] == "initialize" else {})
    else:
        server.fail(msg["id"], -32601)


def deadline(seconds: float = 3.0) -> float:
    return time.monotonic() + seconds


# --------------------------------------------------------------------------------------------
# Codex's store
# --------------------------------------------------------------------------------------------


class StoreTests(Base):
    def test_the_newest_store_is_the_one_read(self) -> None:
        for name in ("state_4.sqlite", "state_12.sqlite", "state_x.sqlite", "logs_2.sqlite", "state_5.sqlite-wal"):
            Path(self.codex, name).write_bytes(b"")
        self.assertEqual(os.path.basename(C.store_file(self.env())), "state_12.sqlite")

    def test_no_codex_home_is_no_store(self) -> None:
        self.assertEqual(C.store_file({"CODEX_HOME": os.path.join(self.tmp.name, "nowhere")}), "")
        self.assertEqual(C.codex_home({"HOME": "/h"}), "/h/.codex")
        self.assertEqual(C.codex_home({"HOME": "/h", "CODEX_HOME": "/elsewhere"}), "/elsewhere")

    def test_a_row_is_read_whole(self) -> None:
        sid = self.add(1, source="cli", originator="codex-tui", title="Fix it", first_user_message="fix the login",
                       name="  AA:  Fix   Login\n")
        row = C.thread_row(self.env(), sid)
        self.assertEqual((row.id, row.source, row.originator, row.thread_source), (sid, "cli", "codex-tui", "user"))
        self.assertEqual((row.title, row.first_message), ("Fix it", "fix the login"))
        self.assertEqual(row.name, "AA: Fix Login")  # one line
        self.assertEqual(row.parent, "")

    def test_a_missing_row_or_store_is_none(self) -> None:
        self.assertIsNone(C.thread_row(self.env(), tid(99)))
        self.assertIsNone(C.thread_row(self.env(), ""))
        self.assertIsNone(C.thread_row({"CODEX_HOME": os.path.join(self.tmp.name, "nowhere")}, tid(1)))

    def test_a_file_that_is_not_a_store_is_none_and_never_raises(self) -> None:
        Path(self.store).write_bytes(b"this is not sqlite" * 50)
        self.assertIsNone(C.thread_row(self.env(), tid(1)))

    def test_a_store_without_a_table_or_a_column_still_reads(self) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("DROP TABLE thread_spawn_edges")
        conn.close()
        sid = self.add(1)
        self.assertEqual(C.thread_row(self.env(), sid).parent, "")  # no edges table
        old = os.path.join(self.tmp.name, "old")
        os.makedirs(old)
        conn = sqlite3.connect(os.path.join(old, "state_3.sqlite"))
        with conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, title TEXT, first_user_message TEXT)")
            conn.execute("INSERT INTO threads VALUES ('old-thread-0001', 'cli', 'T', 'hello')")
        conn.close()
        row = C.thread_row({"CODEX_HOME": old}, "old-thread-0001")  # no originator, no name, no thread_source
        self.assertEqual((row.originator, row.name, row.thread_source, row.first_message), ("", "", "", "hello"))
        self.assertEqual(C.classify(row), "chat")

    def test_the_parent_comes_from_the_spawn_edge_else_the_source(self) -> None:
        parent = self.add(1)
        edge = self.add_child(2, parent)
        bare = self.add_child(3, parent, edge=False)
        self.assertEqual(C.thread_row(self.env(), edge).parent, parent)
        self.assertEqual(C.thread_row(self.env(), bare).parent, parent)
        odd = self.add(4, source='{"subagent": nonsense', thread_source="subagent")
        self.assertEqual(C.thread_row(self.env(), odd).parent, "")

    def test_reading_leaves_the_store_alone(self) -> None:
        sid = self.add(1)
        before = Path(self.store).read_bytes()
        C.thread_row(self.env(), sid)
        self.assertEqual(Path(self.store).read_bytes(), before)

    def test_a_locked_store_is_waited_for_a_moment_then_none(self) -> None:
        sid = self.add(1)
        holder = sqlite3.connect(self.store, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        self.addCleanup(holder.close)
        started = time.monotonic()
        row = C.thread_row(self.env(), sid)
        self.assertLess(time.monotonic() - started, C.STORE_WAIT + 1.5)
        self.assertTrue(row is None or row.id == sid)  # never raises, whatever the journal mode allows


class ClassifyTests(Base):
    uses = 0

    def kind(self, **cols: object) -> str:
        self.uses += 1
        sid = self.add(100 + self.uses, **cols)
        return C.classify(C.thread_row(self.env(), sid))

    def test_an_interactive_chat_is_a_chat(self) -> None:
        self.assertEqual(self.kind(originator="Codex Desktop", source="vscode"), "chat")
        self.assertEqual(self.kind(originator="codex-tui", source="cli"), "chat")
        self.assertEqual(self.kind(originator=None, thread_source=None), "chat")  # an older store has neither

    def test_every_program_of_codexs_own_is_a_chat(self) -> None:
        for originator in ("codex_cli_rs", "codex-tui", "codex_vscode", "Codex Desktop", "CODEX_DESKTOP", "", " codex-tui "):
            self.assertEqual(self.kind(originator=originator, source="vscode"), "chat", originator)

    def test_what_belongs_to_something_else_is_headless(self) -> None:
        self.assertEqual(self.kind(originator="botfleet"), "headless")
        self.assertEqual(self.kind(originator="minimax-codex-mcp"), "headless")
        self.assertEqual(self.kind(originator="codex_exec", source="exec"), "headless")
        self.assertEqual(self.kind(source="exec"), "headless")
        self.assertEqual(self.kind(source="mcp"), "headless")

    def test_a_program_that_drives_codex_as_its_engine_is_headless(self) -> None:
        # The Mac store holds these (Bitrig, editorts_native, codex_sdk_ts), all with source vscode and no thread_source.
        for originator in ("Bitrig", "editorts_native", "codex_sdk_ts", "CODEX_SDK_TS", "some-new-editor", "BotFleet"):
            self.assertEqual(self.kind(originator=originator, source="vscode", thread_source=None), "headless", originator)
            self.assertEqual(self.kind(originator=originator, source="vscode", thread_source="user"), "headless")

    def test_the_originator_rule(self) -> None:
        self.assertTrue(C.own_originator(""))
        self.assertTrue(C.own_originator("Codex Desktop"))
        self.assertFalse(C.own_originator("Bitrig"))
        self.assertFalse(C.own_originator("codex_exec"))
        self.assertFalse(C.own_originator("minimax-codex-mcp"))  # carries the name, is not Codex's own

    def test_a_thread_that_is_not_a_chat_someone_talks_to_is_headless(self) -> None:
        for source in ("automation", "realtime_voice", "voice_chat", "guardian_review"):
            self.assertEqual(self.kind(thread_source=source), "headless", source)

    def test_a_spawned_agent_is_a_subagent_and_a_guardian_review_is_not(self) -> None:
        parent = self.add(1)
        self.assertEqual(C.classify(C.thread_row(self.env(), self.add_child(2, parent))), "subagent")
        guardian = self.add(3, source='{"subagent":{"other":"guardian"}}', thread_source="guardian_review")
        self.assertEqual(C.classify(C.thread_row(self.env(), guardian)), "headless")
        old = self.add(4, source=SUB_SOURCE % parent, thread_source=None)
        self.assertEqual(C.classify(C.thread_row(self.env(), old)), "subagent")

    def test_a_thread_the_store_does_not_show_takes_no_letter(self) -> None:
        self.assertEqual(C.classify(None), "headless")


class TaskNameTests(Base):
    uses = 0

    def name(self, **cols: object) -> str:
        self.uses += 1
        parent = self.add(100 + 2 * self.uses)
        return C.task_name(C.thread_row(self.env(), self.add_child(101 + 2 * self.uses, parent, **cols)))

    def test_the_task_is_the_last_part_of_the_path(self) -> None:
        self.assertEqual(self.name(path="/root/pong"), "Pong")
        self.assertEqual(self.name(path="/root/muse_diff_review"), "Muse Diff Review")
        self.assertEqual(self.name(path="/root/cost-review/fix_it/"), "Fix It")

    def test_no_task_falls_back_to_the_nickname_a_secret_never_becomes_a_title(self) -> None:
        self.assertEqual(self.name(path="/root", agent_nickname="Curie"), "Curie")
        self.assertEqual(self.name(path=None, agent_nickname="Hilbert"), "Hilbert")
        self.assertNotIn("ghp_", self.name(path="/root/" + secret_word()))


# --------------------------------------------------------------------------------------------
# The accessors
# --------------------------------------------------------------------------------------------


class AccessorTests(Base):
    def test_a_session_event_is_about_its_own_thread_a_subagent_event_about_the_agent(self) -> None:
        env = self.env(CODEX_THREAD_ID="from-env-0001")
        self.assertEqual(C.codex_session_id({"hook_event_name": "SessionStart", "session_id": "p-0001"}, env), "p-0001")
        self.assertEqual(C.codex_session_id({"hook_event_name": "SessionStart"}, env), "from-env-0001")
        sub = {"hook_event_name": "SubagentStart", "session_id": "p-0001", "agent_id": "c-0001"}
        self.assertEqual(C.codex_session_id(sub, env), "c-0001")
        self.assertEqual(C.codex_session_id({"hook_event_name": "SubagentStop", "session_id": "p-0001"}, env), "")

    def test_the_parent_of_a_subagent_event_is_the_session_that_fired_it(self) -> None:
        sub = {"hook_event_name": "SubagentStart", "session_id": "p-0001", "agent_id": "c-0001"}
        self.assertEqual(C.codex_parent(sub, self.env()), "p-0001")

    def test_the_parent_of_a_resumed_subagent_thread_is_in_the_store(self) -> None:
        parent = self.add(1)
        child = self.add_child(2, parent)
        event = {"hook_event_name": "SessionStart", "session_id": child}
        self.assertEqual(C.codex_parent(event, self.env()), parent)
        self.assertEqual(C.codex_kind(event, self.env()), "subagent")
        self.assertEqual(C.codex_parent({"hook_event_name": "SessionStart", "session_id": parent}, self.env()), "")

    def test_a_subagent_event_is_lettered_when_the_chat_that_started_it_is(self) -> None:
        chat, bot = self.add(1), self.add(2, originator="botfleet")
        child = self.add_child(3, chat)
        event = {"hook_event_name": "SubagentStart", "session_id": chat, "agent_id": child}
        self.assertEqual(C.codex_kind(event, self.env()), "subagent")
        self.assertEqual(C.codex_kind(dict(event, session_id=bot), self.env()), "headless")
        self.assertEqual(C.codex_kind(dict(event, session_id=tid(77)), self.env()), "headless")  # not in the store
        # The child's row, once it is there, can rule it out; before it is there, the parent decides.
        guardian = self.add(4, source='{"subagent":{"other":"guardian"}}', thread_source="guardian_review")
        self.assertEqual(C.codex_kind(dict(event, agent_id=guardian), self.env()), "headless")
        self.assertEqual(C.codex_kind(dict(event, agent_id=tid(88)), self.env()), "subagent")

    def test_an_event_inside_a_subagent_turn_is_not_its_parents_chat(self) -> None:
        chat = self.add(1)
        inside = {"hook_event_name": "UserPromptSubmit", "session_id": chat, "agent_id": tid(5), "agent_type": "default"}
        self.assertEqual(C.codex_kind(inside, self.env()), "headless")
        self.assertEqual(C.codex_kind({"hook_event_name": "UserPromptSubmit", "session_id": chat}, self.env()), "chat")

    def test_the_current_title_is_the_threads_name_a_subagents_task_stands_in(self) -> None:
        chat = self.add(1, name="Fleet Sweep")
        unnamed = self.add(2)
        child = self.add_child(3, chat)
        prompt = lambda sid: {"hook_event_name": "UserPromptSubmit", "session_id": sid}  # noqa: E731
        self.assertEqual(C.codex_current_title(prompt(chat), self.env()), "Fleet Sweep")
        self.assertEqual(C.codex_current_title(prompt(unnamed), self.env()), "")
        self.assertEqual(C.codex_current_title(prompt(tid(9)), self.env()), "")
        sub = {"hook_event_name": "SubagentStart", "session_id": chat, "agent_id": child}
        self.assertEqual(C.codex_current_title(sub, self.env()), "Pong")
        self.set_name(child, "AB: Pong Review")
        self.assertEqual(C.codex_current_title(sub, self.env()), "AB: Pong Review")

    def test_a_subagent_event_that_names_no_thread_does_not_wait_for_a_row(self) -> None:
        # A malformed SubagentStart used to spend the 1.5 s row wait on a thread that cannot appear.
        with mock.patch.object(C, "_wait_for_row", side_effect=AssertionError("waited")) as waited:
            self.assertEqual(C.codex_current_title({"hook_event_name": "SubagentStart"}, self.env()), "")
        waited.assert_not_called()

    def test_a_subagent_row_that_is_written_a_moment_late_is_waited_for(self) -> None:
        chat = self.add(1)
        timer = threading.Timer(0.3, self.add_child, args=(2, chat))
        timer.start()
        self.addCleanup(timer.cancel)
        sub = {"hook_event_name": "SubagentStart", "session_id": chat, "agent_id": tid(2)}
        self.assertEqual(C.codex_current_title(sub, self.env()), "Pong")

    def test_a_subagent_that_never_appears_does_not_hold_the_hook(self) -> None:
        sub = {"hook_event_name": "SubagentStart", "session_id": self.add(1), "agent_id": tid(2)}
        started = time.monotonic()
        with mock.patch.object(C, "CHILD_ROW_WAIT", 0.3):
            self.assertEqual(C.codex_current_title(sub, self.env()), "")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_chat_never_named_is_titled_from_its_first_message(self) -> None:
        resumed = self.add(1, first_user_message="rotate the infisical tokens " + secret_word())
        named = self.add(2, name="Fleet Sweep", first_user_message="something else")
        event = lambda sid: {"hook_event_name": "SessionStart", "session_id": sid}  # noqa: E731
        self.assertEqual(C.codex_stored_title(event(resumed), self.env()), "Rotate the Infisical Tokens")
        self.assertEqual(C.codex_stored_title(event(named), self.env()), "Fleet Sweep")
        self.assertEqual(C.codex_stored_title(event(tid(9)), self.env()), "")


# --------------------------------------------------------------------------------------------
# The app-server
# --------------------------------------------------------------------------------------------


class RpcTests(unittest.TestCase):
    def conversation(self, framing: str, handler=standard, **kw) -> tuple[FakeServer, C.Rpc]:
        server = FakeServer(framing, handler, **kw)
        rpc = C.Rpc(["codex"], {}, deadline(), framing=framing, popen=lambda argv, **k: server)
        return server, rpc

    def test_both_framings_initialize_then_answer(self) -> None:
        for framing in ("lines", "ws"):
            server, rpc = self.conversation(framing)
            with rpc:
                self.assertEqual(rpc.call("thread/name/set", {"threadId": "t", "name": "n"}), {})
            server.wait(2)
            self.assertEqual([m for m, _ in server.requests], ["initialize", "thread/name/set"], framing)
            self.assertEqual(server.requests[0][1], {"clientInfo": C.CLIENT_INFO})
            self.assertEqual(server.requests[1][1], {"threadId": "t", "name": "n"})
            self.assertEqual(server.notes, ["initialized"], framing)

    def test_messages_that_are_not_the_answer_are_dropped(self) -> None:
        def noisy(server: FakeServer, msg: dict) -> None:
            if msg["method"] == "thread/name/set":
                server.send({"method": "thread/name/updated", "params": {"threadId": "t"}})
                server.send({"id": msg["id"], "method": "item/commandExecution/requestApproval", "params": {}})
                server.send({"id": msg["id"] + 100, "result": {"other": True}})
            standard(server, msg)

        for framing in ("lines", "ws"):
            server, rpc = self.conversation(framing, noisy)
            with rpc:
                self.assertEqual(rpc.call("thread/name/set", {"threadId": "t", "name": "n"}), {})

    def test_an_error_is_an_rpc_error_that_never_repeats_the_request(self) -> None:
        server, rpc = self.conversation("lines")
        with rpc:
            with self.assertRaises(C.RpcError) as caught:
                rpc.call("thread/read", {"threadId": "t", "name": secret_word()})
        self.assertIn("thread/read", str(caught.exception))
        self.assertNotIn("ghp_", str(caught.exception))

    def test_a_server_that_never_answers_runs_out_of_time(self) -> None:
        server = FakeServer("lines", lambda s, m: None)
        rpc = C.Rpc(["codex"], {}, deadline(0.4), framing="lines", popen=lambda argv, **k: server)
        started = time.monotonic()
        with self.assertRaises(C.RpcError):
            with rpc:
                pass
        self.assertLess(time.monotonic() - started, 3.0)

    def test_a_server_that_hangs_up_is_an_rpc_error(self) -> None:
        def hang_up(server: FakeServer, msg: dict) -> None:
            os.close(server._wfd)
            server._wfd = os.open(os.devnull, os.O_WRONLY)

        server = FakeServer("lines", hang_up)
        with self.assertRaises(C.RpcError):
            with C.Rpc(["codex"], {}, deadline(), framing="lines", popen=lambda argv, **k: server):
                pass

    def test_a_refused_or_wrong_upgrade_is_an_rpc_error(self) -> None:
        server = FakeServer("ws", standard, bad_accept=True)
        with self.assertRaises(C.RpcError):
            with C.Rpc(["codex"], {}, deadline(), framing="ws", popen=lambda argv, **k: server):
                pass

    def test_a_large_message_crosses_the_websocket_framing(self) -> None:
        def big(server: FakeServer, msg: dict) -> None:
            server.reply(msg["id"], {"data": "x" * 70000} if msg["method"] == "thread/list" else {})

        server, rpc = self.conversation("ws", big)
        with rpc:
            self.assertEqual(len(rpc.call("thread/list", {})["data"]), 70000)
            self.assertEqual(rpc.call("thread/name/set", {"name": "y" * 300}), {})  # a 16-bit frame length
        self.assertEqual(server.requests[-1][1]["name"], "y" * 300)


class BinaryTests(Base):
    def test_the_named_program_wins_and_is_not_second_guessed(self) -> None:
        named = self.exe("named", "codex")
        self.assertEqual(C.codex_binary(self.env(CODEX_LETTER_BIN=named, PATH="/nonexistent")), named)
        self.assertEqual(C.codex_binary(self.env(CODEX_LETTER_BIN="/nonexistent/codex")), "")

    def test_the_path_then_the_usual_places(self) -> None:
        on_path = self.exe("bin", "codex")
        self.assertEqual(C.codex_binary(self.env(PATH=os.path.dirname(on_path))), on_path)
        local = self.exe("home", ".local", "bin", "codex")
        with mock.patch.object(C, "BIN_CANDIDATES", (".local/bin/codex", "/nonexistent/bin/codex")):
            self.assertEqual(C.codex_binary(self.env(PATH="/nonexistent")), local)
            os.remove(local)
            self.assertEqual(C.codex_binary(self.env(PATH="/nonexistent")), "")

    def test_the_daemon_socket_is_there_only_when_the_daemon_is(self) -> None:
        self.assertEqual(C.daemon_socket(self.env()), "")
        path = os.path.join(self.codex, "app-server-control", "app-server-control.sock")
        os.makedirs(os.path.dirname(path))
        Path(path).write_bytes(b"")
        self.assertEqual(C.daemon_socket(self.env()), path)

    def test_the_daemon_is_asked_through_the_proxy_else_a_server_of_our_own(self) -> None:
        binary = self.exe("bin", "codex")
        seen = []

        def popen(argv, **kw):
            seen.append(argv)
            return FakeServer("ws" if argv[-1] == "proxy" else "lines", standard)

        env = self.env(CODEX_LETTER_BIN=binary)
        for daemon, framing, argv in ((True, "ws", [binary, "app-server", "proxy"]), (False, "lines", [binary, "app-server"])):
            with C.open_rpc(env, deadline(), daemon=daemon, popen=popen) as rpc:
                self.assertEqual(rpc.framing, framing)
            self.assertEqual(seen[-1], argv)
        with self.assertRaises(C.RpcError):
            C.open_rpc(self.env(CODEX_LETTER_BIN="/nonexistent/codex"), deadline(), daemon=False, popen=popen)


class RenameTests(Base):
    """`rename_thread` against a fake app-server that writes the name into the store, as the real one does."""

    def setUp(self) -> None:
        super().setUp()
        self.binary = self.exe("bin", "codex")
        self.argvs: list[list[str]] = []
        self.handler = self.writes_the_name

    def writes_the_name(self, server: FakeServer, msg: dict) -> None:
        if msg["method"] == "thread/name/set":
            self.set_name(msg["params"]["threadId"], msg["params"]["name"])
        standard(server, msg)

    def popen(self, argv, **kw):
        self.argvs.append(argv)
        handler = self.handler if argv[-1] != "proxy" or self.daemon_ok else (lambda s, m: s.fail(m["id"]))
        return FakeServer("ws" if argv[-1] == "proxy" else "lines", handler)

    daemon_ok = True

    def with_daemon(self) -> None:
        path = os.path.join(self.codex, "app-server-control", "app-server-control.sock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_bytes(b"")

    def rename(self, sid: str, name: str) -> bool:
        return C.rename_thread(sid, name, self.env(CODEX_LETTER_BIN=self.binary), popen=self.popen)

    def test_the_daemon_route_names_the_thread(self) -> None:
        self.with_daemon()
        sid = self.add(1)
        self.assertTrue(self.rename(sid, "AA: Fix Login"))
        self.assertEqual(self.stored_name(sid), "AA: Fix Login")
        self.assertEqual([a[-1] for a in self.argvs], ["proxy"])

    def test_without_a_daemon_a_server_of_our_own_does_it(self) -> None:
        sid = self.add(1)
        self.assertTrue(self.rename(sid, "AA: Fix Login"))
        self.assertEqual(self.stored_name(sid), "AA: Fix Login")
        self.assertEqual([a[-1] for a in self.argvs], ["app-server"])

    def test_a_daemon_that_fails_falls_back_to_a_server_of_our_own(self) -> None:
        self.with_daemon()
        self.daemon_ok = False
        sid = self.add(1)
        self.assertTrue(self.rename(sid, "AA: Fix Login"))
        self.assertEqual(self.stored_name(sid), "AA: Fix Login")
        self.assertEqual([a[-1] for a in self.argvs], ["proxy", "app-server"])

    def test_a_name_that_never_reaches_the_store_is_not_a_success(self) -> None:
        self.handler = standard  # accepts the request, writes nothing
        sid = self.add(1)
        self.assertFalse(self.rename(sid, "AA: Fix Login"))

    def test_nothing_to_name_or_nothing_to_ask(self) -> None:
        sid = self.add(1)
        self.assertFalse(self.rename("", "AA: X"))
        self.assertFalse(self.rename(sid, "  \n "))
        self.assertEqual(self.argvs, [])
        env = self.env(CODEX_LETTER_BIN="/nonexistent/codex")
        self.assertFalse(C.rename_thread(sid, "AA: X", env, popen=self.popen))

    def test_a_program_that_cannot_start_is_a_failure_not_a_crash(self) -> None:
        def broken(argv, **kw):
            raise OSError("exec format error")

        sid = self.add(1)
        self.assertFalse(C.rename_thread(sid, "AA: X", self.env(CODEX_LETTER_BIN=self.binary), popen=broken))

    def test_the_name_is_one_line_with_no_control_characters(self) -> None:
        sid = self.add(1)
        self.assertTrue(self.rename(sid, "AA:  Fix\n\x00Login \ud83d"))
        self.assertEqual(self.stored_name(sid), "AA: Fix Login")

    def test_the_whole_rename_stays_inside_its_budget(self) -> None:
        self.with_daemon()
        self.handler = lambda server, msg: None  # nothing ever answers
        sid = self.add(1)
        started = time.monotonic()
        with mock.patch.object(C, "RPC_SECONDS", 1.0), mock.patch.object(C, "DAEMON_SECONDS", 0.4):
            self.assertFalse(self.rename(sid, "AA: X"))
        self.assertLess(time.monotonic() - started, 3.0)

    def test_the_spec_writer_renames_the_requests_own_thread(self) -> None:
        chat, child = self.add(1), self.add(2)
        request = PL.TitleRequest(
            platform="codex", session_id=child, label="AB", title="AB: Pong", words="Pong", current="",
            reason="start", data={"session_id": chat, "agent_id": child},
            env=self.env(CODEX_LETTER_BIN=self.binary),
        )
        with mock.patch.object(C, "open_rpc", side_effect=lambda env, dl, *, daemon, popen=None: FakeApp(self, [])):
            self.assertTrue(C.codex_rename(request))
        self.assertEqual((self.stored_name(child), self.stored_name(chat)), ("AB: Pong", None))


# --------------------------------------------------------------------------------------------
# The hook, end to end in process
# --------------------------------------------------------------------------------------------


class ChatTests(HookBase):
    def test_a_new_chat_gets_a_pair_then_a_name(self) -> None:
        sid = self.add(1, originator="Codex Desktop", source="vscode")
        out = self.start(sid)
        label = self.note(sid)["label"]
        self.assertRegex(label, PAIR)
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("Codex %s" % label, out["additionalContext"])
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])
        self.assertEqual(self.stored_name(sid), "%s: New Chat" % label)
        self.assertEqual(self.lookup(sid)["label"], label)
        self.assertEqual(self.lookup(sid)["seat"], "CODEX")
        self.prompt(sid, "help get all PR issues addressed from review findings.")
        self.assertEqual(self.stored_name(sid), "%s: Help Get All PR Issues Addressed" % label)
        self.assertEqual(self.lookup(sid)["title"], "Help Get All PR Issues Addressed")
        calls = len(self.renames)
        self.assertIsNone(self.prompt(sid, "and another thing"))
        self.assertEqual(len(self.renames), calls)  # a steady-state prompt asks the app-server for nothing

    def test_the_rename_names_the_chat_by_its_own_thread_id(self) -> None:
        sid = self.add(1)
        self.start(sid)
        method, params = self.renames[-1]
        self.assertEqual((method, params["threadId"]), ("thread/name/set", sid))

    def test_a_chat_someone_named_keeps_their_words_under_the_letter(self) -> None:
        sid = self.add(1, name="Fleet PR Sweep")
        self.start(sid, source="resume")
        self.assertEqual(self.stored_name(sid), "%s: Fleet PR Sweep" % self.note(sid)["label"])
        self.assertEqual(self.lookup(sid)["title"], "Fleet PR Sweep")

    def test_a_resumed_chat_that_was_never_named_is_titled_from_its_first_message(self) -> None:
        sid = self.add(1, first_user_message="rotate the infisical tokens " + secret_word())
        self.start(sid, source="resume")
        self.assertEqual(self.stored_name(sid), "%s: Rotate the Infisical Tokens" % self.note(sid)["label"])
        self.assertNotIn("ghp_", self.stored_name(sid))

    def test_a_name_that_dropped_the_letter_gets_it_back(self) -> None:
        sid = self.add(1)
        self.start(sid)
        self.prompt(sid, "first words here")
        label = self.note(sid)["label"]
        self.set_name(sid, "My Own Words")
        self.prompt(sid, "go on")
        self.assertEqual(self.stored_name(sid), "%s: My Own Words" % label)
        self.assertEqual(self.lookup(sid)["title"], "My Own Words")

    def test_a_rename_that_keeps_the_letter_is_left_alone_and_sent_to_the_operator(self) -> None:
        sid = self.add(1)
        self.start(sid)
        self.prompt(sid, "first words here")
        label = self.note(sid)["label"]
        calls = len(self.renames)
        self.set_name(sid, "%s: Renamed By Hand" % label)
        self.prompt(sid, "go on")
        self.assertEqual(len(self.renames), calls)
        self.assertEqual(self.lookup(sid)["title"], "Renamed By Hand")

    def test_a_fork_keeps_the_words_and_drops_the_other_chats_letter(self) -> None:
        sid = self.add(1, name="ZZ: Original Words")
        self.start(sid, source="fork")
        label = self.note(sid)["label"]
        self.assertEqual(self.stored_name(sid), "%s: Original Words" % label)

    def test_a_compact_only_reminds(self) -> None:
        sid = self.add(1)
        self.start(sid)
        label = self.note(sid)["label"]
        calls = len(self.renames)
        out = self.start(sid, source="compact")
        self.assertIn("Codex %s" % label, out["additionalContext"])
        self.assertEqual(len(self.renames), calls)

    def test_two_chats_get_two_pairs(self) -> None:
        first, second = self.add(1), self.add(2)
        self.start(first)
        self.start(second)
        self.assertNotEqual(self.note(first)["label"], self.note(second)["label"])
        self.assertRegex(self.note(second)["label"], PAIR)

    def test_a_writer_that_fails_leaves_the_title_pending_and_the_chat_alone(self) -> None:
        self.rpc_error = C.RpcError("the app-server did not answer in time")
        sid = self.add(1)
        out = self.start(sid)
        note = self.note(sid)
        self.assertIn(note["label"], out["additionalContext"])  # the letter is still told to the model
        self.assertTrue(note["pending"])
        self.assertIsNone(self.stored_name(sid))
        self.prompt(sid, "words for the title")  # the first title is tried at once, and fails the same way
        self.assertTrue(self.note(sid)["pending"])
        self.assertIsNone(self.stored_name(sid))
        self.rpc_error = None
        calls = len(self.renames)
        self.prompt(sid, "and more")
        self.assertEqual((len(self.renames), self.stored_name(sid)), (calls, None))  # not asked again yet
        later = self.note(sid)["retry_at"] + 1
        out = io.StringIO()
        PL.hook(C.SPEC, self.env(), io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid,
                "prompt": "even more"})), out, platform="darwin", now=later)
        self.assertEqual(self.stored_name(sid), "%s: Words for the Title" % note["label"])
        self.assertFalse(self.note(sid)["pending"])

    def test_the_server_registers_through_its_transport_and_names_the_chat(self) -> None:
        sid = self.add(1)
        calls = []

        def to_the_mac(argv: list[str]) -> dict:
            calls.append(argv[0])
            out = io.StringIO()
            PL.registry_main(argv, env=self.env(), stdout=out)
            return json.loads(out.getvalue())

        out = self.start(sid, platform="linux", transport=to_the_mac, location_flag="server", remote_flag="mac")
        self.assertEqual(calls, ["register"])
        self.assertRegex(self.lookup(sid, "server")["label"], PAIR)
        self.assertEqual(self.stored_name(sid), "%s: New Chat" % self.note(sid)["label"])
        self.assertIn("server", out["additionalContext"])

    def test_a_host_that_is_not_named_takes_no_letter(self) -> None:
        sid = self.add(1)
        out = self.start(sid, platform="linux")
        self.assertIn("No Operator letter", out["additionalContext"])
        self.assertEqual((self.renames, self.registered()), ([], 0))


class ScopeTests(HookBase):
    def nothing_happened(self, sid: str) -> None:
        self.assertEqual(self.renames, [])
        self.assertEqual(self.registered(), 0)
        self.assertFalse(self.has_note(sid))

    def test_a_botfleet_thread_takes_no_letter(self) -> None:
        for originator in ("botfleet", "minimax-codex-mcp"):
            sid = self.add(1 if originator == "botfleet" else 2, originator=originator)
            self.assertIsNone(self.start(sid))
            self.assertIsNone(self.prompt(sid, "do the thing"))
            self.nothing_happened(sid)

    def test_a_program_that_drives_codex_takes_no_letter_and_neither_do_its_sub_agents(self) -> None:
        for n, originator in enumerate(("Bitrig", "editorts_native", "codex_sdk_ts")):
            sid = self.add(30 + 2 * n, originator=originator, source="vscode", thread_source=None)
            self.assertIsNone(self.start(sid))
            self.assertIsNone(self.prompt(sid, "do the thing"))
            self.nothing_happened(sid)
            child = self.add_child(31 + 2 * n, sid, originator=originator)
            self.assertIsNone(self.sub_start(sid, child))
            self.assertIsNone(self.sub_stop(sid, child))
            self.nothing_happened(child)

    def test_an_exec_or_mcp_run_takes_no_letter(self) -> None:
        for n, cols in enumerate(({"originator": "codex_exec", "source": "exec"}, {"source": "mcp"}, {"source": "exec"})):
            sid = self.add(10 + n, **cols)
            self.assertIsNone(self.start(sid))
            self.nothing_happened(sid)

    def test_a_thread_that_is_not_a_chat_takes_no_letter(self) -> None:
        for n, source in enumerate(("automation", "voice_chat", "guardian_review")):
            sid = self.add(20 + n, thread_source=source)
            self.assertIsNone(self.start(sid))
            self.nothing_happened(sid)

    def test_a_thread_the_store_does_not_show_takes_no_letter(self) -> None:
        self.assertIsNone(self.start(tid(500)))
        self.assertEqual(self.registered(), 0)
        self.assertIsNone(self.run_hook({"hook_event_name": "SessionStart", "session_id": tid(501)},
                                        env={"HOME": self.home, "CODEX_HOME": os.path.join(self.tmp.name, "nowhere"),
                                             "AGENT_SYNC_STATE_DIR": self.state}))
        self.assertEqual(self.registered(), 0)

    def test_a_launcher_run_session_takes_no_letter(self) -> None:
        sid = self.add(1)
        for extra in ({"AGENT_LAUNCHER": "botfleet"}, {"AGENT_LAUNCH_SEAT": "CODEX"},
                      {"AGENT_LAUNCHER": "agent-sync-wake", "AGENT_LAUNCH_SEAT": "CODEX"}):
            self.assertIsNone(self.start(sid, env=self.env(**extra)))
            self.nothing_happened(sid)

    def test_the_hook_can_be_turned_off(self) -> None:
        sid = self.add(1)
        for off in ("SESSION_LETTER", "CODEX_LETTER"):
            self.assertIsNone(self.start(sid, env=self.env(**{off: "0"})))
            self.nothing_happened(sid)

    def test_an_event_the_adapter_does_not_hook_is_ignored(self) -> None:
        sid = self.add(1)
        for name in ("SessionEnd", "Stop", "PreToolUse", ""):
            self.assertIsNone(self.run_hook({"hook_event_name": name, "session_id": sid}))
        self.nothing_happened(sid)

    def test_a_broken_store_never_breaks_the_hook(self) -> None:
        sid = self.add(1)
        Path(self.store).write_bytes(b"garbage" * 100)
        self.assertIsNone(self.start(sid))
        self.assertEqual(self.registered(), 0)


class SubagentTests(HookBase):
    def chat(self, n: int = 1) -> str:
        sid = self.add(n)
        self.start(sid)
        self.prompt(sid, "spawn a helper")
        return sid

    def test_a_subagent_gets_a_pair_and_its_own_name(self) -> None:
        parent = self.chat()
        child = self.add_child(2, parent)
        out = self.sub_start(parent, child)
        label, parent_label = self.note(child)["label"], self.note(parent)["label"]
        self.assertRegex(label, PAIR)
        self.assertNotEqual(label, parent_label)
        self.assertEqual(self.stored_name(child), "%s: Pong" % label)
        self.assertEqual(self.stored_name(parent), "%s: Spawn a Helper" % parent_label)  # the parent's name is untouched
        self.assertEqual(self.renames[-1][1]["threadId"], child)
        self.assertIn("Codex %s" % label, out["additionalContext"])
        self.assertEqual(self.lookup(child)["title"], "Pong")
        self.assertEqual(self.note(child)["parent"], parent)

    def test_the_first_sub_thread_takes_the_pair_right_after_its_parent(self) -> None:
        # Main's registry gives the lowest free pair and the pair registry the one after the parent's; with one chat
        # and one child both are the next pair, AA then AB.
        parent = self.chat()
        child = self.add_child(2, parent)
        self.sub_start(parent, child)
        a, b = self.note(parent)["label"], self.note(child)["label"]
        self.assertEqual((a, b), ("AA", "AB"))

    @unittest.skipUnless(HAS_PARENT, "the registry of this checkout has no parent-adjacent allocation yet")
    def test_a_sub_thread_follows_its_parent_not_the_oldest_free_pair(self) -> None:
        first, second = self.chat(1), self.chat(2)
        child = self.add_child(3, second)
        self.sub_start(second, child)
        label, parent_label = self.note(child)["label"], self.note(second)["label"]
        self.assertEqual(label[0], parent_label[0])
        self.assertGreater(label, parent_label)
        self.assertNotEqual(label, self.note(first)["label"])

    def spy(self, **kw):
        """A transport onto the temporary registry that keeps the verbs it was asked (the Mac side, in process)."""
        calls: list[str] = []

        def transport(argv: list[str]) -> dict:
            calls.append(argv[0])
            out = io.StringIO()
            PL.registry_main(argv, env=self.env(), stdout=out)
            return json.loads(out.getvalue())

        transport.calls = calls  # type: ignore[attr-defined]
        return transport

    def test_a_stopped_subagent_keeps_its_registration_and_its_pair(self) -> None:
        # SubagentStop fires at the end of every turn of a sub-agent, so it is no end: the pair stays the sub-agent's,
        # as its title says, and a sibling that starts later takes the next pair.
        parent = self.chat()
        child = self.add_child(2, parent)
        self.sub_start(parent, child)
        label = self.note(child)["label"]
        self.assertIsNone(self.sub_stop(parent, child))
        self.assertEqual(self.lookup(child)["state"], "active")
        self.assertEqual(self.note(child)["state"], "open")
        self.assertEqual(self.lookup(parent)["state"], "active")
        again = self.add_child(3, parent, path="/root/second")
        self.sub_start(parent, again)
        self.assertNotEqual(self.note(again)["label"], label)
        self.assertEqual(self.stored_name(child), "%s: Pong" % label)
        self.assertEqual(self.stored_name(again), "%s: Second" % self.note(again)["label"])

    def test_a_follow_up_turn_of_a_subagent_does_not_free_its_pair(self) -> None:
        # The capture of a real run: SubagentStart once, SubagentStop after each of the two turns, the same agent_id.
        parent = self.chat()
        a = self.add_child(2, parent, path="/root/task_one")
        self.sub_start(parent, a)
        label = self.note(a)["label"]
        self.sub_stop(parent, a, "PONG")
        self.sub_stop(parent, a, "PING")
        self.assertEqual((self.lookup(a)["state"], self.lookup(a)["label"]), ("active", label))
        b = self.add_child(3, parent, path="/root/task_two")
        self.sub_start(parent, b)
        self.assertNotEqual(self.note(b)["label"], label)
        self.assertEqual(self.lookup(a)["state"], "active")
        self.assertEqual(self.stored_name(a), "%s: Task One" % label)
        self.assertEqual(self.stored_name(b), "%s: Task Two" % self.note(b)["label"])

    def test_siblings_that_finish_one_before_the_next_starts_take_one_pair_each(self) -> None:
        # The owner's rule: parent CC, child CD, then CE.  Finished sub-agents keep their title, so their pair is theirs.
        parent = self.chat()
        labels = []
        for n in range(2, 6):
            child = self.add_child(n, parent, path="/root/task_%d" % n)
            self.sub_start(parent, child)
            self.sub_stop(parent, child)
            labels.append(self.note(child)["label"])
        self.assertEqual(len(set(labels)), 4)
        self.assertEqual(labels, sorted(labels))
        self.assertNotIn(self.note(parent)["label"], labels)
        if HAS_PARENT:
            self.assertEqual({label[0] for label in labels}, {self.note(parent)["label"][0]})

    def test_a_grandchild_spawned_in_a_follow_up_turn_follows_its_sub_agent(self) -> None:
        parent = self.chat(1)
        other = self.chat(2)  # a second main chat, so "after its parent" differs from "the oldest free pair"
        a = self.add_child(3, other, path="/root/task_one")
        self.sub_start(other, a)
        self.sub_stop(other, a)
        grand = self.add_child(4, a, path="/root/helper")
        self.sub_start(a, grand)
        self.assertEqual(self.note(a)["state"], "open")
        self.assertEqual(self.note(grand)["parent"], a)
        self.assertEqual(self.lookup(grand)["state"], "active")
        self.assertNotIn(self.note(grand)["label"], (self.note(parent)["label"], self.note(other)["label"]))
        if HAS_PARENT:
            self.assertEqual(self.note(grand)["label"][0], self.note(a)["label"][0])
            self.assertGreater(self.note(grand)["label"], self.note(a)["label"])

    def test_a_stop_prints_nothing_even_when_it_registers_the_subagent(self) -> None:
        # SubagentStop has no hookSpecificOutput in Codex's schema, so the hook says nothing on it.
        parent = self.chat()
        child = self.add_child(2, parent)  # it started before the hook was installed: no SubagentStart was seen
        self.assertIsNone(self.sub_stop(parent, child))
        self.assertRegex(self.note(child)["label"], PAIR)  # the stop registered it
        self.assertEqual(self.stored_name(child), "%s: Pong" % self.note(child)["label"])
        self.assertEqual(self.note(child)["parent"], parent)
        out = self.sub_start(parent, self.add_child(3, parent))
        self.assertIn("additionalContext", out)  # a start still tells the sub-agent its letter

    def test_a_subagent_that_failed_to_register_at_its_start_is_tried_again_at_a_stop(self) -> None:
        parent = self.chat()
        child = self.add_child(2, parent)
        t0 = time.time()
        out = self.sub_start(parent, child, now=t0, transport=lambda argv: {"error": "the registry host did not answer"})
        self.assertEqual(self.note(child)["state"], "failed")
        self.assertIn("end of its next turn", out["additionalContext"])  # the line that says when it is tried again
        self.assertIsNone(self.stored_name(child))
        self.assertIsNone(self.sub_stop(parent, child, now=t0 + 30))  # inside the ten minutes: left alone
        self.assertEqual(self.note(child)["state"], "failed")
        self.assertIsNone(self.sub_stop(parent, child, now=t0 + PL.RETRY_SECONDS + 5))
        label = self.note(child)["label"]
        self.assertRegex(label, PAIR)
        self.assertEqual((self.note(child)["state"], self.lookup(child)["state"]), ("open", "active"))
        self.assertEqual(self.stored_name(child), "%s: Pong" % label)

    def test_a_subagent_title_that_failed_is_tried_again_at_a_stop(self) -> None:
        parent = self.chat()
        child = self.add_child(2, parent)
        self.rpc_error = C.RpcError("the app-server did not answer in time")
        t0 = time.time()
        self.sub_start(parent, child, now=t0)
        self.assertTrue(self.note(child)["pending"])
        self.assertIsNone(self.stored_name(child))
        self.rpc_error = None
        self.assertIsNone(self.sub_stop(parent, child, now=t0 + PL.RETRY_SECONDS + 5))
        self.assertEqual(self.stored_name(child), "%s: Pong" % self.note(child)["label"])
        self.assertFalse(self.note(child)["pending"])

    def test_a_stop_sends_a_touch_only_when_the_registration_is_half_an_hour_quiet(self) -> None:
        parent = self.chat()
        child = self.add_child(2, parent)
        seen = self.spy()
        t0 = time.time()
        self.sub_start(parent, child, now=t0, transport=seen)
        self.sub_stop(parent, child, now=t0 + 60, transport=seen)
        self.sub_stop(parent, child, now=t0 + 120, transport=seen)
        self.assertEqual(seen.calls, ["register"])  # a steady-state stop costs no hop
        self.sub_stop(parent, child, now=t0 + PL.TOUCH_SECONDS + 5, transport=seen)
        self.assertEqual(seen.calls, ["register", "touch"])

    def test_a_subagent_that_is_renamed_keeps_the_letter_through_a_stop(self) -> None:
        parent = self.chat()
        child = self.add_child(2, parent)
        self.sub_start(parent, child)
        label = self.note(child)["label"]
        self.set_name(child, "Pong, Renamed By Hand")
        self.sub_stop(parent, child)
        self.assertEqual(self.stored_name(child), "%s: Pong, Renamed By Hand" % label)
        self.assertEqual(self.lookup(child)["title"], "Pong, Renamed By Hand")

    def test_a_parent_that_holds_no_registration_is_registered_before_its_subagent(self) -> None:
        # Its own start failed (a registry that answered an error), so the Operator does not know it, and a child
        # registered first would take a main pair ahead of it.
        parent = self.add(1)
        t0 = time.time()
        self.start(parent, now=t0, transport=lambda argv: {"error": "no Operator registry on this host"})
        self.assertEqual(self.note(parent)["state"], "failed")
        child = self.add_child(2, parent)
        self.sub_start(parent, child, now=t0 + 5)
        self.assertEqual(self.note(parent)["state"], "open")
        p, c = self.note(parent)["label"], self.note(child)["label"]
        self.assertLess(p, c)
        if HAS_PARENT:
            self.assertEqual(p[0], c[0])
        self.assertEqual(self.lookup(parent)["state"], "active")
        # The parent is named and told its letter by its own next prompt.
        out = self.prompt(parent, "fix the build", now=t0 + 10)
        self.assertEqual(self.stored_name(parent), "%s: Fix the Build" % p)
        self.assertIn('"@**Codex** %s"' % p, out["additionalContext"])

    def test_a_parent_the_hook_never_saw_is_registered_before_its_subagent(self) -> None:
        parent = self.add(1, name="Fleet PR Sweep")  # a chat that began before the hook was installed
        child = self.add_child(2, parent)
        seen = self.spy()
        self.sub_start(parent, child, transport=seen)
        self.assertEqual(seen.calls, ["register", "register"])
        self.assertLess(self.note(parent)["label"], self.note(child)["label"])
        self.prompt(parent, "go on")
        self.assertEqual(self.stored_name(parent), "%s: Fleet PR Sweep" % self.note(parent)["label"])

    def test_a_parent_that_is_registered_costs_its_subagent_no_extra_hop(self) -> None:
        parent = self.chat()
        seen = self.spy()
        for n in (2, 3, 4):
            self.sub_start(parent, self.add_child(n, parent, path="/root/task_%d" % n), transport=seen)
        self.assertEqual(seen.calls, ["register"] * 3)

    def test_a_chain_of_subagents_is_registered_from_the_top(self) -> None:
        root = self.add(1)
        a = self.add_child(2, root, path="/root/task_one")
        grand = self.add_child(3, a, path="/root/helper")
        seen = self.spy()
        self.sub_start(a, grand, transport=seen)  # neither root nor a was ever seen
        self.assertEqual(seen.calls, ["register", "register", "register"])
        labels = [self.note(sid)["label"] for sid in (root, a, grand)]
        self.assertEqual(labels, sorted(labels))
        self.assertEqual(len(set(labels)), 3)

    def test_a_silent_registry_host_is_asked_once_for_a_fan_out_of_subagents(self) -> None:
        # Server side: every SubagentStart carries a new agent id with no note of its own, so the ten minutes a failed
        # chat waits cannot spare it the ssh timeout.  The host-wide mark does.
        parent = self.add(1)
        env = self.env(SESSION_LETTER_LOCATION="server", SESSION_LETTER_REMOTE="mac")
        calls: list = []

        def ssh(cmd, **kwargs):
            calls.append(cmd)
            raise subprocess.TimeoutExpired("ssh", 8)

        t0 = time.time()
        self.start(parent, env=env, platform="linux", now=t0, runner=ssh)
        for n in range(2, 8):
            child = self.add_child(n, parent, path="/root/task_%d" % n)
            out = self.sub_start(parent, child, env=env, platform="linux", now=t0 + n, runner=ssh)
            self.assertIn("No Operator letter yet", out["additionalContext"])
            self.assertEqual(self.note(child)["state"], "failed")
        self.assertEqual(len(calls), 1)  # one timeout for the parent's start, none for the six sub-agents
        later = t0 + PL.RETRY_SECONDS + 10
        self.assertIsNone(self.sub_stop(parent, tid(2), env=env, platform="linux", now=later, runner=ssh))
        self.assertEqual(len(calls), 2)  # after the wait the host is asked again

    def test_a_subagent_of_a_botfleet_chat_takes_no_letter(self) -> None:
        parent = self.add(1, originator="botfleet")
        child = self.add_child(2, parent, originator="botfleet")
        self.assertIsNone(self.sub_start(parent, child))
        self.assertEqual((self.registered(), self.renames), (0, []))

    def test_a_guardian_review_takes_no_letter(self) -> None:
        parent = self.chat()
        guardian = self.add(2, source='{"subagent":{"other":"guardian"}}', thread_source="guardian_review")
        calls = len(self.renames)
        self.assertIsNone(self.sub_start(parent, guardian))
        self.assertEqual(len(self.renames), calls)
        self.assertEqual(self.registered(), 1)

    def test_a_subagent_whose_row_is_late_still_gets_its_task_name(self) -> None:
        parent = self.chat()
        timer = threading.Timer(0.3, self.add_child, args=(2, parent))
        timer.start()
        self.addCleanup(timer.cancel)
        self.sub_start(parent, tid(2))
        self.assertEqual(self.stored_name(tid(2)), "%s: Pong" % self.note(tid(2))["label"])

    def test_a_resumed_subagent_thread_is_a_sub_thread_of_its_parent(self) -> None:
        parent = self.chat()
        child = self.add_child(2, parent, name="Pong Review")
        self.start(child, source="resume")
        self.assertEqual(self.note(child)["parent"], parent)
        self.assertEqual(self.stored_name(child), "%s: Pong Review" % self.note(child)["label"])

    def test_events_inside_a_subagents_turn_take_no_letter(self) -> None:
        parent = self.chat()
        calls, rows = len(self.renames), self.registered()
        event = {"hook_event_name": "UserPromptSubmit", "session_id": parent, "agent_id": tid(9), "agent_type": "default",
                 "prompt": "inside"}
        self.assertIsNone(self.run_hook(event))
        self.assertEqual((len(self.renames), self.registered()), (calls, rows))


# --------------------------------------------------------------------------------------------
# Installing and trusting the hooks
# --------------------------------------------------------------------------------------------


class InstallTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.states: list[dict] | Exception = []  # what the app-server lists as hooks, or why it cannot
        self.asked = 0
        patcher = mock.patch.object(C, "hook_states", side_effect=self.hook_states)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hook_states(self, env, *, popen=None) -> list[dict]:
        self.asked += 1
        if isinstance(self.states, Exception):
            raise self.states
        return list(self.states)

    def entries(self, status: str) -> list[dict]:
        """What the app-server lists for the installed file: one entry for each of our hooks, all at `status`."""
        found = []
        for event, groups in self.hooks().items():
            for g, group in enumerate(groups):
                for h, hook in enumerate(group["hooks"]):
                    key = "%s:%s:%d:%d" % (self.hooks_path, event, g, h)
                    found.append({"key": key, "command": hook["command"], "sourcePath": self.hooks_path,
                                  "trustStatus": status})
        return found

    def install(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.install_main(["--platform", "codex", *argv], self.env(), out)
        return code, json.loads(out.getvalue())

    @property
    def hooks_path(self) -> str:
        return os.path.join(self.codex, "hooks.json")

    def hooks(self) -> dict:
        with open(self.hooks_path) as fh:
            return json.load(fh)["hooks"]

    def seed(self, doc: dict) -> None:
        Path(self.hooks_path).write_text(json.dumps(doc, indent=2))
        os.chmod(self.hooks_path, 0o600)

    def ours(self, group: dict) -> bool:
        return any(PL.installed_command(h.get("command"), "codex") for h in group["hooks"])

    def test_it_adds_the_events_and_never_session_end(self) -> None:
        code, answer = self.install("--location", "server", "--remote", "mac")
        self.assertEqual(code, 0)
        self.assertEqual(set(answer["changed"]), {"SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop"})
        hooks = self.hooks()
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop"})
        for event, timeout in C.HOOK_TIMEOUTS.items():
            (entry,) = hooks[event]
            (hook,) = entry["hooks"]
            self.assertEqual((hook["type"], hook["timeout"]), ("command", timeout))
            self.assertEqual(
                hook["command"],
                "%s/.local/bin/platform-letter hook --platform codex --location server --remote mac || true" % self.home,
            )
        self.assertIn("trust", answer)
        self.assertIn("agent_sync.platform_letter_codex trust", answer["trust"])

    def test_the_mac_install_carries_no_location_or_remote(self) -> None:
        self.install()
        self.assertEqual(self.hooks()["SessionStart"][0]["hooks"][0]["command"],
                         "%s/.local/bin/platform-letter hook --platform codex || true" % self.home)

    def commands(self) -> list[str]:
        return [hook["command"] for groups in self.hooks().values() for group in groups for hook in group["hooks"]]

    def test_every_command_ends_in_or_true_and_is_still_found_and_current(self) -> None:
        self.install("--location", "server", "--remote", "mac")
        self.assertEqual(len(self.commands()), 4)
        for command in self.commands():
            self.assertTrue(command.endswith(" || true"), command)
            words = PL.installed_command(command, "codex")
            self.assertEqual((PL._flag(words, "--location"), PL._flag(words, "--remote")), ("server", "mac"))
        self.states = self.entries("trusted")
        code, answer = self.install("--check")
        self.assertEqual((code, set(answer["current"].values())), (0, {True}))
        code, answer = self.install("--location", "server", "--remote", "mac")  # a re-run changes nothing
        self.assertEqual((code, answer["changed"]), (0, []))

    def test_the_installed_command_never_hands_codex_an_exit_status(self) -> None:
        # Codex refuses a prompt when a UserPromptSubmit hook exits 2 with a message on stderr, and python3 exits 2 when
        # the script it is told to run is gone: the server's managed wrapper outlives a rollback of the release.
        wrapper = os.path.join(self.home, ".local", "bin", "platform-letter")
        os.makedirs(os.path.dirname(wrapper))
        Path(wrapper).write_text("#!/bin/sh\nexec python3 /nonexistent/platform-letter \"$@\"\n")
        os.chmod(wrapper, 0o755)
        self.install()
        command = self.hooks()["UserPromptSubmit"][0]["hooks"][0]["command"]

        def run(line: str) -> subprocess.CompletedProcess:
            return subprocess.run(["sh", "-c", line], capture_output=True, text=True, input="{}", timeout=60)

        bare = run(command.removesuffix(" || true"))
        self.assertEqual(bare.returncode, 2)  # what Codex would have been handed
        self.assertTrue(bare.stderr)
        self.assertEqual(run(command).returncode, 0)
        os.remove(wrapper)  # and with no wrapper at all (sh answers 127)
        self.assertEqual(run(command).returncode, 0)

    def test_every_entry_budgets_for_the_ssh_hop(self) -> None:
        self.assertGreaterEqual(min(C.HOOK_TIMEOUTS.values()), int(PL.REMOTE_TIMEOUT) + 2)

    def test_it_appends_after_the_owners_hooks_so_their_trust_stays_valid(self) -> None:
        mine = {"hooks": [{"type": "command", "command": "/bin/true", "timeout": 5}]}
        keep = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/guard", "timeout": 3}]}
        self.seed({"hooks": {"UserPromptSubmit": [mine, keep], "SessionStart": [mine], "PreToolUse": [keep]}})
        before = self.hooks()
        self.install()
        after = self.hooks()
        for event in ("UserPromptSubmit", "SessionStart"):
            self.assertEqual(after[event][: len(before[event])], before[event])  # same order, same positions
            self.assertEqual(len(after[event]), len(before[event]) + 1)
            self.assertTrue(self.ours(after[event][-1]))
        self.assertEqual(after["PreToolUse"], before["PreToolUse"])

    def test_it_is_idempotent_and_backs_up_only_when_it_changes_the_file(self) -> None:
        self.seed({"hooks": {"PreToolUse": []}})
        self.install()
        first = Path(self.hooks_path).read_text()
        backups = lambda: sorted(n for n in os.listdir(self.codex) if ".bak-letter-" in n)  # noqa: E731
        self.assertEqual(len(backups()), 1)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.codex, backups()[0])).st_mode), 0o600)
        code, answer = self.install()
        self.assertEqual((code, answer["changed"]), (0, []))
        self.assertEqual(Path(self.hooks_path).read_text(), first)
        self.assertEqual(len(backups()), 1)

    def test_a_new_file_is_private(self) -> None:
        self.install()
        self.assertEqual(stat.S_IMODE(os.stat(self.hooks_path).st_mode), 0o600)

    def test_check_reports_without_writing(self) -> None:
        code, answer = self.install("--check")
        self.assertEqual((code, set(answer["installed"].values())), (1, {False}))
        self.assertFalse(os.path.exists(self.hooks_path))
        self.assertEqual(self.asked, 0)  # nothing is installed, so there is no trust to ask about
        self.install()
        self.states = self.entries("trusted")
        code, answer = self.install("--check")
        self.assertEqual((code, set(answer["current"].values())), (0, {True}))
        self.assertTrue(answer["trusted"])

    def test_install_says_whether_its_hooks_are_trusted_on_every_run(self) -> None:
        code, answer = self.install()
        self.assertEqual((code, answer["changed"] != [], answer["trusted"]), (0, True, None))  # none listed yet
        self.states = self.entries("untrusted")
        code, answer = self.install()  # nothing changes, and it still says so: the hint is not for a changing run only
        self.assertEqual((code, answer["changed"], answer["trusted"]), (0, [], False))
        self.assertIn("agent_sync.platform_letter_codex trust", answer["trust"])
        self.assertEqual(set(answer["trust_status"].values()), {"untrusted"})
        self.assertEqual(len(answer["trust_status"]), 4)
        self.states = self.entries("modified")  # a changed command (a new --location) makes every entry modified
        self.assertFalse(self.install("--location", "server", "--remote", "mac")[1]["trusted"])
        self.states = self.entries("trusted")
        code, answer = self.install()
        self.assertEqual((code, answer["trusted"]), (0, True))
        self.assertNotIn("trust", answer)  # nothing left to do
        self.states = self.entries("managed")
        self.assertTrue(self.install()[1]["trusted"])

    def test_only_our_own_entries_count_for_trust(self) -> None:
        self.install()
        self.states = [
            *self.entries("trusted"),
            {"key": "k1", "command": "/bin/true", "sourcePath": self.hooks_path, "trustStatus": "untrusted"},
            {"key": "k2", "command": PL.command_for(self.home, "codex"), "sourcePath": "/plugin/hooks.json",
             "trustStatus": "untrusted"},
            {"key": "k3", "command": PL.command_for(self.home, "opencode"), "sourcePath": self.hooks_path,
             "trustStatus": "untrusted"},
        ]
        answer = self.install()[1]
        self.assertTrue(answer["trusted"])
        self.assertEqual(len(answer["trust_status"]), 4)

    def test_check_is_done_only_when_the_hooks_are_installed_and_trusted(self) -> None:
        self.install()
        before = Path(self.hooks_path).read_text()
        self.states = self.entries("untrusted")
        code, answer = self.install("--check")
        self.assertEqual((code, answer["trusted"], set(answer["installed"].values())), (1, False, {True}))
        self.assertIn("trust", answer)
        self.states = self.entries("trusted")
        self.assertEqual(self.install("--check")[0], 0)
        self.assertEqual(Path(self.hooks_path).read_text(), before)  # a check writes nothing
        self.assertEqual([n for n in os.listdir(self.codex) if ".bak-letter-" in n], [])  # nor does it copy anything

    def test_check_is_not_done_while_a_hook_command_is_out_of_date(self) -> None:
        # An install made before the commands ended in `|| true`: installed, and trusted as it stands, but not current.
        self.install()
        doc = json.loads(Path(self.hooks_path).read_text())
        for groups in doc["hooks"].values():
            for hook in groups[0]["hooks"]:
                hook["command"] = hook["command"].removesuffix(" || true")
        Path(self.hooks_path).write_text(json.dumps(doc))
        self.states = self.entries("trusted")
        code, answer = self.install("--check")
        self.assertEqual((code, answer["trusted"], set(answer["current"].values())), (1, True, {False}))
        self.assertEqual(self.install()[0], 0)  # the install brings them up to date...
        self.assertTrue(all(c.endswith(" || true") for c in self.commands()))
        self.states = self.entries("modified")  # ...and a changed command needs trusting again
        self.assertEqual(self.install("--check")[0], 1)
        self.states = self.entries("trusted")
        self.assertEqual(self.install("--check")[0], 0)

    def test_an_app_server_that_cannot_be_asked_is_not_a_trusted_install(self) -> None:
        self.install()
        for failure in (C.RpcError("the app-server did not answer in time"), OSError("no such file"), ValueError("x")):
            self.states = failure
            code, answer = self.install("--check")
            self.assertEqual((code, answer["trusted"]), (1, None), failure)
            self.assertIn("trust", answer)
            code, answer = self.install()  # an install still succeeds: the trust step is the owner's to run
            self.assertEqual((code, answer["trusted"]), (0, None))
            self.assertNotIn("trust_status", answer)

    def test_remove_and_errors_ask_the_app_server_nothing(self) -> None:
        self.install()
        self.asked = 0
        code, answer = self.install("--remove")
        self.assertEqual((code, self.asked, "trusted" in answer), (0, 0, False))
        Path(self.hooks_path).write_text("{not json")
        self.assertEqual(self.install()[0], 2)
        self.assertEqual(self.asked, 0)

    def test_remove_takes_out_only_what_it_added(self) -> None:
        keep = {"hooks": [{"type": "command", "command": "/bin/true", "timeout": 5}]}
        self.seed({"hooks": {"UserPromptSubmit": [keep], "Stop": [keep]}})
        self.install()
        code, answer = self.install("--remove")
        self.assertEqual(code, 0)
        self.assertEqual(self.hooks(), {"UserPromptSubmit": [keep], "Stop": [keep]})
        self.assertNotIn("trust", answer)
        self.assertEqual(len([n for n in os.listdir(self.codex) if ".bak-letter-" in n]), 2)

    def test_a_file_of_an_unexpected_shape_is_refused_untouched(self) -> None:
        Path(self.hooks_path).write_text("{not json")
        code, answer = self.install()
        self.assertEqual(code, 2)
        self.assertEqual(Path(self.hooks_path).read_text(), "{not json")
        self.seed({"hooks": {"SessionStart": "nope"}})
        before = Path(self.hooks_path).read_text()
        self.assertEqual(self.install()[0], 2)
        self.assertEqual(Path(self.hooks_path).read_text(), before)

    def test_the_installed_command_is_one_the_spec_loads(self) -> None:
        self.assertIs(PL.load_spec("codex"), C.SPEC)
        self.assertIs(PL.load_adapter("codex"), C)
        words = PL.installed_command(PL.command_for(self.home, "codex", "server", "mac"), "codex")
        self.assertEqual(words[1:3], ["hook", "--platform"])
        self.assertEqual(set(C.SPEC.events), set(C.HOOK_TIMEOUTS))


class HooksApp:
    """A fake `hooks/list` and `config/batchWrite`: the hook table of an app-server, with trust statuses."""

    def __init__(self, hooks: list[dict], log: list, fail: Exception | None = None):
        self.hooks, self.log, self.rpc_error = hooks, log, fail

    def __enter__(self) -> HooksApp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, method: str, params: dict) -> dict:
        self.log.append((method, params))
        if self.rpc_error is not None:
            raise self.rpc_error
        if method == "hooks/list":
            return {"data": [{"cwd": "/x", "hooks": self.hooks, "errors": [], "warnings": []}]}
        if method == "config/batchWrite":
            (edit,) = params["edits"]
            assert edit["keyPath"] == "hooks.state" and edit["mergeStrategy"] == "upsert"
            for hook in self.hooks:
                if hook["key"] in edit["value"]:
                    assert edit["value"][hook["key"]]["trusted_hash"] == hook["currentHash"]
                    hook["trustStatus"] = "trusted"
            return {"status": "ok"}
        raise AssertionError(method)


class TrustTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.path = os.path.join(self.codex, "hooks.json")
        self.log: list = []
        self.hooks: list[dict] = []
        self.rpc_error: Exception | None = None
        patcher = mock.patch.object(C, "open_rpc", side_effect=lambda env, dl, *, daemon, popen=None: HooksApp(self.hooks, self.log, self.rpc_error))
        patcher.start()
        self.addCleanup(patcher.stop)

    def hook(self, key: str, status: str, *, command: str | None = None, source: str | None = None) -> dict:
        entry = {
            "key": key,
            "eventName": "sessionStart",
            "handlerType": "command",
            "command": command or "%s/.local/bin/platform-letter hook --platform codex" % self.home,
            "sourcePath": source or self.path,
            "source": "user",
            "trustStatus": status,
            "currentHash": "sha256:" + hashlib.sha256(key.encode()).hexdigest(),
        }
        self.hooks.append(entry)
        return entry

    def trust(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        code = C.trust_main(list(argv), self.env(), out)
        return code, json.loads(out.getvalue())

    def writes(self) -> list:
        return [params for method, params in self.log if method == "config/batchWrite"]

    def test_only_this_adapters_untrusted_entries_are_written(self) -> None:
        ours = [self.hook("%s:session_start:0:0" % self.path, "untrusted"),
                self.hook("%s:user_prompt_submit:1:0" % self.path, "modified")]
        other = self.hook("%s:user_prompt_submit:0:0" % self.path, "untrusted", command="/bin/true")
        plugin = self.hook("plug@x:hooks.json:session_start:0:0", "untrusted",
                           command="/h/.local/bin/platform-letter hook --platform codex", source="/plugin/hooks.json")
        elsewhere = self.hook("%s:stop:0:0" % self.path, "untrusted",
                              command="/h/.local/bin/platform-letter hook --platform opencode")
        code, answer = self.trust()
        self.assertEqual(code, 0)
        (write,) = self.writes()
        self.assertEqual(sorted(write["edits"][0]["value"]), sorted(h["key"] for h in ours))
        self.assertEqual([h["trustStatus"] for h in ours], ["trusted", "trusted"])
        self.assertEqual((other["trustStatus"], plugin["trustStatus"], elsewhere["trustStatus"]), ("untrusted",) * 3)
        self.assertEqual(sorted(answer["written"]), sorted(h["key"] for h in ours))
        self.assertEqual(set(answer["hooks"].values()), {"trusted"})

    def test_the_config_is_backed_up_privately_before_it_is_written(self) -> None:
        config = os.path.join(self.codex, "config.toml")
        Path(config).write_text("# mine\n[features]\nhooks = true\n")
        self.hook("%s:session_start:0:0" % self.path, "untrusted")
        code, answer = self.trust()
        self.assertEqual(code, 0)
        self.assertRegex(answer["backup"], r"/config\.toml\.bak-letter-\d{8}T\d{12}Z$")
        self.assertEqual(Path(answer["backup"]).read_text(), "# mine\n[features]\nhooks = true\n")
        self.assertEqual(stat.S_IMODE(os.stat(answer["backup"]).st_mode), 0o600)
        code, answer = self.trust()  # nothing is pending now, so nothing is copied
        self.assertEqual((code, "backup" in answer), (0, False))
        self.assertEqual(len([n for n in os.listdir(self.codex) if ".bak-letter-" in n]), 1)

    def test_no_config_file_is_nothing_to_back_up_and_check_never_copies(self) -> None:
        config = os.path.join(self.codex, "config.toml")
        self.hook("%s:session_start:0:0" % self.path, "untrusted")
        code, answer = self.trust()
        self.assertEqual((code, "backup" in answer), (0, False))
        Path(config).write_text("[features]\nhooks = true\n")
        self.hook("%s:stop:0:0" % self.path, "untrusted")
        self.trust("--check")
        self.assertEqual([n for n in os.listdir(self.codex) if ".bak-letter-" in n], [])

    def test_a_backup_that_cannot_be_made_writes_nothing(self) -> None:
        Path(os.path.join(self.codex, "config.toml")).write_text("[features]\nhooks = true\n")
        self.hook("%s:session_start:0:0" % self.path, "untrusted")
        with mock.patch.object(PL, "_backup", side_effect=PermissionError("read-only folder")):
            code, answer = self.trust()
        self.assertEqual(code, 2)
        self.assertIn("backed up", answer["error"])
        self.assertEqual(self.writes(), [])

    def test_check_writes_nothing_and_says_what_is_pending(self) -> None:
        self.hook("%s:session_start:0:0" % self.path, "untrusted")
        code, answer = self.trust("--check")
        self.assertEqual((code, self.writes(), answer["written"]), (1, [], []))
        self.assertEqual(set(answer["hooks"].values()), {"untrusted"})

    def test_trusted_entries_are_not_written_again(self) -> None:
        self.hook("%s:session_start:0:0" % self.path, "trusted")
        code, answer = self.trust()
        self.assertEqual((code, self.writes(), answer["written"]), (0, [], []))
        code, _ = self.trust("--check")
        self.assertEqual(code, 0)

    def test_no_entry_of_ours_is_not_a_pass(self) -> None:
        self.hook("%s:stop:0:0" % self.path, "trusted", command="/bin/true")
        code, answer = self.trust()
        self.assertEqual((code, answer["hooks"]), (1, {}))

    def test_an_app_server_that_cannot_be_asked_is_exit_two_and_names_no_request(self) -> None:
        self.rpc_error = C.RpcError("hooks/list failed (code -32601)")
        code, answer = self.trust()
        self.assertEqual(code, 2)
        self.assertIn("RpcError", answer["error"])
        self.rpc_error = OSError("no such file")
        self.assertEqual(self.trust()[0], 2)

    def test_bad_arguments_are_exit_two(self) -> None:
        self.assertEqual(self.trust("--now")[0], 2)
        self.assertEqual(self.log, [])

    def test_main_wires_trust_and_prints_usage_otherwise(self) -> None:
        self.hook("%s:session_start:0:0" % self.path, "untrusted")
        out = io.StringIO()
        self.assertEqual(C.main(["trust", "--check"], env=self.env(), stdout=out), 1)
        self.assertEqual(json.loads(out.getvalue())["platform"], "codex")
        out = io.StringIO()
        self.assertEqual(C.main([], env=self.env(), stdout=out), 2)
        self.assertIn("usage", out.getvalue())


class SpecTests(unittest.TestCase):
    def test_the_spec_is_codex_under_its_own_seat_and_namespace(self) -> None:
        self.assertEqual((C.SPEC.platform, C.SPEC.seat, C.SPEC.source), ("codex", "CODEX", "codex-letter-hook"))
        self.assertEqual(C.SPEC.launchers, ())  # a launcher's session never takes the Codex letter
        self.assertIs(C.SPEC.title_writer, C.codex_rename)
        self.assertFalse(C.SPEC.title_in_output)  # Codex hooks cannot set a title; the app-server does

    def test_the_events_are_the_ones_the_installer_hooks(self) -> None:
        self.assertEqual(C.SPEC.events["SubagentStart"], "start")
        # SubagentStop fires after every turn of a sub-agent, so it is a sign of life and never an end.
        self.assertEqual(C.SPEC.events["SubagentStop"], "prompt")
        self.assertNotIn("end", C.SPEC.events.values())
        self.assertNotIn("SessionEnd", C.SPEC.events)  # every Codex SessionEnd says "other"; a letter is not freed by it
        self.assertEqual(C.SPEC.quiet_events, ("SubagentStop",))  # its output schema has no hookSpecificOutput

    def test_the_engine_drivers_are_skipped_by_name_and_everything_foreign_by_rule(self) -> None:
        self.assertEqual(C.SKIP_ORIGINATORS, ("botfleet", "minimax-codex-mcp", "codex_exec", "codex_sdk_ts"))
        self.assertEqual(C.DRIVER_ORIGINATORS, ("codex_exec", "codex_sdk_ts"))

    def test_the_spec_can_look_a_parent_up(self) -> None:
        self.assertEqual(
            C.SPEC.chat_payload("p-0001"),
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": "p-0001"},
        )

    def test_the_library_finds_the_adapter_and_shows_a_chat_with_no_letter(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            out = io.StringIO()
            code = PL.main(["show", "--platform", "codex", "--session-id", "nope-0001"],
                           env={"HOME": state, "AGENT_SYNC_STATE_DIR": state}, stdout=out)
        self.assertEqual((code, json.loads(out.getvalue())["letter"]), (1, None))


if __name__ == "__main__":
    unittest.main()
