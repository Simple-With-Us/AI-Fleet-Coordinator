"""Codex server transport tests use an injected daemon and never start a real app-server."""

import os
import tempfile
import unittest
from unittest.mock import patch

from simple_tools import codex_server_transport as CST
from simple_tools import platform_letter_codex as PLC


class FakeRpc:
    """One daemon conversation:  it answers `thread/read` with the status it was given."""

    def __init__(self, status, error=None):
        self.status = status
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def call(self, method, params):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        if method != "thread/read":
            return {}
        return {"thread": {"id": params.get("threadId"), "status": {"type": self.status}}}


def opener(status=None, error=None):
    """A daemon opener that answers with one status, and remembers every conversation."""
    made = []

    def open_rpc(host_env, deadline, *, daemon=True):
        made.append({"env": dict(host_env), "deadline": deadline, "daemon": daemon})
        return FakeRpc(status, error)

    open_rpc.made = made
    return open_rpc


class DaemonStatusTests(unittest.TestCase):
    def test_the_daemon_says_which_thread_is_loaded(self):
        open_rpc = opener("idle")
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            status = CST.daemon_status({"HOME": "/home/test"}, "thread-1", opener=open_rpc)
        self.assertEqual(status, "idle")
        self.assertEqual(len(open_rpc.made), 1)
        self.assertTrue(open_rpc.made[0]["daemon"])

    def test_a_host_with_no_daemon_answers_nothing_and_asks_nothing(self):
        open_rpc = opener("idle")
        with patch.object(PLC, "daemon_socket", return_value=""):
            status = CST.daemon_status({"HOME": "/home/test"}, "thread-1", opener=open_rpc)
        self.assertEqual(status, "")
        self.assertEqual(open_rpc.made, [])

    def test_an_unreadable_daemon_answers_nothing(self):
        open_rpc = opener(None, error=RuntimeError("the daemon is down"))
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            status = CST.daemon_status({"HOME": "/home/test"}, "thread-1", opener=open_rpc)
        self.assertEqual(status, "")

    def test_an_answer_that_names_no_status_answers_nothing(self):
        class Nameless(FakeRpc):
            def call(self, method, params):
                self.calls.append((method, params))
                return {"thread": {"id": params.get("threadId")}}

        def open_rpc(host_env, deadline, *, daemon=True):
            return Nameless(None)

        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            status = CST.daemon_status({"HOME": "/home/test"}, "thread-1", opener=open_rpc)
        self.assertEqual(status, "")

    def test_one_answer_serves_the_next_caller_and_a_fresh_probe_asks_again(self):
        open_rpc = opener("idle")
        probe = CST.DaemonStatus({"HOME": "/home/test"}, opener=open_rpc)
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            self.assertTrue(probe.loaded("thread-1"))
            self.assertTrue(probe.loaded("thread-1"))
            self.assertEqual(len(open_rpc.made), 1)
            self.assertTrue(probe.loaded("thread-1", fresh=True))
            self.assertEqual(len(open_rpc.made), 2)

    def test_a_dead_thread_is_not_loaded(self):
        open_rpc = opener("notLoaded")
        probe = CST.DaemonStatus({"HOME": "/home/test"}, opener=open_rpc)
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            self.assertFalse(probe.loaded("thread-1"))
            self.assertFalse(probe.loaded("thread-1"))
            self.assertEqual(len(open_rpc.made), 1)

    def test_the_cache_holds_only_so_many_threads(self):
        open_rpc = opener("idle")
        probe = CST.DaemonStatus({"HOME": "/home/test"}, opener=open_rpc, max_entries=3)
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            for number in range(5):
                self.assertTrue(probe.loaded("thread-%d" % number))
            self.assertEqual(len(probe._cache), 3)
            self.assertEqual(len(open_rpc.made), 5)
            # The oldest answers went first:  asking for one again costs a daemon
            # conversation, while a kept answer still serves from the cache.
            self.assertTrue(probe.loaded("thread-0"))
            self.assertEqual(len(open_rpc.made), 6)
            self.assertTrue(probe.loaded("thread-4"))
            self.assertEqual(len(open_rpc.made), 6)

    def test_an_expired_answer_is_dropped_when_the_next_one_is_stored(self):
        open_rpc = opener("idle")
        probe = CST.DaemonStatus({"HOME": "/home/test"}, opener=open_rpc)
        probe._cache["thread-old"] = (0.0, "idle")
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            self.assertTrue(probe.loaded("thread-1"))
        self.assertNotIn("thread-old", probe._cache)
        self.assertIn("thread-1", probe._cache)

    def test_a_failed_probe_is_cached_as_no_answer(self):
        open_rpc = opener(None, error=RuntimeError("the daemon is down"))
        probe = CST.DaemonStatus({"HOME": "/home/test"}, opener=open_rpc)
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            self.assertFalse(probe.loaded("thread-1"))
            self.assertFalse(probe.loaded("thread-1"))
            self.assertEqual(len(open_rpc.made), 1)


class ServerTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host_env = {"HOME": self.tmp.name, "PATH": "/usr/bin:/bin"}

    def test_the_transport_reads_the_profile_it_owns_and_asks_the_daemon(self):
        probe = CST.DaemonStatus(self.host_env, opener=opener("idle"))
        with patch.object(PLC, "codex_binary", return_value="/usr/local/bin/codex"):
            transport = CST.CodexServerChatTransport(env=self.host_env, status=probe)
        self.assertEqual(transport.binary, "/usr/local/bin/codex")
        self.assertEqual(str(transport.home), self.tmp.name)
        # The one-shot app-server of our own reads the profile this seat owns.
        with patch.object(CST.CT, "RPC") as rpc:
            transport.rpc_factory()
        self.assertEqual(
            rpc.call_args.kwargs["env"]["CODEX_HOME"], os.path.join(self.tmp.name, ".codex")
        )
        self.assertEqual(rpc.call_args.args[0], "/usr/local/bin/codex")

    def test_a_thread_the_daemon_calls_loaded_is_loaded_and_a_dead_one_is_not(self):
        probe = CST.DaemonStatus(self.host_env, opener=opener("idle"))
        with patch.object(PLC, "codex_binary", return_value="/usr/local/bin/codex"):
            transport = CST.CodexServerChatTransport(env=self.host_env, status=probe)
        with patch.object(PLC, "daemon_socket", return_value="/tmp/app-server-control.sock"):
            self.assertTrue(transport._is_loaded({"id": "thread-1"}))
        self.assertFalse(transport._is_loaded({}))
        self.assertFalse(transport._is_loaded({"id": ""}))

    def test_a_host_with_no_codex_program_has_a_transport_that_cannot_run(self):
        with patch.object(PLC, "codex_binary", return_value=""):
            transport = CST.CodexServerChatTransport(
                env=self.host_env, status=CST.DaemonStatus(self.host_env)
            )
        self.assertEqual(transport.binary, "")
        self.assertFalse(transport._prerequisites())
