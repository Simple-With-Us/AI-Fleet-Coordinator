"""Hermetic authenticated-identity regression tests for remote Recall writes."""
import importlib.machinery
import importlib.util
import json
import os
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

loader = importlib.machinery.SourceFileLoader('recall_identity_test', str(Path(__file__).resolve().parents[2] / 'recall'))
spec = importlib.util.spec_from_loader(loader.name, loader)
recall = importlib.util.module_from_spec(spec)
loader.exec_module(recall)


class RemoteIdentityTests(unittest.TestCase):
    def principal(self, **updates):
        return {'authenticated_seat': 'ZC', 'default_identity': 'ZC', 'identities': [{'identity': 'ZC'}], **updates}

    def test_strict_principal_validation(self):
        good = self.principal()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(recall._remote_principal({'structuredContent': good}), 'ZC')
            for data in (None, {}, self.principal(authenticated_seat='ECHO'),
                         self.principal(default_identity=[]), self.principal(identities=[]),
                         self.principal(identities=[{'identity': 'ECHO'}]),
                         self.principal(identities=[{'identity': 'ZC'}, None]),
                         # A missing authenticated_seat is never covered by the default alone.
                         self.principal(default_identity=None),
                         self.principal(authenticated_seat=None),
                         self.principal(authenticated_seat=None, default_identity=None)):
                with self.subTest(data=data), self.assertRaises(recall.FleetRagError):
                    recall._remote_principal({'structuredContent': data})
            # Server-valid seat tags (canonical recall_api._SEAT_RE shape) are accepted.
            tagged = self.principal(authenticated_seat='ZC_2', default_identity='ZC_2',
                                    identities=[{'identity': 'ZC_2'}])
            self.assertEqual(recall._remote_principal({'structuredContent': tagged}), 'ZC_2')

    def test_declared_identity_must_match_and_launcher_must_be_bound(self):
        for env in ({'AGENT_SEAT': 'ECHO'}, {'AGENT_TAG': 'ECHO'},
                    {'AGENT_LAUNCHER': 'zcode'},
                    {'AGENT_LAUNCHER': 'zcode', 'AGENT_LAUNCH_SEAT': 'ECHO'}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), self.assertRaises(recall.FleetRagError):
                recall._remote_principal({'structuredContent': self.principal()})

    def test_launch_seat_alone_is_the_valid_launched_assignment(self):
        # Established file_guard/fleet_bearer behavior: AGENT_LAUNCH_SEAT names the seat
        # even without AGENT_LAUNCHER; only launcher-without-seat is refused.
        with mock.patch.dict(os.environ, {'AGENT_LAUNCH_SEAT': 'ZC'}, clear=True):
            self.assertEqual(recall._remote_principal({'structuredContent': self.principal()}), 'ZC')

    def test_same_captured_bearer_and_injected_identity_for_every_write(self):
        bearer = 'sbt_zc_' + 'x' * 43
        for tool in ('recall_contribute', 'recall_correct', 'recall_retract'):
            calls = []
            def post(url, headers, body, timeout):
                msg = json.loads(body)
                calls.append((headers, msg))
                result = {'structuredContent': self.principal()} if len(calls) == 1 else {'structuredContent': {'ok': True}}
                os.environ['SIMPLE_TOOLS_BEARER'] = 'sbt_echo_' + 'y' * 43
                return 200, 'application/json', json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}).encode()
            with mock.patch.dict(os.environ, {'SIMPLE_TOOLS_BEARER': bearer, 'AGENT_SEAT': 'ZC'}, clear=True), mock.patch.object(recall, '_remote_post', post), mock.patch.object(recall, '_remote_scanner', return_value=SimpleNamespace(scan=lambda *a, **k: None)):
                recall.remote_call(tool, {'text': 'safe text'}, write=True)
            self.assertEqual([call[1]['params']['name'] for call in calls], ['identities', tool])
            self.assertEqual(calls[1][1]['params']['arguments']['identity'], 'ZC')
            self.assertTrue(all(call[0]['Authorization'] == 'Bearer ' + bearer for call in calls))

    def test_bad_identity_probe_never_sends_write(self):
        calls = []
        def post(url, headers, body, timeout):
            calls.append(json.loads(body))
            return 200, 'application/json', json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': self.principal(authenticated_seat='ECHO')}}).encode()
        with mock.patch.dict(os.environ, {'SIMPLE_TOOLS_BEARER': 'sbt_zc_' + 'x' * 43}, clear=True), mock.patch.object(recall, '_remote_post', post), mock.patch.object(recall, '_remote_scanner', return_value=SimpleNamespace(scan=lambda *a, **k: None)), self.assertRaises(recall.FleetRagError):
            recall.remote_call('recall_contribute', {'text': 'safe text'}, write=True)
        self.assertEqual([call['params']['name'] for call in calls], ['identities'])
