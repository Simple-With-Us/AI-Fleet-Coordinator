import ast, errno, fcntl, os, subprocess, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
source=Path(__file__).with_name('candidate-zcode_wake.py').read_text()
node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_deliver_prompt')
class Delivery:
 def __init__(self,outcome,*a,code=None,**kw):self.outcome,self.code=outcome,code
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir='/dev/shm');self.root=Path(self.tmp.name);self.calls=[];self.binding={'id':'sess-fixture','directory':str(self.root)}
  self.g={'Mapping':dict,'Any':object,'WR':SimpleNamespace(Delivery=Delivery,Target=object,workspace_allowed=lambda a,b:a==b),'Runner':object,'subprocess':subprocess,'SESSION_RE':__import__('re').compile('sess-fixture'),'state_dir':lambda env:self.root,'_private_dir':lambda p:p.mkdir(exist_ok=True),'os':os,'Path':Path,'fcntl':fcntl,'errno':errno,'_tasks_row':lambda *a:('ok',{'task_status':'running'}),'_session_binding':lambda *a:('ok',self.binding),'_subagent_ids':lambda *a:('ok',set()),'_recheck':lambda *a:None}
  exec(compile(ast.Module(body=[node],type_ignores=[]),'<bridge>','exec'),self.g)
  self.target=SimpleNamespace(host='server',generation=1)
 def tearDown(self):self.tmp.cleanup()
 def call(self,**kw):
  def driver(*a,**k):
   self.calls.append((a,k));self.assertTrue(k['verify'](a[0]));return {'outcome':'unknown','code':'native-admitted-not-confirmed'}
  return self.g['_deliver_prompt']({'HOME':str(self.root)}, {'workspace':str(self.root),'native_delivery_review_only':True},'sess-fixture','fixture',1,evidence_ids=('new-wake',),target=kw.pop('target',self.target),native_driver=kw.pop('driver',driver),native_fresh=True,**kw)
 def test_busy_queues_only_native_branch(self):self.assertEqual(self.call().code,'native-unconfirmed');self.assertEqual(len(self.calls),1)
 def test_no_driver_no_fallback(self):self.assertEqual(self.call(driver=None).code,'native-not-qualified')
 def test_missing_target_refused(self):self.assertEqual(self.call(target=None).code,'native-not-qualified')
 def test_mac_refused(self):self.target.host='mac';self.assertEqual(self.call().code,'native-local-server-only')
 def test_stale_owner_refused(self):self.g['_recheck']=lambda *a:Delivery('refused',code='changed');self.assertEqual(self.call().code,'native-binding-changed')
 def test_wrong_workspace_refused(self):self.binding['directory']='/other';self.assertEqual(self.call().code,'workspace-mismatch')
 def test_subagent_refused(self):self.g['_subagent_ids']=lambda *a:('ok',{'sess-fixture'});self.assertEqual(self.call().code,'session-subagent')
 def test_missing_session_refused(self):self.g['_session_binding']=lambda *a:('missing',None);self.assertEqual(self.call().code,'session-not-found')
 def test_driver_error_unknown_no_fallback(self):
  def driver(*a,**k):raise TimeoutError()
  self.assertEqual(self.call(driver=driver).code,'native-driver-uncertain')
 def test_untrusted_driver_results_never_retryable(self):
  for bad in [None, [], {}, {'outcome':'failed','code':'resume-OSError'}, {'outcome':'busy'}, {'outcome':'refused'}, {'outcome':'sent'}]:
   self.assertEqual(self.call(driver=lambda *a,**k:bad).outcome,'unknown')
if __name__=='__main__':unittest.main(verbosity=2)
