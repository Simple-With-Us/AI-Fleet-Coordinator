"""Fake RPC tests; no real thread sends or model execution."""
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_sync import codex_chat_transport as module
from agent_sync.codex_chat_transport import CodexChatTransport, RPC, message_id

SID = '11111111-1111-4111-8111-111111111111'


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home/'.codex/sessions/rollout.jsonl'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('')
        (self.home/'.codex/queue_1.sqlite').touch()
        self.binary = self.home/'codex'
        self.binary.touch()
        self.binary.chmod(0o700)
        self.binding = {'platform': 'codex', 'host': socket.gethostname(), 'cwd': str(self.home), 'session_id': SID}
        self.thread = {'id': SID, 'cwd': str(self.home), 'path': str(self.path), 'status': {'type': 'notLoaded'}}
        self.calls = []
        self.queued = []
        self.fail = None
        self.ack = None
        outer = self
        class FakeRPC:
            def request(self, method, params):
                outer.calls.append((method, params))
                if method == outer.fail:
                    raise TimeoutError('private error')
                if method == 'thread/read':
                    return {'thread': outer.thread}
                if method == 'thread/queue/list':
                    return {'data': outer.queued}
                if method == 'thread/queue/add':
                    return outer.ack if outer.ack is not None else {'queuedSubmission': {'id': 'queue-id', 'clientUserMessageId': params['clientUserMessageId']}}
                raise AssertionError(method)
            def close(self):
                pass
        self.adapter = CodexChatTransport(home=self.home, binary=str(self.binary), rpc_factory=FakeRPC, loaded=lambda row: True)

    def test_send_exact_input_stable_id_and_permit(self):
        self.assertEqual(self.adapter.send(self.binding, 'nudge', permit=lambda: True), 'accepted')
        method, params = self.calls[-1]
        self.assertEqual(method, 'thread/queue/add')
        self.assertEqual(params, {'threadId': SID, 'input': [{'type': 'text', 'text': 'nudge', 'textElements': []}], 'clientUserMessageId': message_id(self.binding, 'nudge')})
        self.assertEqual(message_id(self.binding, 'nudge'), message_id(self.binding, 'nudge'))
        self.calls.clear()
        self.assertEqual(self.adapter.send(self.binding, 'nudge', permit=lambda: False), 'deferred')
        self.assertNotIn('thread/queue/add', [x[0] for x in self.calls])
        self.assertTrue(all(x[0] in ('thread/read', 'thread/queue/list') for x in self.calls))

    def test_ambiguous_or_mismatched_ack_is_unknown(self):
        for ack in ({}, {'queuedSubmission': {}}, {'queuedSubmission': {'id': 'q', 'clientUserMessageId': 'wrong'}}):
            self.ack = ack
            self.assertEqual(self.adapter.send(self.binding, 'nudge'), 'unknown')

    def test_same_client_id_with_wrong_text_is_not_receipt(self):
        self.queued = [{'clientUserMessageId': message_id(self.binding, 'nudge'),
                        'input': [{'type': 'text', 'text': 'different', 'textElements': []}]}]
        self.assertFalse(self.adapter.receipt(self.binding, 'nudge'))

    def test_queue_receipt_accepts_real_snake_case_text_elements_only(self):
        self.queued = [{'clientUserMessageId': message_id(self.binding, 'nudge'),
                        'input': [{'type': 'text', 'text': 'nudge', 'text_elements': []}]}]
        self.assertTrue(self.adapter.receipt(self.binding, 'nudge'))
        for input in ([{'type': 'text', 'text': 'nudge', 'text_elements': [{'start': 0}]}],
                      [{'type': 'text', 'text': 'nudge'}, {'type': 'image', 'url': 'x'}],
                      [{'type': 'text', 'text': 'nudge', 'attachment': 'x'}]):
            self.queued[0]['input'] = input
            self.assertFalse(self.adapter.receipt(self.binding, 'nudge'))

    def test_invalid_bindings_never_open_rpc(self):
        for changed in ({'host': 'remote'}, {'cwd': '/not-present'}, {'session_id': 'not-uuid'}, {'platform': 'other'}):
            self.assertEqual(self.adapter.preflight(dict(self.binding, **changed)), 'invalid')
        self.assertEqual(self.calls, [])

    def test_archived_unreadable_unknown_or_unloaded_refused(self):
        for key, value in [('archived', True), ('readOnly', True), ('id', 'wrong'), ('cwd', '/wrong'), ('path', '/missing')]:
            original = dict(self.thread)
            self.thread[key] = value
            self.assertEqual(self.adapter.preflight(self.binding), 'busy')
            self.thread = original
        self.adapter.loaded = lambda _: False
        self.assertEqual(self.adapter.preflight(self.binding), 'busy')

    def test_timeout_before_send_deferred_after_add_unknown(self):
        self.fail = 'thread/read'
        self.assertEqual(self.adapter.send(self.binding, 'nudge'), 'deferred')
        self.fail = 'thread/queue/add'
        self.assertEqual(self.adapter.send(self.binding, 'nudge'), 'unknown')
        self.assertEqual(sum(method == 'thread/queue/add' for method, _ in self.calls), 1)

    def test_receipt_queue_client_id_or_exact_user_text_only(self):
        self.queued = [{'clientUserMessageId': message_id(self.binding, 'nudge'), 'input': [{'type': 'text', 'text': 'nudge', 'textElements': []}]}]
        self.assertTrue(self.adapter.receipt(self.binding, 'nudge'))
        self.queued = []
        self.path.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'input_text', 'text': 'nudge'}]}})+'\n')
        self.assertFalse(self.adapter.receipt(self.binding, 'nudge'))
        self.path.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'nudge'}})+'\n')
        self.assertTrue(self.adapter.receipt(self.binding, 'nudge'))

    def test_rpc_process_argv_has_no_resume_start_or_model_override(self):
        # A real local fake protocol subprocess exercises initialize, malformed JSON and cleanup.
        script = self.home/'fake-codex'
        script.write_text('#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n if "id" in r:\n  print(json.dumps({"id":r["id"],"result":{}}),flush=True)\n')
        script.chmod(0o700)
        rpc = RPC(str(script))
        try:
            self.assertEqual(rpc.proc.args, [str(script), 'app-server', '--listen', 'stdio://'])
            self.assertEqual(rpc.request('thread/queue/list', {'threadId': SID}), {})
        finally:
            rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)

    def test_rpc_blocked_writer_obeys_overall_deadline_and_closes(self):
        script = self.home/'fake-codex'
        script.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        script.chmod(0o700)
        with patch.object(RPC, 'request', autospec=True, return_value={}):
            rpc = RPC(str(script))
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                rpc.request('thread/queue/add', {'text': 'x'*1000000}, timeout=0.1)
            self.assertLess(time.monotonic()-started, 1)
        finally:
            rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)
        self.assertLess(time.monotonic()-started, 3)

    def test_rpc_short_writes_preserve_whole_request(self):
        script = self.home/'fake-codex'
        script.write_text('#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n if "id" in r: print(json.dumps({"id":r["id"],"result":{"method":r["method"]}}),flush=True)\n')
        script.chmod(0o700)
        original = os.write
        with patch.object(module.os, 'write', side_effect=lambda fd, data: original(fd, data[:7])) as write:
            rpc = RPC(str(script))
            try:
                self.assertEqual(rpc.request('thread/queue/list', {'threadId': SID}), {'method': 'thread/queue/list'})
                self.assertGreater(write.call_count, 20)
            finally:
                rpc.close()

    def test_rpc_malformed_response_and_timeout(self):
        for body, error in [('print("not-json",flush=True)', ValueError), ('import time;time.sleep(2)', TimeoutError)]:
            script = self.home/'fake-codex'
            script.write_text('#!/usr/bin/env python3\nimport sys\nfor line in sys.stdin:\n '+body+'\n')
            script.chmod(0o700)
            with patch.object(RPC, 'request', autospec=True) as init:
                init.return_value = {}
                rpc = RPC(str(script))
            try:
                with self.assertRaises(error):
                    rpc.request('thread/read', {}, timeout=2 if error is ValueError else 0.1)
            finally:
                rpc.close()


