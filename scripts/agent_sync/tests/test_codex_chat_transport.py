"""Fake RPC tests; no real thread sends or model execution."""

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from agent_sync import codex_chat_transport as module
from agent_sync.codex_chat_transport import RPC, CodexChatTransport, message_id

SID = "11111111-1111-4111-8111-111111111111"


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / ".codex/sessions/rollout.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text("")
        (self.home / ".codex/queue_1.sqlite").touch()
        self.binary = self.home / "codex"
        self.binary.touch()
        self.binary.chmod(0o700)
        self.binding = {
            "platform": "codex",
            "host": socket.gethostname(),
            "cwd": str(self.home),
            "session_id": SID,
        }
        self.thread = {
            "id": SID,
            "cwd": str(self.home),
            "path": str(self.path),
            "status": {"type": "notLoaded"},
        }
        self.calls = []
        self.queued = []
        self.fail = None
        self.ack = None
        outer = self

        class FakeRPC:
            def request(self, method, params, **kwargs):
                outer.calls.append((method, params))
                if method == outer.fail:
                    raise TimeoutError("private error")
                if method == "thread/read":
                    return {"thread": outer.thread}
                if method == "thread/queue/list":
                    limit = params.get("limit")
                    start = int(params.get("cursor", "0"))
                    end = start + limit
                    return {
                        "data": outer.queued[start:end],
                        "nextCursor": str(end) if end < len(outer.queued) else None,
                    }
                if method == "thread/queue/add":
                    return (
                        outer.ack
                        if outer.ack is not None
                        else {
                            "queuedSubmission": {
                                "id": "queue-id",
                                "clientUserMessageId": params["clientUserMessageId"],
                            }
                        }
                    )
                raise AssertionError(method)

            def close(self):
                pass

        self.adapter = CodexChatTransport(
            home=self.home, binary=str(self.binary), rpc_factory=FakeRPC, loaded=lambda row: True
        )

    def test_send_exact_input_stable_id_and_permit(self):
        self.assertEqual(self.adapter.send(self.binding, "nudge", permit=lambda: True), "accepted")
        method, params = self.calls[-1]
        self.assertEqual(method, "thread/queue/add")
        self.assertEqual(
            params,
            {
                "threadId": SID,
                "input": [{"type": "text", "text": "nudge", "textElements": []}],
                "clientUserMessageId": message_id(self.binding, "nudge"),
            },
        )
        self.assertEqual(message_id(self.binding, "nudge"), message_id(self.binding, "nudge"))
        self.calls.clear()
        self.assertEqual(self.adapter.send(self.binding, "nudge", permit=lambda: False), "deferred")
        self.assertNotIn("thread/queue/add", [x[0] for x in self.calls])
        self.assertTrue(all(x[0] in ("thread/read", "thread/queue/list") for x in self.calls))

    def test_ambiguous_or_mismatched_ack_is_unknown(self):
        for ack in (
            {},
            {"queuedSubmission": {}},
            {"queuedSubmission": {"id": "q", "clientUserMessageId": "wrong"}},
        ):
            self.ack = ack
            self.assertEqual(self.adapter.send(self.binding, "nudge"), "unknown")

    def test_same_client_id_with_wrong_text_is_not_receipt(self):
        self.queued = [
            {
                "clientUserMessageId": message_id(self.binding, "nudge"),
                "input": [{"type": "text", "text": "different", "textElements": []}],
            }
        ]
        self.assertFalse(self.adapter.receipt(self.binding, "nudge"))

    def test_queue_receipt_accepts_real_snake_case_text_elements_only(self):
        self.queued = [
            {
                "clientUserMessageId": message_id(self.binding, "nudge"),
                "input": [{"type": "text", "text": "nudge", "text_elements": []}],
            }
        ]
        self.assertTrue(self.adapter.receipt(self.binding, "nudge"))
        for input in (
            [{"type": "text", "text": "nudge", "text_elements": [{"start": 0}]}],
            [{"type": "text", "text": "nudge"}, {"type": "image", "url": "x"}],
            [{"type": "text", "text": "nudge", "attachment": "x"}],
        ):
            self.queued[0]["input"] = input
            self.adapter._receipts.clear()
            self.assertFalse(self.adapter.receipt(self.binding, "nudge"))

    def test_invalid_bindings_never_open_rpc(self):
        for changed in (
            {"host": "remote"},
            {"cwd": "relative/workspace"},
            {"session_id": "not-uuid"},
            {"platform": "other"},
        ):
            self.assertEqual(self.adapter.preflight(dict(self.binding, **changed)), "invalid")
        self.assertEqual(self.calls, [])

    def test_missing_workspace_stays_busy_not_invalid(self):
        # An unmounted workspace is recoverable; a terminal hold would drop the wake forever.
        self.assertEqual(self.adapter.preflight(dict(self.binding, cwd="/not-present")), "busy")
        self.assertEqual(self.calls, [])

    def test_send_rechecks_workspace_availability(self):
        # The workspace can unmount between preflight and send, and send() is callable directly.
        self.assertEqual(
            self.adapter.send(dict(self.binding, cwd="/not-present"), "nudge"), "deferred"
        )
        self.assertEqual(self.calls, [])
        # A relative path never reaches the directory check and stays deferred rather than held.
        self.assertEqual(
            self.adapter.send(dict(self.binding, cwd="relative/workspace"), "nudge"), "deferred"
        )
        self.assertEqual(self.calls, [])

    def test_archived_unreadable_unknown_or_unloaded_refused(self):
        for key, value in [
            ("archived", True),
            ("readOnly", True),
            ("id", "wrong"),
            ("cwd", "/wrong"),
            ("path", str(self.path.parent / "missing")),
        ]:
            original = dict(self.thread)
            self.thread[key] = value
            expected = "busy" if key == "path" else "invalid"
            self.assertEqual(self.adapter.preflight(self.binding), expected)
            self.thread = original
        self.adapter.loaded = lambda _: False
        self.assertEqual(self.adapter.preflight(self.binding), "busy")

    def test_missing_local_prerequisites_stay_busy_not_unsupported(self):
        # Recoverable local state must never strand a queued nudge on a permanent hold.
        self.binary.chmod(0o600)
        self.assertEqual(self.adapter.preflight(self.binding), "busy")
        self.assertEqual(self.calls, [])
        self.binary.chmod(0o700)
        (self.home / ".codex/queue_1.sqlite").unlink()
        self.assertEqual(self.adapter.preflight(self.binding), "busy")
        self.assertEqual(self.calls, [])
        (self.home / ".codex/queue_1.sqlite").touch()
        self.assertEqual(self.adapter.preflight(self.binding), "ready")

    def test_queue_short_pages_complete_with_one_shared_deadline(self):
        calls = []
        class SmallPages:
            def request(self, method, params, *, timeout):
                calls.append(timeout)
                index = int(params.get("cursor", "0"))
                return {"data": [{"id": str(index)}],
                        "nextCursor": str(index + 1) if index < 11 else None}
        with patch.object(module.time, "monotonic", side_effect=[0] + [i / 10 for i in range(12)]):
            self.assertEqual(len(self.adapter._queue(SmallPages(), self.binding)), 12)
        self.assertEqual(len(calls), 12)
        self.assertGreater(calls[0], calls[-1])
        with patch.object(module.time, "monotonic", side_effect=[0, 5]):
            with self.assertRaisesRegex(TimeoutError, "pagination deadline exceeded"):
                self.adapter._queue(SmallPages(), self.binding)

    def test_full_native_queue_is_busy_before_send(self):
        self.queued = [{"clientUserMessageId": str(i)} for i in range(module.QUEUE_CAPACITY)]
        self.assertEqual(self.adapter.preflight(self.binding), "busy")
        # The probe must count the whole queue; a partial page would under-report a full one.
        self.assertEqual(
            [p["limit"] for m, p in self.calls if m == "thread/queue/list"],
            [module.QUEUE_PAGE_SIZE] * (module.QUEUE_CAPACITY // module.QUEUE_PAGE_SIZE),
        )
        self.calls.clear()
        self.assertEqual(self.adapter.send(self.binding, "nudge"), "deferred")
        self.assertNotIn("thread/queue/add", [x[0] for x in self.calls])
        self.queued = self.queued[:-1]
        self.adapter._full_queues.clear()  # A drained queue becomes visible after its brief busy TTL.
        self.assertEqual(self.adapter.preflight(self.binding), "ready")

    def test_receipt_reads_the_same_queue_window_as_preflight(self):
        # The queue is shared storage, so a narrower receipt window would hide the client's
        # submission from reconcile and strand the item as unknown.
        self.assertFalse(self.adapter.receipt(self.binding, "nudge"))
        self.assertEqual(
            [p["limit"] for m, p in self.calls if m == "thread/queue/list"],
            [module.QUEUE_PAGE_SIZE],
        )

    def test_timeout_before_send_deferred_after_add_unknown(self):
        self.fail = "thread/read"
        self.assertEqual(self.adapter.send(self.binding, "nudge"), "deferred")
        self.fail = "thread/queue/add"
        self.assertEqual(self.adapter.send(self.binding, "nudge"), "unknown")
        self.assertEqual(sum(method == "thread/queue/add" for method, _ in self.calls), 1)

    def test_pre_enqueue_failure_is_retryable_not_stranded(self):
        # A failure before thread/queue/add enqueued nothing, so the wake stays retryable
        # instead of picking a permanent hold_reason in chat_nudges.
        outer = self
        state = {"reads": 0}

        class FailingRPC:
            def request(self, method, params, **kwargs):
                outer.calls.append((method, params))
                if method == "thread/read":
                    state["reads"] += 1
                    if state["reads"] == 1:
                        raise OSError("app-server vanished")
                    return {"thread": outer.thread}
                return {"data": []}

            def close(self):
                pass

        self.adapter.rpc_factory = FailingRPC
        self.assertEqual(self.adapter.send(self.binding, "nudge"), "deferred")
        self.assertNotIn("thread/queue/add", [m for m, _ in self.calls])

    def test_single_send_session_and_no_cached_ready_or_permit(self):
        original = self.adapter.rpc_factory
        with patch.object(self.adapter, "rpc_factory", wraps=original) as factory:
            self.assertEqual(
                self.adapter.send(self.binding, "nudge", permit=lambda: True), "accepted"
            )
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(sum(m == "thread/read" for m, _ in self.calls), 1)
            self.assertEqual(
                self.adapter.send(self.binding, "nudge", permit=lambda: False), "deferred"
            )
            self.assertEqual(factory.call_count, 2)

    def test_terminal_history_scope_vs_retryable_missing_and_unloaded(self):
        self.thread["path"] = "/outside/sessions/rollout.jsonl"
        self.assertEqual(self.adapter.preflight(self.binding), "invalid")
        self.thread["path"] = str(self.path)
        self.path.unlink()
        self.assertEqual(self.adapter.preflight(self.binding), "busy")
        self.path.touch()
        self.adapter.loaded = lambda _: False
        self.assertEqual(self.adapter.preflight(self.binding), "busy")
        self.adapter.loaded = lambda _: True
        self.assertEqual(self.adapter.preflight(self.binding), "ready")

    def test_full_queue_verdict_cached_per_exact_binding_and_expires(self):
        self.queued = [{} for _ in range(module.QUEUE_CAPACITY)]
        with patch.object(module.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.adapter.preflight(self.binding), "busy")
            count = len(self.calls)
            for _ in range(20):
                self.assertEqual(self.adapter.preflight(self.binding), "busy")
            self.assertEqual(len(self.calls), count)
            self.queued = []
            clock.return_value = 106
            self.assertEqual(self.adapter.preflight(self.binding), "ready")

    def test_capacity_rejection_deferred_but_other_errors_uncertain(self):
        original = self.adapter.rpc_factory

        def factory():
            rpc = original()
            request = rpc.request

            def request_with_rejection(method, params, **kwargs):
                if method == "thread/queue/add":
                    raise module.RPCError(
                        {
                            "code": -32600,
                            "message": "queue cannot contain more than 100 submissions",
                        }
                    )
                return request(method, params)

            rpc.request = request_with_rejection
            return rpc

        self.adapter.rpc_factory = factory
        self.assertEqual(self.adapter.send(self.binding, "nudge"), "deferred")
        self.assertTrue(self.adapter._full(self.binding))
        self.assertNotIn(
            "queue cannot", str(module.RPCError({"code": -32600, "message": "private"}))
        )

    def test_wrong_or_boolean_capacity_error_code_remains_unknown_after_add(self):
        original = self.adapter.rpc_factory
        for code in (-32603, True, "-32600", None):

            def factory():
                rpc = original()
                request = rpc.request

                def reject(method, params):
                    if method == "thread/queue/add":
                        raise module.RPCError(
                            {
                                "code": code,
                                "message": "queue cannot contain more than 100 submissions",
                            }
                        )
                    return request(method, params)

                rpc.request = reject
                return rpc

            self.adapter.rpc_factory = factory
            self.assertEqual(self.adapter.send(self.binding, "nudge"), "unknown")
            self.assertFalse(self.adapter._full(self.binding))

    def test_receipt_snapshot_shared_for_different_items_then_refreshes(self):
        self.queued = [
            {
                "clientUserMessageId": message_id(self.binding, "one"),
                "input": [{"type": "text", "text": "one", "text_elements": []}],
            }
        ]
        original = self.adapter.rpc_factory
        with (
            patch.object(module.time, "monotonic", return_value=100) as clock,
            patch.object(self.adapter, "rpc_factory", wraps=original) as factory,
        ):
            self.assertTrue(self.adapter.receipt(self.binding, "one"))
            self.assertFalse(self.adapter.receipt(self.binding, "two"))
            self.assertEqual(factory.call_count, 1)
            self.queued = [
                {
                    "clientUserMessageId": message_id(self.binding, "two"),
                    "input": [{"type": "text", "text": "two", "textElements": []}],
                }
            ]
            clock.return_value = 103
            self.assertTrue(self.adapter.receipt(self.binding, "two"))
            self.assertFalse(self.adapter.receipt(self.binding, "one"))
            self.assertEqual(factory.call_count, 2)

    def test_receipt_finds_last_page_and_bounds_requests(self):
        self.queued = [{} for _ in range(module.QUEUE_CAPACITY - 1)] + [
            {
                "clientUserMessageId": message_id(self.binding, "nudge"),
                "input": [{"type": "text", "text": "nudge", "text_elements": []}],
            }
        ]
        self.assertTrue(self.adapter.receipt(self.binding, "nudge"))
        pages = [p for m, p in self.calls if m == "thread/queue/list"]
        self.assertEqual(len(pages), module.QUEUE_CAPACITY // module.QUEUE_PAGE_SIZE)
        self.assertEqual(pages[-1]["cursor"], "90")
        self.assertTrue(all(p["limit"] == module.QUEUE_PAGE_SIZE for p in pages))

    def test_incomplete_cyclic_pagination_is_retryable_and_bounded(self):
        original = self.adapter.rpc_factory

        def factory():
            rpc = original()
            request = rpc.request
            rpc.request = lambda method, params, **kwargs: (
                {"data": [{}], "nextCursor": "repeated"}
                if method == "thread/queue/list"
                else request(method, params)
            )
            return rpc

        self.adapter.rpc_factory = factory
        self.assertEqual(self.adapter.preflight(self.binding), "busy")
        self.assertFalse(self.adapter.receipt(self.binding, "nudge"))
        self.assertEqual(self.adapter._receipts, {})

    def test_failed_receipt_does_not_cache_false_and_hide_new_evidence(self):
        self.fail = "thread/read"
        self.assertFalse(self.adapter.receipt(self.binding, "nudge"))
        self.assertEqual(self.adapter._receipts, {})
        self.fail = None
        self.queued = [
            {
                "clientUserMessageId": message_id(self.binding, "nudge"),
                "input": [{"type": "text", "text": "nudge", "text_elements": []}],
            }
        ]
        self.assertTrue(self.adapter.receipt(self.binding, "nudge"))

    def test_rpc_preserves_structured_capacity_rejection_without_server_message(self):
        script = self.home / "fake-codex"
        script.write_text(
            '#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n if "id" in r:\n  result={"id":r["id"],"result":{}} if r["method"]=="initialize" else {"id":r["id"],"error":{"code":-32600,"message":"queue cannot contain more than 100 submissions"}}\n  print(json.dumps(result),flush=True)\n'
        )
        script.chmod(0o700)
        rpc = RPC(str(script))
        try:
            with self.assertRaises(module.RPCError) as caught:
                rpc.request("thread/queue/add", {})
            self.assertTrue(caught.exception.queue_full)
            self.assertEqual(caught.exception.code, -32600)
            self.assertNotIn("queue cannot", str(caught.exception))
        finally:
            rpc.close()

    def test_receipt_cache_retains_only_compact_evidence_and_expires_on_insert(self):
        text = "sensitive message body " * 1000
        self.queued = [
            {
                "clientUserMessageId": message_id(self.binding, text),
                "input": [{"type": "text", "text": text, "text_elements": []}],
            }
        ]
        with patch.object(module.time, "monotonic", return_value=100) as clock:
            self.assertTrue(self.adapter.receipt(self.binding, text))
            snapshot = self.adapter._receipts[self.adapter._key(self.binding)]
            self.assertEqual(len(snapshot[1]), 1)
            self.assertNotIn(text, repr(snapshot))
            self.assertTrue(
                all(isinstance(digest, bytes) and len(digest) == 32 for _, digest in snapshot[1])
            )
            self.adapter._receipts["expired"] = (99, set(), set())
            self.adapter._receipts.clear()  # Force one refreshed evidence snapshot.
            self.adapter._receipts["expired"] = (99, set(), set())
            clock.return_value = 103
            self.assertTrue(self.adapter.receipt(self.binding, text))
            self.assertNotIn("expired", self.adapter._receipts)

    def test_rpc_close_swallows_teardown_errors(self):
        # Cleanup runs from callers' finally blocks; a raise there would replace an
        # acknowledged outcome, so every teardown step is guarded.
        script = self.home / "fake-codex"
        script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        script.chmod(0o700)
        with patch.object(RPC, "request", autospec=True, return_value={}):
            rpc = RPC(str(script))
        broken = mock.Mock()
        broken.wait.side_effect = subprocess.TimeoutExpired("codex", 1)
        broken.kill.side_effect = subprocess.TimeoutExpired("codex", 1)
        broken.terminate.side_effect = ProcessLookupError("gone")
        broken.stdin.close.side_effect = OSError("closed")
        broken.stdout.close.side_effect = OSError("closed")
        real = rpc.proc
        rpc.proc = broken
        try:
            rpc.close()
        finally:
            real.kill()
            real.wait(timeout=5)
            real.stdin.close()
            real.stdout.close()
        rpc.proc.kill.assert_called()

    def test_receipt_queue_client_id_or_exact_user_text_only(self):
        self.queued = [
            {
                "clientUserMessageId": message_id(self.binding, "nudge"),
                "input": [{"type": "text", "text": "nudge", "textElements": []}],
            }
        ]
        self.adapter._receipts.clear()
        self.assertTrue(self.adapter.receipt(self.binding, "nudge"))
        self.queued = []
        self.path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "input_text", "text": "nudge"}],
                    },
                }
            )
            + "\n"
        )
        self.adapter._receipts.clear()
        self.adapter._receipts.clear()
        self.assertFalse(self.adapter.receipt(self.binding, "nudge"))
        self.path.write_text(
            json.dumps(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "nudge"}}
            )
            + "\n"
        )
        self.adapter._receipts.clear()
        self.assertTrue(self.adapter.receipt(self.binding, "nudge"))

    def test_rpc_process_argv_has_no_resume_start_or_model_override(self):
        # A real local fake protocol subprocess exercises initialize, malformed JSON and cleanup.
        script = self.home / "fake-codex"
        script.write_text(
            '#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n if "id" in r:\n  print(json.dumps({"id":r["id"],"result":{}}),flush=True)\n'
        )
        script.chmod(0o700)
        rpc = RPC(str(script))
        try:
            self.assertEqual(rpc.proc.args, [str(script), "app-server", "--listen", "stdio://"])
            self.assertEqual(rpc.request("thread/queue/list", {"threadId": SID}), {})
        finally:
            rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)

    def test_rpc_blocked_writer_obeys_overall_deadline_and_closes(self):
        script = self.home / "fake-codex"
        script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        script.chmod(0o700)
        with patch.object(RPC, "request", autospec=True, return_value={}):
            rpc = RPC(str(script))
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                rpc.request("thread/queue/add", {"text": "x" * 1000000}, timeout=0.1)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)
        self.assertLess(time.monotonic() - started, 3)

    def test_rpc_short_writes_preserve_whole_request(self):
        script = self.home / "fake-codex"
        script.write_text(
            '#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n if "id" in r: print(json.dumps({"id":r["id"],"result":{"method":r["method"]}}),flush=True)\n'
        )
        script.chmod(0o700)
        original = os.write
        with patch.object(
            module.os, "write", side_effect=lambda fd, data: original(fd, data[:7])
        ) as write:
            rpc = RPC(str(script))
            try:
                self.assertEqual(
                    rpc.request("thread/queue/list", {"threadId": SID}),
                    {"method": "thread/queue/list"},
                )
                self.assertGreater(write.call_count, 20)
            finally:
                rpc.close()

    def test_rpc_malformed_response_and_timeout(self):
        for body, error in [
            ('print("not-json",flush=True)', ValueError),
            ("import time;time.sleep(2)", TimeoutError),
        ]:
            script = self.home / "fake-codex"
            script.write_text(
                "#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n"
                " if r.get('method')=='initialize': print(json.dumps({'id':r['id'],'result':{}}),flush=True)\n"
                " elif 'id' in r: " + body + "\n"
            )
            script.chmod(0o700)
            rpc = RPC(
                str(script)
            )  # Real handshake proves the child is ready before timing its response.
            try:
                with self.assertRaises(error):
                    rpc.request("thread/read", {}, timeout=2 if error is ValueError else 0.1)
            finally:
                rpc.close()


