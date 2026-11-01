import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('export_catalog', HERE / 'export_catalog.py')
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)

class ExportCatalogTests(unittest.TestCase):
    def test_installation_metadata_and_binary_are_reviewed_reads(self):
        for name in ['gr_wf_installation_metadata', 'gr_wf_git_blob_binary_get']:
            self.assertIn(name, export.READ_TOOLS)
        self.assertNotIn('gr_wf_repo_variable_create', export.READ_TOOLS)
        self.assertNotIn('gr_wf_unknown_future', export.READ_TOOLS)

    def test_capture_keeps_dispatch_fences_and_neutralizes_only_identity(self):
        schema = {'type': 'object', 'properties': {
            'identity': {'type': 'string', 'enum': ['ECHO']},
            'intent_id': {'type': 'string', 'pattern': '^intent$'},
            'expected_head_sha': {'type': 'string', 'pattern': '^[0-9a-f]{40}$'},
        }, 'required': ['identity']}
        tools = [{'name': n, 'inputSchema': schema} for n in [
            'gr_wf_installation_metadata', 'gr_wf_git_blob_binary_get',
            'gr_workflow_dispatch', 'gr_wf_unknown_future',
        ]]
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / 'tools.json'
            capture.write_text(json.dumps({'tools': tools}))
            out = subprocess.check_output([sys.executable, str(HERE / 'export_catalog.py'), str(capture)])
        rows = {x['name']: x for x in json.loads(out)['tools']}
        for name in ['gr_wf_installation_metadata', 'gr_wf_git_blob_binary_get']:
            self.assertTrue(rows[name]['read'])
        for name in ['gr_workflow_dispatch', 'gr_wf_unknown_future']:
            self.assertFalse(rows[name]['read'])
        actual = rows['gr_workflow_dispatch']['schema']
        self.assertNotIn('enum', actual['properties']['identity'])
        for key in ['intent_id', 'expected_head_sha']:
            self.assertEqual(actual['properties'][key], schema['properties'][key])
        self.assertEqual(actual['required'], ['identity'])
        self.assertEqual(schema['properties']['identity']['enum'], ['ECHO'])

if __name__ == '__main__':
    unittest.main()