class LoadedProofTests(unittest.TestCase):
    def test_exact_desktop_executable_and_rollout_must_share_process(self):
        path = '/Users/x/.codex/sessions/rollout.jsonl'
        output = ('p1\nn' + module.DESKTOP_EXECUTABLE + '\np2\nn' + path + '\n').encode()
        with patch.object(module, '_LOADED_CACHE', (0, frozenset())), patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)):
            self.assertEqual(module.loaded_rollouts(), frozenset())
        output = ('p1\nn' + module.DESKTOP_EXECUTABLE + '\nn' + path + '\n').encode()
        with patch.object(module, '_LOADED_CACHE', (0, frozenset())), patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)) as run:
            self.assertEqual(module.loaded_rollouts(), frozenset([path]))
            self.assertEqual(module.loaded_rollouts(), frozenset([path]))
            self.assertEqual(run.call_count, 1)

    def test_timeout_fails_closed(self):
        with patch.object(module, '_LOADED_CACHE', (0, frozenset())), patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('lsof', 3)):
            self.assertEqual(module.loaded_rollouts(), frozenset())

    def test_rpc_explicit_codex_home(self):
        with tempfile.TemporaryDirectory() as home, patch.object(module, 'RPC') as rpc:
            CodexChatTransport(home=home).rpc_factory()
            self.assertEqual(rpc.call_args.kwargs['env']['CODEX_HOME'], str(Path(home)/'.codex'))
