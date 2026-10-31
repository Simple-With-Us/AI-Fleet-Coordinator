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
 def test_missing_cutover_proof_skips_only_that_target(self):
  with patch.object(m.pathlib.Path,'exists',return_value=False):self.assertFalse(m.cutover_receipt())
 def test_synthetic_cutover_proof_fails(self):
  import json
  receipt={'sender_id':1212033,'channel_id':642233,'source_sha':'a'*40,'version_id':'verified','message_id':123,'message_url':'https://simplewithus.zulipchat.com/#narrow/id/123','real_conflict_event':False}
  with patch.object(m.pathlib.Path,'exists',return_value=True),patch.object(m.pathlib.Path,'read_text',return_value=json.dumps(receipt)):
   with self.assertRaises(RuntimeError):m.cutover_receipt()
 def test_review_includes_release_infrastructure(self):
  self.assertIn('.github/workflows/alert-workers-deploy.yml',m.RELEASE_PATHS)
  self.assertIn('scripts/deploy-mapped-worker.py',m.RELEASE_PATHS)
 def test_changed_kv_is_blocked_before_release(self):
  with patch.object(m.pathlib.Path,'read_text',return_value='[[kv_namespaces]]\nbinding="PR_STATE"\nid="changed"\n'):
   with self.assertRaises(RuntimeError):m.preflight_config({'config':'x'},{'bindings':[{'name':'PR_STATE','namespace_id':'existing'}]})
 def test_unchanged_kv_passes(self):
  with patch.object(m.pathlib.Path,'read_text',return_value='[[kv_namespaces]]\nbinding="PR_STATE"\nid="same"\n'):
   m.preflight_config({'config':'x'},{'bindings':[{'name':'PR_STATE','namespace_id':'same'}]})
 def test_old_review_cannot_cover_changed_release_source(self):
  pr={'number':1,'merged_at':'now','merge_commit_sha':'a'*40,'head':{'sha':'b'*40}}
  review={'user':{'login':'kody-ai[bot]'},'state':'APPROVED','commit_id':'c'*40}
  with patch.object(m,'github',side_effect=[[pr],[review]]),patch.object(m,'git',return_value='scripts/deploy-alert-workers.py'):
   with self.assertRaises(RuntimeError):m.reviewed('a'*40)
 def test_review_with_unresolved_thread_fails(self):
  import json
  from unittest.mock import MagicMock
  pr={'number':1,'merged_at':'now','merge_commit_sha':'a'*40,'head':{'sha':'b'*40}}
  review={'user':{'login':'kody-ai[bot]'},'state':'APPROVED','commit_id':'b'*40}
  response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'data':{'repository':{'pullRequest':{'reviewThreads':{'pageInfo':{'hasNextPage':False},'nodes':[{'isResolved':False}]}}}}}).encode()
  with patch.object(m,'github',side_effect=[[pr],[review]]),patch.dict(m.os.environ,{'GITHUB_REPOSITORY':'test/repo','GH_TOKEN':'offline'}),patch.object(m.urllib.request,'urlopen',return_value=response):
   with self.assertRaises(RuntimeError):m.reviewed('a'*40)
 def test_exact_review_and_resolved_threads_pass(self):
  import json
  from unittest.mock import MagicMock
  pr={'number':1,'merged_at':'now','merge_commit_sha':'a'*40,'head':{'sha':'b'*40}}
  review={'user':{'login':'kody-ai[bot]'},'state':'COMMENTED','commit_id':'b'*40}
  response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'data':{'repository':{'pullRequest':{'reviewThreads':{'pageInfo':{'hasNextPage':False},'nodes':[{'isResolved':True}]}}}}}).encode()
  with patch.object(m,'github',side_effect=[[pr],[review]]),patch.dict(m.os.environ,{'GITHUB_REPOSITORY':'test/repo','GH_TOKEN':'offline'}),patch.object(m.urllib.request,'urlopen',return_value=response):m.reviewed('a'*40)
 def test_workflow_is_main_ci_and_catchup_without_broad_gate(self):
  text=m.pathlib.Path('.github/workflows/alert-workers-deploy.yml').read_text()
  self.assertIn('workflow_run:',text);self.assertIn('workflows: [CI]',text)
  self.assertNotIn('WORKER_DEPLOY_ENABLED',text);self.assertNotIn('agent-sync-mcp/wrangler',text);self.assertIn('17,47 * * * *',text)
if __name__=='__main__':unittest.main()
