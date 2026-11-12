"""Hosted contract.errorResult errors retain their code without echoing upstream text.

No real credentials or network. Run from scripts with unittest.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_tools import fleet_bearer as F


class ErrorMetadataTests(unittest.TestCase):
    def test_typed_error_metadata_json_and_sse(self):
        for content_type in ('application/json', 'text/event-stream'):
            for code in ('not_found', 'not_configured', 'forbidden', 'session_required', 'outcome_unknown'):
                with self.subTest(content_type=content_type, code=code):
                    self.check_error({'_meta': {'simple-tools/error': {'code': code, 'message': 'PRIVATE UPSTREAM TEXT'}}}, code, content_type)

    def test_metadata_takes_precedence_over_legacy_shape(self):
        self.check_error({'_meta': {'simple-tools/error': {'code': 'forbidden'}}, 'structuredContent': {'code': 'internal'}}, 'forbidden')

    def test_legacy_error_shape_still_supported(self):
        self.check_error({'structuredContent': {'code': 'not_configured'}}, 'not_configured')

    def test_malformed_metadata_uses_legacy_or_generic_without_echo(self):
        for value in (None, [], 'private', {'code': 'SECRET Value'}, {'code': 'x' * 41}, {'code': 123}):
            with self.subTest(value=value):
                self.check_error({'_meta': {'simple-tools/error': value}}, 'upstream_error')
                self.check_error({'_meta': {'simple-tools/error': value}, 'structuredContent': {'code': 'forbidden'}}, 'forbidden')
        for value in (None, [], 'private'):
            self.check_error({'_meta': value}, 'upstream_error')

    def test_write_keeps_unknown_warning_and_never_retries(self):
        self.check_error({'_meta': {'simple-tools/error': {'code': 'outcome_unknown'}}}, 'outcome_unknown', write=True)

    def check_error(self, fields, expected, content_type='application/json', write=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bearer'
            token = 'sbt_echo_' + 'x' * 43
            path.write_text(token)
            path.chmod(0o600)
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'PRIVATE UPSTREAM TEXT'}], **fields}
            reply = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})
            raw = ('data: ' + reply + '\n\n') if content_type == 'text/event-stream' else reply
            response = io.BytesIO(raw.encode())
            response.headers = {'Content-Type': content_type}
            with patch.object(F, 'gateway_transport') as transport:
                transport.return_value.open.return_value = response
                client = F.Client(path, identity_env={'AGENT_SEAT': 'ECHO'})
                with self.assertRaises(F.FleetError) as caught:
                    client.rpc('tools/call', {'name': 'notes_status', 'arguments': {}}, write=write)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn('PRIVATE UPSTREAM TEXT', str(caught.exception))
                self.assertNotIn(token, str(caught.exception))
                self.assertEqual(transport.return_value.open.call_count, 1)
                if write:
                    self.assertIn('outcome unknown', str(caught.exception))
