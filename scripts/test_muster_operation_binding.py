"""Synthetic exact-account and unavailable regressions. No live credentials or providers."""
import contextlib
import io
import unittest
from unittest import mock
from fleet_infisical_cli_tests.test_bounded_reads import DeclaredConsumerReaderTests


class ExpectedAccountTests(DeclaredConsumerReaderTests):
    def run_reader(self, verb='get', extra=()):
        return super().run_reader(verb, ('--consumer-account', 'consumer-ro', *extra))

    def test_wrong_same_scope_account_refuses_before_store(self):
        with mock.patch.object(self.reader.FI, 'token_from_store') as store:
            code, out, _ = super().run_reader(extra=('--consumer-account', 'another-ro'))
        self.assertEqual((code, out), (4, ''))
        store.assert_not_called()

    def test_undeclared_expected_account_refuses_before_store(self):
        with mock.patch.object(self.reader.FI, 'token_from_store') as store:
            with contextlib.redirect_stderr(io.StringIO()):
                code = self.reader.main(['reader', 'names', '--folder', '/', '--consumer-account', 'consumer-ro'])
        self.assertEqual(code, 2)
        store.assert_not_called()

    def test_store_and_provider_unavailable_are_empty_exit124_for_every_verb(self):
        failures = [TimeoutError('synthetic'),
                    self.reader.FI.SecretsError('synthetic', 124),
                    self.reader.FI.SecretsError('synthetic', failure_kind='unavailable')]
        for failure in failures:
            for verb in ('get', 'names', 'doctor'):
                for point in ('token_from_store', 'bounded_fetch_project' if verb == 'get' else 'list_project_names'):
                    with self.subTest(verb=verb, point=point, failure=type(failure).__name__):
                        with mock.patch.object(self.reader.FI, point, side_effect=failure):
                            code, out, err = self.run_reader(verb)
                        self.assertEqual((code, out), (124, ''))
                        self.assertNotIn('synthetic', err)

if __name__ == '__main__':
    unittest.main()
