import base64
import unittest
from .test_workflow_verbs import Client, Policy, Minter, REPO, SHA, CEILING, Multi
from git_relay.workflow_verbs import WorkflowVerbs, CeilingDenied, Refused
class WorkflowPermissions(unittest.TestCase):
 def handle(self, action, args, ceiling=None):
  minter=Minter();handle=WorkflowVerbs.for_action(Client(),Policy(ceiling or {**CEILING,'workflows':'write'}),minter,'swu-wise',action,REPO,args=args)
  return handle,minter
 def test_workflow_edits_mint_workflow_scope(self):
  for action,args in [('wf_file_put',{'path':'.github/workflows/ci.yml'}),('wf_file_delete',{'path':'.github/workflows/ci.yml'}),('wf_file_rename',{'source_path':'docs/ci.yml','destination_path':'.github/workflows/ci.yml'}),('wf_files_put',{'files':[{'path':'.github/workflows/ci.yml'}]})]:
   handle,minter=self.handle(action,args)
   self.assertEqual(minter.calls[0][2],{'contents':'write','metadata':'read','workflows':'write'})
   self.assertFalse(handle._write_path_denied('.github/workflows/ci.yml'))
 def test_ordinary_write_does_not_mint_workflows(self):
  handle,minter=self.handle('wf_files_put',{'files':[{'path':'docs/ci.yml'}]})
  self.assertNotIn('workflows',minter.calls[0][2]);self.assertTrue(handle._write_path_denied('.github/workflows/ci.yml'))
 def test_ceiling_missing_fails_before_mint(self):
  minter=Minter()
  with self.assertRaises(CeilingDenied):WorkflowVerbs.for_action(Client(),Policy(CEILING),minter,'swu-wise','wf_files_put',REPO,args={'files':[{'path':'.github/workflows/ci.yml'}]})
  self.assertEqual(minter.calls,[])
 def test_parent_dirs_still_refused_with_permission(self):
  handle,_=self.handle('wf_file_delete',{'path':'.github/workflows/ci.yml'})
  for p in ['.github','.github/workflows']:self.assertTrue(handle._write_path_denied(p))
 def test_shape_accepts_workflow_without_mint(self):
  WorkflowVerbs.check_shape('wf_files_put',{'repo':REPO,'branch':'wise/test','message':'workflow','expected_head_sha':SHA,'files':[{'path':'.github/workflows/release.yml','content_b64':base64.b64encode(b'name: release').decode()}]},REPO)
 def test_default_branch_still_refused(self):
  handle,_=self.handle('wf_file_put',{'path':'.github/workflows/ci.yml'})
  handle.client=Client({'default_branch':'main'})
  with self.assertRaises(Refused):handle.run('wf_file_put',{'repo':REPO,'branch':'main','message':'m','path':'.github/workflows/ci.yml','content_b64':'eA=='})
 def test_secret_scan_still_refuses(self):
  handle,_=self.handle('wf_file_put',{'path':'.github/workflows/ci.yml'})
  with self.assertRaises(Refused):handle.run('wf_file_put',{'repo':REPO,'branch':'wise/test','message':'m','path':'.github/workflows/ci.yml','content_b64':base64.b64encode(('ghp_'+'A'*40).encode()).decode()})
if __name__=='__main__':unittest.main()
