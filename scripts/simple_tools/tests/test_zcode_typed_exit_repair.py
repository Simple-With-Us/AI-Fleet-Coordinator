import dataclasses
import unittest
from simple_tools import zcode_wake as Z
from simple_tools.tests.test_zc_store import Runner

TRACE = ' (traceId: 1855aaaf-ed8e-45e5-aaad-2a49506f8f81)'

class TypedExit(Runner):
    def test_known_templates_with_trace_and_bare_main_message(self):
        for message, code in [
            ('Session not found: private-session', 'cli-session-not-found'),
            ('ZCode app factory is unavailable.', 'cli-app-factory-unavailable'),
            ('Provider Registry runtime is unavailable.', 'cli-provider-runtime-unavailable'),
            ('Failed to load environment file: /private/path', 'cli-env-file-load'),
        ]:
            for prefix in ['', 'Error: ']:
                for suffix in ['', TRACE]:
                    with self.subTest(message=message,prefix=prefix,suffix=suffix):
                        out=(prefix+message+suffix+'\n').encode()
                        self.assertEqual(Z._failure_signature(out,include_cli_errors=True),code)
                        self.assertEqual(Z._failure_signature(out), '')
                        d=self.single(1,out,persist=False)
                        self.assertEqual((d.outcome,d.ack,d.code),('refused','unknown',code))
                        self.assertNotIn('private',str(dataclasses.asdict(d)))

    def test_unknown_error_emits_only_finite_shape_note(self):
        for out,note in [(b'', 'cli-unmapped:empty'),
            (b'Error: PRIVATE SECRET PROMPT\n','cli-unmapped:error-line'),
            (b'PRIVATE SECRET PROMPT\n','cli-unmapped:other-output'),
            (b'x'*(Z.OUTPUT_SCAN_CAP+1),'cli-unmapped:other-output:truncated')]:
            d=self.single(1,out,persist=False)
            self.assertEqual((d.outcome,d.code,d.ack,d.note),('refused','resume-exit-1','unknown',note))
            self.assertNotIn('PRIVATE',str(dataclasses.asdict(d)))

    def test_no_clean_exit_or_persisted_prompt_semantics_change(self):
        out=('Error: ZCode app factory is unavailable.'+TRACE).encode()
        self.assertEqual(self.single(0,out,persist=False).outcome,'sent')
        d=self.single(1,out,persist=True)
        self.assertEqual((d.outcome,d.ack),('sent','unknown'))
        self.assertEqual(d.note,'zcode-prompt-persisted:cli-app-factory-unavailable')

    def test_unknown_suffix_quote_prefix_and_partial_line_do_not_match(self):
        for out in [b'quoted Error: ZCode app factory is unavailable.',
            b'Error: ZCode app factory is unavailable. (traceId: PRIVATE)',
            b'x'*(Z.OUTPUT_SCAN_CAP+1)+b'Error: ZCode app factory is unavailable.']:
            self.assertEqual(Z._failure_signature(out,include_cli_errors=True),'')

if __name__=='__main__':unittest.main()
