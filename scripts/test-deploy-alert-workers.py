import importlib.util
import unittest
from unittest.mock import patch
s=importlib.util.spec_from_file_location('release','scripts/deploy-alert-workers.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Tests(unittest.TestCase):
 def test_targets_cannot_expand_to_shared_workers(self):
  self.assertEqual(set(m.TARGETS),{'alert-ingress','pr-conflict-watch'})
  self.assertIn('zulip-cutover',m.TARGETS['pr-conflict-watch']['config'])
 def test_no_review_fails_closed(self):
  with patch.object(m,'github',return_value=[]):
   with self.assertRaises(RuntimeError):m.reviewed('a'*40)
 def test_unreviewed_merge_fails(self):
  with patch.object(m,'github',side_effect=[[{'number':1,'merged_at':'now','merge_commit_sha':'a'*40}],[]]):
   with self.assertRaises(RuntimeError):m.reviewed('a'*40)
 def test_no_exact_main_ci_fails(self):
  with patch.object(m.subprocess,'check_output',return_value='a'*40),patch.object(m.m,'current_main',return_value='a'*40),patch.object(m,'github',return_value={'workflow_runs':[]}):
   with self.assertRaises(RuntimeError):m.main()
 def test_superseded_does_not_release(self):
  with patch.object(m.subprocess,'check_output',return_value='a'*40),patch.object(m.m,'current_main',return_value='b'*40),patch.object(m.subprocess,'run') as run:
   m.main();run.assert_not_called()
 def test_workflow_is_main_ci_only_and_has_no_broad_gate(self):
  text=open('.github/workflows/alert-workers-deploy.yml').read()
  self.assertIn('workflow_run:',text);self.assertIn('workflows: [CI]',text)
  self.assertNotIn('WORKER_DEPLOY_ENABLED',text);self.assertNotIn('agent-sync-mcp/wrangler',text);self.assertNotIn('schedule:',text)
if __name__=='__main__':unittest.main()