class LoadedProofTests(unittest.TestCase):
    def test_exact_desktop_executable_and_rollout_must_share_process(self):
        path = "/Users/x/.codex/sessions/rollout.jsonl"
        output = ("p1\nn" + module.DESKTOP_EXECUTABLE + "\np2\nn" + path + "\n").encode()
        with (
            patch.object(module, "_LOADED_CACHE", (0, frozenset())),
            patch.object(
                module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)
            ),
        ):
            self.assertEqual(module.loaded_rollouts(), frozenset())
        output = ("p1\nn" + module.DESKTOP_EXECUTABLE + "\nn" + path + "\n").encode()
        with (
            patch.object(module, "_LOADED_CACHE", (0, frozenset())),
            patch.object(
                module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)
            ) as run,
        ):
            self.assertEqual(module.loaded_rollouts(), frozenset([path]))
            self.assertEqual(module.loaded_rollouts(), frozenset([path]))
            self.assertEqual(run.call_count, 1)

    def test_timeout_fails_closed(self):
        with (
            patch.object(module, "_LOADED_CACHE", (0, frozenset())),
            patch.object(
                module.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 3)
            ),
        ):
            self.assertEqual(module.loaded_rollouts(), frozenset())

    def test_rpc_explicit_codex_home(self):
        with tempfile.TemporaryDirectory() as home, patch.object(module, "RPC") as rpc:
            CodexChatTransport(home=home).rpc_factory()
            self.assertEqual(rpc.call_args.kwargs["env"]["CODEX_HOME"], str(Path(home) / ".codex"))
