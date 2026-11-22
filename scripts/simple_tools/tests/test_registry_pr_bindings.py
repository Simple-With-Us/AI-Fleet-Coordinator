import unittest,tempfile,io,json,os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from simple_tools import operator_registry as OR,platform_letter as PL,listener_registry as LR

class RegistryBindings(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=str(Path(self.tmp.name)/'registry.db');self.repo='Simple-With-Us/CodeCaps'
  r=OR.SessionRegistry(self.db);r.close_db()
 def register(self,sid):
  return OR.register_session(self.db,platform='testbed',verified_host='mac',profile_scope='default',native_session_id=sid,seat='CLAUDE',source='test',issuer='OPERATOR',letter_space='double',letter_pick='primary')
 def bind(self,sid,generation,number=537):
  return OR.bind_pr(self.db,platform='testbed',verified_host='mac',profile_scope='default',native_session_id=sid,repo=self.repo,number=number,generation=generation)
 def test_bound_visible_history_resolve_and_idempotent(self):
  row=self.register('test-session-a');out=self.bind('test-session-a',row['generation']);self.assertEqual(out['outcome'],'bound');self.assertEqual(self.bind('test-session-a',row['generation'])['outcome'],'unchanged')
  rows=OR.letter_history(self.db,platform='testbed')
  self.assertTrue(any(x.get('bound_prs')for x in rows))
  resolved=OR.resolve_label(self.db,platform='testbed',letter=out['label']);self.assertEqual(resolved['bound_prs'][0]['generation'],row['generation'])
 def test_other_live_session_conflicts(self):
  a=self.register('test-session-a');b=self.register('test-session-b');self.bind('test-session-a',a['generation'])
  with self.assertRaisesRegex(ValueError,'another live'):self.bind('test-session-b',b['generation'])
 def test_missing_generation_and_unknown_session_hold(self):
  row=self.register('test-session-a')
  for g in [None,0,True,-1,row['generation']+1]:
   with self.assertRaises(ValueError):self.bind('test-session-a',g)
  with self.assertRaises(ValueError):self.bind('test-missing-000',1)
 def test_repo_and_number_limits(self):
  a=self.register('test-session-a')
  for n in [True,0,10**12+1]:
   with self.assertRaises(ValueError):self.bind('test-session-a',a['generation'],n)
  for n in range(1,33):self.bind('test-session-a',a['generation'],n)
  with self.assertRaisesRegex(ValueError,'limit'):self.bind('test-session-a',a['generation'],33)
 def test_crossseat_refused_existing_boundary(self):
  a=self.register('test-session-a');svc=LR.RegistryService(self.db,{'CLAUDE':'c'*48,'CODEX':'d'*48});status,_=svc._op_bind_pr('CODEX',{'platform':'testbed','location':'mac','scope':'default','session_id':'test-session-a','repo':self.repo,'number':537,'generation':a['generation']});self.assertEqual(status,403)

 def test_same_record_live_generation_renewal(self):
  a=self.register('test-session-a');self.bind('test-session-a',a['generation'])
  reg=OR.SessionRegistry(self.db)
  with reg._write_txn()as conn:
   conn.execute("UPDATE alias_assignments SET generation=generation+1 WHERE record_id=? AND retired_at IS NULL",(a['record_id'],))
  reg.close_db()
  with self.assertRaises(ValueError):self.bind('test-session-a',a['generation'])
  self.assertEqual(self.bind('test-session-a',a['generation']+1)['outcome'],'renewed')
 def test_old_readonly_registry_has_no_binding(self):
  reg=OR.SessionRegistry(self.db)
  reg._conn.execute('DROP TABLE session_pr_bindings');reg._conn.commit();reg.close_db()
  reg=OR.SessionRegistry(self.db,read_only=True)
  self.assertEqual(OR._bound_prs(reg._conn,'missing'),[]);reg.close_db()
  reg=OR.SessionRegistry(self.db);self.assertIsNotNone(reg._conn.execute("SELECT name FROM sqlite_master WHERE name='session_pr_bindings'").fetchone());reg.close_db()

