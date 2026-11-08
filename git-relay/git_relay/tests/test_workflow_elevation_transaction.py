import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / 'deploy' / 'workflow-elevation-1549'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('workflow1549_builder', 'build.py')
proof = load('workflow1549_proof', 'runtime_proof.py')


class BuilderTests(unittest.TestCase):
    def test_baseline_drift_refuses(self):
        with self.assertRaisesRegex(ValueError, 'baseline drift'):
            builder.image(b'changed', b'candidate')

    def test_candidate_drift_refuses(self):
        with patch.object(builder, 'digest', side_effect=[builder.BEFORE, 'wrong']):
            with self.assertRaisesRegex(ValueError, 'candidate drift'):
                builder.image(b'before', b'candidate')

    def test_image_parses_syntax(self):
        with patch.object(builder, 'digest', side_effect=[builder.BEFORE, builder.AFTER]):
            with self.assertRaises(SyntaxError):
                builder.image(b'before', b'def broken(')

    def test_render_binds_exact_manifest_and_modules(self):
        raw = b'manifest'
        sources = {'git_relay/a.py': b'content'}
        entry = builder.render_entry((HERE / 'runner.template.py').read_text(), raw, sources)
        self.assertNotIn('PLACEHOLDER', entry)
        self.assertIn(builder.digest(raw), entry)
        self.assertIn(builder.digest(b'content'), entry)
        self.assertIn(builder.ROOT, entry)

    def test_render_refuses_missing_or_repeated_placeholders(self):
        template = (HERE / 'runner.template.py').read_text()
        for changed in [template.replace('MANIFEST_DIGEST_PLACEHOLDER', 'missing'),
                        template + '\n# MANIFEST_DIGEST_PLACEHOLDER']:
            with self.assertRaises(ValueError):
                builder.render_entry(changed, b'manifest', {'a': b'content'})

    def test_build_has_only_one_exact_file_and_fixed_bindings(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / 'before'
            before.write_bytes(b'before')
            output = Path(directory) / 'new'
            with patch.object(builder, 'image', return_value=b'candidate'):
                receipt = builder.build(root, before, output)
            manifest = json.loads((output / 'manifest.json').read_bytes())
            self.assertEqual(manifest['files'], [{
                'root': 'code', 'name': 'workflow_verbs.py',
                'before': builder.BEFORE, 'after': builder.AFTER,
                'meta': {'uid': 0, 'gid': 0, 'mode': 420}}])
            self.assertEqual(manifest['runner'], builder.ROOT + '/runner.py')
            self.assertEqual(manifest['unit'], builder.UNIT)
            self.assertEqual(manifest['config'], builder.CONFIG)
            self.assertEqual(receipt['manifest'], builder.digest((output / 'manifest.json').read_bytes()))
            self.assertFalse((output / '1.candidate').exists())
            with self.assertRaises(FileExistsError):
                with patch.object(builder, 'image', return_value=b'candidate'):
                    builder.build(root, before, output)


class ProofTests(unittest.TestCase):
    def run_proof(self, replies):
        instance = proof.RuntimeProof('test-only')
        calls = []
        def call(action, args):
            calls.append((action, args))
            return replies[len(calls) - 1]
        instance.call = call
        result = instance({}, {'code/workflow_verbs.py': builder.AFTER})
        return result, calls

    def repository(self, default='main'):
        return (200, {'repository': {'full_name': proof.RuntimeProof.REPO, 'default_branch': default}})

    def test_exact_no_write_probes(self):
        result, calls = self.run_proof([self.repository(), (400, {'error': 'default branch is not writable'})])
        self.assertTrue(result)
        self.assertEqual([x[0] for x in calls], ['wf_repo_get', 'wf_file_patch'])
        args = calls[1][1]
        self.assertEqual(args['branch'], 'main')
        self.assertEqual(args['expected_head_sha'], '0' * 40)
        self.assertEqual(args['base_blob_sha'], '0' * 40)
        self.assertEqual(args['edits'], [{'start': 0, 'end': 0, 'text': ''}])

    def test_wrong_repository_or_default_stops_before_patch(self):
        for reply in [(401, {}), self.repository('other'), (200, {'repository': {'full_name': 'wrong', 'default_branch': 'main'}})]:
            result, calls = self.run_proof([reply])
            self.assertFalse(result)
            self.assertEqual(len(calls), 1)

    def test_unexpected_patch_response_fails_without_retry(self):
        for response in [(200, {}), (400, {'error': 'unknown action'}), (500, None)]:
            result, calls = self.run_proof([self.repository(), response])
            self.assertFalse(result)
            self.assertEqual(len(calls), 2)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(proof.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'http://other'))


if __name__ == '__main__':
    unittest.main()
