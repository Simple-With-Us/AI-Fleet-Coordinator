import unittest,tempfile,json,hmac,hashlib
import relay as R
from relay import Relay
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/db';self.env={'ECHO_LISTENER_HMAC_KEY':'listener-test','ECHO_ZULIP_WEBHOOK_TOKEN':'webhook-test','ECHO_BOT_EMAIL':'bot@example.test','RELAY_DELIVERY_ENABLED':'true'};self.r=Relay(self.path,self.env,clock=lambda:1000)
 def tearDown(self):self.r.db.close();self.tmp.cleanup()
 def listener(self,mid=7):
  raw=json.dumps({'contract':R.WAKE_CONTRACT,'seat':'ECHO','message_id':mid,'trigger_ids':[mid],'sent_at':1000}).encode();sig=hmac.new(b'listener-test',raw,hashlib.sha256).hexdigest();return raw,sig
 def native(self,mid=7):
  return json.dumps({'token':'webhook-test','bot_email':'bot@example.test','trigger':'mention','message':{'id':mid,'type':'stream','content':'hello','timestamp':999,'token':'DO NOT STORE'}}).encode()
 def test_separate_auth(self):
  raw,sig=self.listener()
  with self.assertRaises(PermissionError):self.r.accept('/listener/echo',raw,'webhook-test')
  with self.assertRaises(PermissionError):self.r.accept('/zulip/echo',raw,sig)
 def test_cross_transport_dedup_and_restart(self):
  self.r.accept('/listener/echo',*self.listener());self.r.accept('/zulip/echo',self.native());other=Relay(self.path,self.env);self.assertEqual(other.health()['pending'],1);other.db.close()
 def test_normalization_drops_token(self):
  self.r.accept('/zulip/echo',self.native());raw=self.r.db.execute('select body from outbox_v2').fetchone()[0];self.assertNotIn('DO NOT STORE',raw)
 def test_fetch_full_message_and_mail(self):
  self.r.accept('/listener/echo',*self.listener());seen=[];self.r.read_message=lambda mid,seat:{'id':mid,'content':'full'};self.r.send_email=lambda mid,msg,seat:seen.append(msg) or 'mail';self.r.once();self.assertEqual(seen,[{'id':7,'content':'full'}]);self.assertEqual(self.r.health()['pending'],0)
 def test_failed_delivery_stays_pending(self):
  self.r.accept('/zulip/echo',self.native());self.r.send_email=lambda *a:(_ for _ in ()).throw(RuntimeError('secret'));self.r.once();self.assertEqual(self.r.health()['pending'],1);self.assertNotIn('secret',str(self.r.health()))
 def test_stale_and_oversize(self):
  raw,sig=self.listener();self.r.clock=lambda:2000
  with self.assertRaises(ValueError):self.r.accept('/listener/echo',raw,sig)
  with self.assertRaises(ValueError):self.r.accept('/zulip/echo',b'x'*300000)
 def test_disabled_does_not_attempt(self):
  self.env['RELAY_DELIVERY_ENABLED']='false';self.r.accept('/zulip/echo',self.native());self.assertFalse(self.r.once());self.assertEqual(self.r.db.execute('select attempts from outbox_v2').fetchone()[0],0)
 def test_readiness_checks_source_reads(self):
  self.env.pop('ECHO_ZULIP_WEBHOOK_TOKEN');self.env.update(RELAY_SMTP_USERNAME='test@example.test',RELAY_SMTP_PASSWORD='test');self.assertFalse(self.r.health()['ok']);self.env['ECHO_ZULIP_API_KEY']='test';self.assertTrue(self.r.health()['ok'])
 def test_rate_cap_is_durable(self):
  self.r.accept('/zulip/echo',self.native(7));self.r.accept('/zulip/echo',self.native(8));self.r.send_email=lambda *a:'mail';self.r.once();self.assertFalse(self.r.once());other=Relay(self.path,self.env,clock=lambda:1000);self.assertFalse(other.once());other.db.close()
 def test_seat_isolation(self):
  self.env.update(WISE_ZULIP_WEBHOOK_TOKEN='wise-test',WISE_BOT_EMAIL='wise@example.test')
  raw=self.native().decode().replace('webhook-test','wise-test').replace('bot@example.test','wise@example.test').encode()
  self.r.accept('/zulip/echo',self.native());self.r.accept('/zulip/wise',raw)
  self.assertEqual(self.r.health()['pending'],2)
  with self.assertRaises(PermissionError):self.r.accept('/zulip/wise',self.native())
  self.assertEqual(self.r.db.execute('select count(distinct seat) from outbox_v2').fetchone()[0],2)
 def test_current_contract_and_header_pinned(self):
  self.assertEqual(R.WAKE_CONTRACT,'simple-tools-wake/1');self.assertEqual(R.SIGNATURE_HEADER,'X-Simple-Tools-Signature')
 def test_listener_full_body_compat(self):
  body={'contract':R.WAKE_CONTRACT,'seat':'ECHO','wake_id':'w1-abc','message_id':7,'trigger_ids':[7,8],'dm':False,'channel':'Work-In-Progress','topic':'t','stream_id':642232,'dm_recipient_ids':[],'sender_user_id':123,'sender_full_name':'A Peer','is_bot':True,'owner':False,'excerpt':'scrubbed','reply_prefix':'[ECHO·wake] re=7','sent_at':1000}
  raw=json.dumps(body).encode();sig=hmac.new(b'listener-test',raw,hashlib.sha256).hexdigest()
  self.r.accept('/listener/echo',raw,sig)
  ids=[row[0] for row in self.r.db.execute('select id from outbox_v2 order by id').fetchall()]
  self.assertEqual(ids,[7,8])
  for row in self.r.db.execute('select body from outbox_v2').fetchall():self.assertEqual(json.loads(row[0]),{'fetch':True})
 def test_hmac_rejects_tampered_bytes_and_missing_signature(self):
  raw,sig=self.listener()
  with self.assertRaises(PermissionError):self.r.accept('/listener/echo',raw+b' ',sig)
  with self.assertRaises(PermissionError):self.r.accept('/listener/echo',raw,None)
  with self.assertRaises(PermissionError):self.r.accept('/listener/echo',raw,'')
 def test_replay_window_edges_and_types(self):
  for at,ok in ((700,True),(699,False),(1300,True),(1301,False)):
   raw,sig=self.listener();body=json.loads(raw);body['sent_at']=at;raw=json.dumps(body).encode();sig=hmac.new(b'listener-test',raw,hashlib.sha256).hexdigest()
   if ok:self.r.accept('/listener/echo',raw,sig)
   else:
    with self.assertRaises(ValueError):self.r.accept('/listener/echo',raw,sig)
  raw,sig=self.listener();body=json.loads(raw);body['sent_at']='1000';raw=json.dumps(body).encode();sig=hmac.new(b'listener-test',raw,hashlib.sha256).hexdigest()
  with self.assertRaises(ValueError):self.r.accept('/listener/echo',raw,sig)
 def test_trigger_ids_bounds_and_fallback(self):
  raw,sig=self.listener(mid=9);self.r.accept('/listener/echo',raw,sig)
  self.assertEqual(self.r.db.execute('select id from outbox_v2').fetchone()[0],9)
  raw,sig=self.listener();body=json.loads(raw);body['trigger_ids']=list(range(1,102));raw=json.dumps(body).encode();sig=hmac.new(b'listener-test',raw,hashlib.sha256).hexdigest()
  with self.assertRaises(ValueError):self.r.accept('/listener/echo',raw,sig)
 def test_native_form_encoded(self):
  import urllib.parse as up
  form=up.urlencode({'token':'webhook-test','bot_email':'bot@example.test','trigger':'direct_message','message':json.dumps({'id':11,'type':'private','content':'hi','timestamp':999})}).encode()
  self.r.accept('/zulip/echo',form);self.assertEqual(self.r.db.execute('select id from outbox_v2 where id=11').fetchone()[0],11)
if __name__=='__main__':unittest.main()

