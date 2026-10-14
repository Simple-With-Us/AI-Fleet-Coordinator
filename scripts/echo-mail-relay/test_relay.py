import unittest,tempfile,json,hmac,hashlib
from relay import Relay
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/db';self.env={'ECHO_LISTENER_HMAC_KEY':'listener-test','ECHO_ZULIP_WEBHOOK_TOKEN':'webhook-test','ECHO_BOT_EMAIL':'bot@example.test'};self.r=Relay(self.path,self.env,clock=lambda:1000)
 def tearDown(self):self.r.db.close();self.tmp.cleanup()
 def listener(self,mid=7):
  raw=json.dumps({'contract':'agent-sync-wake/1','seat':'ECHO','message_id':mid,'trigger_ids':[mid],'sent_at':1000}).encode();sig=hmac.new(b'listener-test',raw,hashlib.sha256).hexdigest();return raw,sig
 def native(self,mid=7):
  return json.dumps({'token':'webhook-test','bot_email':'bot@example.test','trigger':'mention','message':{'id':mid,'type':'stream','content':'hello','timestamp':999,'token':'DO NOT STORE'}}).encode()
 def test_separate_auth(self):
  raw,sig=self.listener()
  with self.assertRaises(PermissionError):self.r.accept('/listener/echo',raw,'webhook-test')
  with self.assertRaises(PermissionError):self.r.accept('/zulip/echo',raw,sig)
 def test_cross_transport_dedup_and_restart(self):
  self.r.accept('/listener/echo',*self.listener());self.r.accept('/zulip/echo',self.native());other=Relay(self.path,self.env);self.assertEqual(other.health()['pending'],1);other.db.close()
 def test_normalization_drops_token(self):
  self.r.accept('/zulip/echo',self.native());raw=self.r.db.execute('select body from outbox').fetchone()[0];self.assertNotIn('DO NOT STORE',raw)
 def test_fetch_full_message_and_mail(self):
  self.r.accept('/listener/echo',*self.listener());seen=[];self.r.read_message=lambda mid:{'id':mid,'content':'full'};self.r.send_email=lambda mid,msg:seen.append(msg) or 'mail';self.r.once();self.assertEqual(seen,[{'id':7,'content':'full'}]);self.assertEqual(self.r.health()['pending'],0)
 def test_failed_delivery_stays_pending(self):
  self.r.accept('/zulip/echo',self.native());self.r.send_email=lambda *a:(_ for _ in ()).throw(RuntimeError('secret'));self.r.once();self.assertEqual(self.r.health()['pending'],1);self.assertNotIn('secret',str(self.r.health()))
 def test_stale_and_oversize(self):
  raw,sig=self.listener();self.r.clock=lambda:2000
  with self.assertRaises(ValueError):self.r.accept('/listener/echo',raw,sig)
  with self.assertRaises(ValueError):self.r.accept('/zulip/echo',b'x'*300000)
if __name__=='__main__':unittest.main()
