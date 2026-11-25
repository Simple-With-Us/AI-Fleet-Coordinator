"""Recall credential gates at the shared API and pre-API transport boundaries."""
from contextlib import ExitStack, contextmanager
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

from fleet_rag import contribute_guard, core, credential_gate, public_fallback, recall_api
from simple_tools import secretscan

SCRIPTS = Path(__file__).resolve().parents[2]
GOOD = 'Keep operational lessons concise and verify the cited source before acting.'


def synthetic_credential():
    return 'sk-' + 'x' * 24


@contextmanager
def no_effects():
    """The gate must stop before config, cache, telemetry and backend seams."""
    with ExitStack() as stack:
        seams = [(recall_api, name) for name in ('get_config', 'embed', 'rerank', 'Qdrant')]
        seams += [(recall_api.search_cache, name) for name in ('lookup', 'store')]
        seams += [(recall_api.search_log, 'log_search'), (core, 'http_json')]
        spies = [stack.enter_context(mock.patch.object(module, name)) for module, name in seams]
        yield
        for spy in spies:
            spy.assert_not_called()


def load_service():
    spec = importlib.util.spec_from_file_location('credential_gate_service', SCRIPTS/'fleet-recall-service/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CredentialGateTests(unittest.TestCase):
    def test_exact_query_is_scanned_before_config_cache_or_backend(self):
        query = '  investigate ' + synthetic_credential() + '  '
        scanner = mock.Mock(wraps=secretscan.scan)
        with mock.patch.object(credential_gate, '_scanner', return_value=scanner), no_effects():
            with self.assertRaises(core.FleetRagError) as failure:
                recall_api.recall_search(query)
        self.assertEqual(str(failure.exception), 'refused_secret:query')
        scanner.assert_called_once_with(query)

    def test_contribution_fields_are_refused_before_scrub_or_persistence(self):
        for field in ('text', 'title', 'url'):
            values = dict(text=GOOD, title='Operational Lesson', url='https://example.invalid/lesson')
            values[field] += ' ' + synthetic_credential()
            with self.subTest(field=field), mock.patch.object(recall_api, 'scrub') as scrub, no_effects():
                with self.assertRaises(core.FleetRagError) as failure:
                    recall_api.recall_contribute(**values, category='lesson', seat='CODEX')
            self.assertEqual(str(failure.exception), 'refused_secret:' + field)
            scrub.assert_not_called()

    def test_missing_or_failed_scanner_refuses_with_fixed_field_code(self):
        cases = [mock.patch.dict(sys.modules, {'simple_tools.secretscan': None}),
                 mock.patch.object(secretscan, 'scan', side_effect=RuntimeError(synthetic_credential()))]
        for missing in cases:
            with missing, no_effects():
                for field, call in (
                    ('query', lambda: recall_api.recall_search('a benign question')),
                    ('text', lambda: recall_api.recall_contribute(GOOD, 'lesson', seat='CODEX')),
                ):
                    with self.subTest(field=field), self.assertRaises(core.FleetRagError) as failure:
                        call()
                    self.assertEqual(str(failure.exception), 'credential_scan_unavailable:' + field)
                    self.assertTrue(failure.exception.__suppress_context__)

    def test_public_fallback_refuses_before_resolving_credentials_or_http(self):
        for name, arguments, field in (
            ('recall_search', {'query': synthetic_credential()}, 'query'),
            ('recall_contribute', {'text': GOOD, 'title': synthetic_credential()}, 'title'),
        ):
            for missing in (False, True):
                with self.subTest(name=name, missing=missing), ExitStack() as stack:
                    if missing:
                        stack.enter_context(mock.patch.object(credential_gate, '_scanner', side_effect=ImportError('fixture')))
                    credentials = stack.enter_context(mock.patch.object(public_fallback, 'public_credentials'))
                    http = stack.enter_context(mock.patch.object(core, 'http_json'))
                    with self.assertRaises(core.FleetRagError) as failure:
                        public_fallback.call_public(name, arguments)
                    expected = 'credential_scan_unavailable:' if missing else 'refused_secret:'
                    self.assertEqual(str(failure.exception), expected + ('text' if missing and name == 'recall_contribute' else field))
                    credentials.assert_not_called(); http.assert_not_called()

    def test_near_duplicate_refuses_before_existing_scrub_embed_and_query(self):
        for missing in (False, True):
            with self.subTest(missing=missing), ExitStack() as stack:
                if missing:
                    stack.enter_context(mock.patch.object(credential_gate, '_scanner', side_effect=ImportError('fixture')))
                scrub = stack.enter_context(mock.patch.object(contribute_guard, 'scrub'))
                embed = mock.Mock(); receiver = mock.Mock()
                with self.assertRaises(core.FleetRagError) as failure:
                    contribute_guard.near_duplicate({}, receiver, synthetic_credential(), embed=embed)
                self.assertEqual(str(failure.exception), ('credential_scan_unavailable:' if missing else 'refused_secret:') + 'text')
                scrub.assert_not_called(); embed.assert_not_called(); receiver.search_dense.assert_not_called()

    def test_service_and_seat_bridge_traverse_real_shared_gate(self):
        service = load_service()
        sys.path.insert(0, str(SCRIPTS/'seat-mcp'))
        self.addCleanup(sys.path.remove, str(SCRIPTS/'seat-mcp'))
        from seat_mcp import recall_bridge
        from seat_mcp.seats import SeatError
        self.addCleanup(recall_bridge._reset_api_cache)
        for missing in (False, True):
            with self.subTest(missing=missing), ExitStack() as stack:
                if missing:
                    stack.enter_context(mock.patch.object(credential_gate, '_scanner', side_effect=ImportError('fixture')))
                stack.enter_context(mock.patch.object(recall_bridge, '_load_api', return_value=recall_api))
                index = stack.enter_context(mock.patch.object(service, 'ensure_retracted_index'))
                stack.enter_context(no_effects())
                for gateway, error in ((service.call_tool, core.FleetRagError),
                                       (lambda name, args: getattr(recall_bridge, name)(args), SeatError)):
                    with self.assertRaises(error) as failure:
                        gateway('recall_search', {'query': synthetic_credential()})
                    self.assertIn(('credential_scan_unavailable:' if missing else 'refused_secret:') + 'query', str(failure.exception))
                    with self.assertRaises(error) as failure:
                        gateway('recall_contribute', {'text': GOOD, 'title': synthetic_credential(), 'category': 'lesson', 'seat': 'CODEX'})
                    self.assertIn(('credential_scan_unavailable:text' if missing else 'refused_secret:title'), str(failure.exception))
                index.assert_not_called()

    def test_benign_api_calls_preserve_scrub_and_optional_gitleaks(self):
        names = ('load_config', 'embed', 'embedder_healthy', 'rerank_healthy', 'Qdrant',
                 'rerank', 'gitleaks_flagged', 'gitleaks_available')
        saved = {name: getattr(recall_api, name) for name in names}
        self.addCleanup(recall_api.reset_config_cache)
        for name, value in saved.items():
            self.addCleanup(setattr, recall_api, name, value)
        recall_api.install_fake_backend()
        with mock.patch.object(recall_api.search_log, 'log_search') as log, mock.patch.object(recall_api, 'scrub', wraps=recall_api.scrub) as scrub:
            result = recall_api.recall_search('handoff operational lesson')
            self.assertIn('hits', result); log.assert_called_once()
            for available in (False, True):
                with mock.patch.object(recall_api, 'gitleaks_available', return_value=available):
                    result = recall_api.recall_contribute(GOOD + ' ' + str(available), 'lesson', seat='CODEX')
                self.assertEqual('gitleaks-unavailable' in result['scrubbed'], not available)
            self.assertEqual(scrub.call_count, 2)

    def test_service_distribution_includes_existing_scanner(self):
        docker = (SCRIPTS/'fleet-recall-service/Dockerfile').read_text()
        bootstrap = (SCRIPTS/'fleet-recall-service/bootstrap.sh').read_text()
        self.assertIn('COPY simple_tools/secretscan.py /app/simple_tools/secretscan.py', docker)
        self.assertIn('"simple_tools/secretscan.py"', bootstrap)
