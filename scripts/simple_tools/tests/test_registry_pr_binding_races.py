import unittest,tempfile,io,json,threading,concurrent.futures
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from simple_tools import operator_registry as OR,platform_letter as PL,listener_registry as LR,registry_api as RA

class ExtraBindings(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=str(Path(self.tmp.name)/'r.db');r=OR.SessionRegistry(self.db);r.close_db();self.repo='Simple-With-Us/CodeCaps'
 def register(self,sid,platform='testbed',seat='CODEX'):
  return OR.register_session(self.db,platform=platform,verified_host='mac',profile_scope='default',native_session_id=sid,seat=seat,source='test',issuer='OPERATOR',letter_space='double',letter_pick='primary')
 def bind(self,sid,g,platform='testbed'):
  return OR.bind_pr(self.db,platform=platform,verified_host='mac',profile_scope='default',native_session_id=sid,repo=self.repo,number=537,generation=g)
 def test_concurrent_different_record_only_one_wins(self):
  rows=[self.register('native-session-'+str(i)) for i in range(2)];barrier=threading.Barrier(2)
  def go(i):
   barrier.wait()
   try:return self.bind('native-session-'+str(i),rows[i]['generation'])['outcome']
   except ValueError as e:return str(e)
  with concurrent.futures.ThreadPoolExecutor(2) as e:out=list(e.map(go,range(2)))
  self.assertEqual(out.count('bound'),1);self.assertEqual(sum('another live' in x for x in out),1)
 def test_renewal_still_conflicts_with_new_live_record(self):
  a=self.register('native-session-a');self.bind('native-session-a',a['generation']);reg=OR.SessionRegistry(self.db)
  with reg._write_txn() as c:c.execute('UPDATE alias_assignments SET generation=generation+1 WHERE record_id=? AND retired_at IS NULL',(a['record_id'],))
  reg.close_db();b=self.register('native-session-b');self.bind('native-session-b',b['generation'])
  with self.assertRaisesRegex(ValueError,'another live'):self.bind('native-session-a',a['generation']+1)
 def test_renewal_has_event_and_current_stamp(self):
  a=self.register('native-session-a');self.bind('native-session-a',a['generation']);reg=OR.SessionRegistry(self.db)
  with reg._write_txn() as c:c.execute('UPDATE alias_assignments SET generation=generation+1 WHERE record_id=? AND retired_at IS NULL',(a['record_id'],))
  reg.close_db();out=self.bind('native-session-a',a['generation']+1);self.assertEqual(out['bound_prs'][0]['generation'],a['generation']+1)
  reg=OR.SessionRegistry(self.db);self.assertEqual(reg._conn.execute("SELECT count(*) FROM events WHERE event='bind-pr-renew'").fetchone()[0],1);reg.close_db()

 def test_reopen_same_generation_cannot_revive_conflicting_pr(self):
  a=self.register('native-session-a');b=self.register('native-session-b')
  self.bind('native-session-a',a['generation'])
  OR.close_session(self.db,platform='testbed',verified_host='mac',profile_scope='default',native_session_id='native-session-a',reason='test')
  self.bind('native-session-b',b['generation'])
  reg=OR.SessionRegistry(self.db)
  letter,generation,_=reg.reopen(a['record_id']);reg.close_db()
  self.assertEqual(generation,a['generation'])
  self.assertEqual(OR.resolve_label(self.db,'testbed',letter)['bound_prs'],[])
  self.assertEqual(OR.resolve_label(self.db,'testbed',b['label'])['bound_prs'][0]['number'],537)
  with self.assertRaisesRegex(ValueError,'another live'):self.bind('native-session-a',generation)
 def test_reopen_invalidates_uncontested_binding_until_explicit_rebind(self):
  a=self.register('native-session-a');self.bind('native-session-a',a['generation'])
  OR.close_session(self.db,platform='testbed',verified_host='mac',profile_scope='default',native_session_id='native-session-a',reason='test')
  reg=OR.SessionRegistry(self.db);letter,generation,_=reg.reopen(a['record_id']);reg.close_db()
  self.assertEqual(OR.resolve_label(self.db,'testbed',letter)['bound_prs'],[])
  self.assertEqual(self.bind('native-session-a',generation)['outcome'],'bound')

 def test_raw_close_is_invalidated_on_reopen_but_live_noop_preserves(self):
  a=self.register('native-session-a');self.bind('native-session-a',a['generation'])
  reg=OR.SessionRegistry(self.db);reg.reopen(a['record_id'])
  self.assertTrue(OR.resolve_label(self.db,'testbed',a['label'])['bound_prs'])
  with reg._write_txn()as c:
   c.execute("UPDATE sessions SET closed_at='2026-11-21',state='closed' WHERE record_id=?",(a['record_id'],))
   c.execute("UPDATE alias_assignments SET retired_at='2026-11-21' WHERE record_id=?",(a['record_id'],))
  letter,g,_=reg.reopen(a['record_id']);reg.close_db()
  self.assertEqual(OR.resolve_label(self.db,'testbed',letter)['bound_prs'],[])

 def test_real_relabel_same_generation_invalidates_but_noop_preserves(self):
  a=self.register('native-session-a');parent=self.register('native-session-parent')
  self.bind('native-session-a',a['generation']);reg=OR.SessionRegistry(self.db)
  self.assertEqual(reg.relabel(a['record_id'])['outcome'],'unchanged')
  self.assertTrue(OR.resolve_label(self.db,'testbed',a['label'])['bound_prs'])
  changed=reg.relabel(a['record_id'],force=True,parent_session_id='native-session-parent');reg.close_db()
  self.assertNotEqual(changed['label'],a['label']);self.assertEqual(changed['generation'],a['generation'])
  self.assertEqual(OR.resolve_label(self.db,'testbed',changed['label'])['bound_prs'],[])
  self.assertEqual(self.bind('native-session-a',changed['generation'])['outcome'],'bound')
