"""Static future-exit categories, never evidence or retry permission."""
import dataclasses
import unittest
from simple_tools import zcode_wake as Z
from simple_tools.tests.test_zc_store import Runner

SAMPLES = (
    (b'Error: Failed to load environment file: /private/fixture/.env', 'cli-env-file-load'),
    (b'Error: ZCode app factory is unavailable.', 'cli-app-factory-unavailable'),
    (b'Error: Provider Registry runtime is unavailable.', 'cli-provider-runtime-unavailable'),
    (b'Error: Session not found: sess_fixture', 'cli-session-not-found'),
    (b'Error: Session is not active and not persisted: sess_fixture', 'cli-session-not-persisted'),
    (b'[zcode] process error kind=uncaughtException origin=uncaughtException', 'cli-process-error'),
    (b'[zcode] process error kind=unhandledRejection origin=unhandledRejection', 'cli-process-error'),
)

class Categories(unittest.TestCase):
    def test_exact_categories_static_only_and_opt_in(self):
        for raw, code in SAMPLES:
            with self.subTest(code=code):
                self.assertEqual(Z._failure_signature(raw), '')
                self.assertEqual(Z._failure_signature(raw, include_cli_errors=True), code)
                self.assertNotIn('fixture', code)

    def test_prefix_quotes_and_unknown_errors_are_not_categorized(self):
        for raw in (b'quoted Error: Session not found: sess_fixture', b'Error: unknown secret-like sk-fixture', b'[zcode] process error kind=other origin=other'):
            self.assertEqual(Z._failure_signature(raw, include_cli_errors=True), '')

    def test_scan_bound_and_no_raw_value_leak(self):
        raw=b'Error: Failed to load environment file: /private/token-fixture'
        self.assertEqual(Z._failure_signature(raw+b'x'*(Z.OUTPUT_SCAN_CAP*2),include_cli_errors=True),'')
        self.assertEqual(Z._failure_signature(b'x'*9000+b'\n'+raw,include_cli_errors=True),'cli-env-file-load')
        self.assertEqual(Z._failure_signature(object(),include_cli_errors=True),'')

    def test_existing_auth_signature_priority_unchanged(self):
        self.assertEqual(Z._failure_signature(SAMPLES[0][0]+b'\nproject_token_login_failed',include_cli_errors=True),'model-auth-login-failed')

class Delivery(Runner):
    def test_new_categories_preserve_single_and_turn_certainty(self):
        for raw, code in SAMPLES:
            for persisted in (True,False):
                with self.subTest(code=code,persisted=persisted):
                    d=self.single(1,raw,persist=persisted)
                    self.assertEqual((d.outcome,d.ack),('sent','unknown')if persisted else('refused','unknown'))
                    self.assertEqual(d.note,'zcode-prompt-persisted:'+code if persisted else '')
                    self.assertEqual(d.code,''if persisted else code)
                    self.assertNotIn('fixture',str(dataclasses.asdict(d)))
                    t=self.turn(1,raw,persist=persisted)
                    self.assertEqual(t.delivery.outcome,'sent'if persisted else'refused')
                    self.assertEqual(t.unreadable,not persisted)
                    self.assertNotEqual(d.outcome,'failed')

    def test_new_categories_do_not_reclassify_clean_exit(self):
        for raw,_ in SAMPLES:
            self.assertEqual(self.single(0,raw,persist=False).outcome,'sent')
            self.assertEqual(self.turn(0,raw,persist=False).delivery.outcome,'sent')

    def test_matching_quoted_full_line_never_proves_nonexecution(self):
        d=self.single(1,b'model quotation\n'+SAMPLES[0][0],persist=True)
        self.assertEqual((d.outcome,d.ack),('sent','unknown'))
