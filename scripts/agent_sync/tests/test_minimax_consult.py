import unittest, tempfile, json, io, hashlib, sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from agent_sync.minimax_consult import *

def request(n): return hashlib.sha256(str(n).encode()).hexdigest()
def answer(**kw):
 d={'model':MODEL,'status':'completed','tools':None,'service_tier':None,'usage':{'input_tokens':10,'output_tokens':20,'total_tokens':30},'output':[{'type':'reasoning','status':'completed','summary':[]},{'type':'message','status':'completed','role':'assistant','content':[{'type':'output_text','text':'{"action":"none"}'}]}]};d.update(kw);return json.dumps(d).encode()
class Response(io.BytesIO):
 status=200
 def geturl(self):return URL
 def __exit__(self,*a): self.close()
class Opener:
 def __init__(self, data=None, error=None):self.calls=[];self.data=data or answer();self.error=error
 def open(self, req, timeout):
  self.calls.append(req)
  if self.error:raise self.error
  return Response(self.data)
class Tests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'budget.sqlite';self.now=1793990000
 def tearDown(self):self.tmp.cleanup()
 def test_durable_accounting_and_replay(self):
  ledger=SpendLedger(self.path)
  for n in range(14):ledger.reserve(request(n),self.now)
  ledger.db.close();ledger=SpendLedger(self.path)
  with self.assertRaisesRegex(ConsultUnavailable,'already'):ledger.reserve(request(0),self.now)
  rows=ledger.db.execute('select count(*),sum(nano) from spends').fetchone()
  self.assertEqual(rows,(14,14*RESERVE_NANO_USD))
 def test_concurrency(self):
  SpendLedger(self.path).db.close()
  def reserve(n):
   ledger=SpendLedger(self.path)
   try:ledger.reserve(request(n),self.now);return True
   except ConsultUnavailable:return False
   finally:ledger.db.close()
  with ThreadPoolExecutor(8) as pool:self.assertEqual(sum(pool.map(reserve,range(40))),40)
 def test_clock_and_day_boundary(self):
  ledger=SpendLedger(self.path);t=datetime(2026,11,6,23,59,tzinfo=ZONE).timestamp()
  for n in range(13):ledger.reserve(request(n),t)
  ledger.reserve(request(20),t+120)
  with self.assertRaisesRegex(ConsultUnavailable,'backward'):ledger.reserve(request(21),t-1)
  ledger.reserve(request(22),t+86401)
  days=[r[0] for r in ledger.db.execute('select distinct day from spends order by 1')]
  self.assertEqual(len(days),2)
 def test_request_is_single_toolless_bounded(self):
  op=Opener();ledger=SpendLedger(self.path);runner=MiniMaxConsult(ledger,lambda:'fixture',opener=op,clock=lambda:self.now)
  self.assertEqual(runner.run('untrusted inbox',request(1)),{'action':'none'})
  req=op.calls[0];payload=json.loads(req.data)
  self.assertEqual(set(payload),{'model','input','max_output_tokens','reasoning','stream'})
  self.assertEqual(payload['max_output_tokens'],4096)
  with self.assertRaises(ConsultUnavailable):runner.run('untrusted inbox',request(1))
  self.assertEqual(len(op.calls),1)
 def test_ambiguous_attempt_retained_no_retry(self):
  op=Opener(error=TimeoutError());ledger=SpendLedger(self.path);runner=MiniMaxConsult(ledger,lambda:'fixture',opener=op,clock=lambda:self.now)
  for _ in range(2):
   with self.assertRaises(ConsultUnavailable):runner.run('x',request(1))
  self.assertEqual(len(op.calls),1)
  self.assertEqual(ledger.db.execute('select count(*) from spends').fetchone()[0],1)
 def test_invalid_credential_no_dispatch(self):
  op=Opener();ledger=SpendLedger(self.path);runner=MiniMaxConsult(ledger,lambda:'',opener=op)
  with self.assertRaises(ConsultUnavailable):runner.run('x',request(1))
  self.assertEqual(op.calls,[])
 def test_reject_model_status_tools_usage_and_schema(self):
  for kw in [{'model':'other'},{'status':'incomplete'},{'tools':[{'type':'web_search'}]},{'service_tier':'priority'},{'usage':{}},{'usage':{'input_tokens':1,'output_tokens':5000,'total_tokens':5001}},{'output':[{'type':'function_call'}]}]:
   with self.subTest(kw=kw),self.assertRaises(ConsultUnavailable):parse_response(answer(**kw))
  for data in [b'{"x":1,"x":2}',b'{"x":NaN}',b'```json\n{}\n```']:
   with self.assertRaises(ConsultUnavailable):strict_json(data)
 def test_refuse_redirect(self):
  with self.assertRaises(ConsultUnavailable):NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid')
 def test_oversize_response_retains_budget(self):
  op=Opener(b'x'*(MAX_RESPONSE_BYTES+1));ledger=SpendLedger(self.path);runner=MiniMaxConsult(ledger,lambda:'fixture',opener=op,clock=lambda:self.now)
  with self.assertRaises(ConsultUnavailable):runner.run('x',request(1))
  self.assertEqual(ledger.db.execute('select count(*) from spends').fetchone()[0],1)
if __name__=='__main__':unittest.main()

class KeyResponse(Response):
 def __init__(self,data,url):super().__init__(json.dumps(data).encode());self.url=url
 def geturl(self):return self.url
class KeyOpener:
 def __init__(self, meta=None, secret=None):
  self.calls=[];self.meta=meta or {'workspace':'project','scopes':[{'environment':'prod','secretPath':'/'}],'permissions':['read'],'expiresAt':None};self.secret=secret or {'secret':{'secretKey':'MINIMAX_API_KEY','secretValue':'fixture-key-value'}}
 def open(self,req,timeout):
  self.calls.append(req.full_url)
  return KeyResponse(self.meta if req.full_url.endswith('/service-token') else self.secret,req.full_url)
class KeyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'token';self.path.write_text('st.fixture-token');self.path.chmod(0o600)
 def tearDown(self):self.tmp.cleanup()
 def test_exact_named_read_and_cache(self):
  op=KeyOpener();reader=ScopedKeyReader(str(self.path),'project',opener=op,clock=lambda:1793990000)
  self.assertEqual(reader(),'fixture-key-value');self.assertEqual(reader(),'fixture-key-value');self.assertEqual(len(op.calls),2)
  self.assertTrue(op.calls[1].startswith(ScopedKeyReader.ORIGIN+'/api/v3/secrets/raw/MINIMAX_API_KEY?'));self.assertNotIn('secrets/raw?',op.calls[1])
 def test_reject_write_wrong_scope_expired_before_key_read(self):
  for change in [{'permissions':['read','write']},{'scopes':[{'environment':'dev','secretPath':'/'}]},{'workspace':'other'},{'expiresAt':'2020-01-01T00:00:00Z'}]:
   op=KeyOpener();op.meta.update(change);reader=ScopedKeyReader(str(self.path),'project',opener=op,clock=lambda:1793990000)
   with self.assertRaises(ConsultUnavailable):reader()
   self.assertEqual(len(op.calls),1)
   with self.assertRaises(ConsultUnavailable):reader()
   self.assertEqual(len(op.calls),1)
 def test_reject_unsafe_file_and_key(self):
  op=KeyOpener();self.path.chmod(0o644)
  with self.assertRaises(ConsultUnavailable):ScopedKeyReader(str(self.path),'project',opener=op)()
  self.assertEqual(op.calls,[]);self.path.chmod(0o600)
  op.secret={'secret':{'secretKey':'OTHER','secretValue':'fixture-key-value'}}
  with self.assertRaises(ConsultUnavailable):ScopedKeyReader(str(self.path),'project',opener=op)()

def process_reserve(path,n,now):
 ledger=SpendLedger(path)
 try:ledger.reserve(request(n),now);return True
 except ConsultUnavailable:return False
 finally:ledger.db.close()
class DurabilityTests(unittest.TestCase):
 def test_independent_processes_share_ledger(self):
  from concurrent.futures import ProcessPoolExecutor
  with tempfile.TemporaryDirectory() as tmp:
   path=str(Path(tmp)/'budget.sqlite');SpendLedger(path).db.close()
   with ProcessPoolExecutor(4) as pool:
    jobs=[pool.submit(process_reserve,path,n,1793990000) for n in range(30)]
    self.assertEqual(sum(job.result() for job in jobs),30)
   ledger=SpendLedger(path)
   self.assertEqual(ledger.db.execute('select count(*) from spends').fetchone()[0],30)
 def test_corrupt_database_and_bad_clock_fail_closed(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'budget.sqlite';path.write_bytes(b'not a database');path.chmod(0o600)
   with self.assertRaises(sqlite3.DatabaseError):SpendLedger(path)
   path.unlink();ledger=SpendLedger(path)
   for now in [float('nan'),float('inf'),-1,True]:
    with self.assertRaises(ConsultUnavailable):ledger.reserve(request(0),now)
 def test_dst_fallback_keeps_single_day_accounting(self):
  with tempfile.TemporaryDirectory() as tmp:
   ledger=SpendLedger(Path(tmp)/'budget.sqlite')
   start=datetime(2026,11,1,1,30,tzinfo=ZONE,fold=0).timestamp()
   after=datetime(2026,11,1,1,30,tzinfo=ZONE,fold=1).timestamp()
   for n in range(13):ledger.reserve(request(n),start)
   ledger.reserve(request(20),after)
   days=[r[0] for r in ledger.db.execute('select distinct day from spends')]
   self.assertEqual(days,['2026-11-01'])

class ChargedDistinctionTests(unittest.TestCase):
 """A pre-charge failure keeps the row pending; a charged one settles it, never retried."""
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'budget.sqlite';self.now=1793990000
 def tearDown(self):self.tmp.cleanup()
 def test_duplicate_reservation_is_charged(self):
  ledger=SpendLedger(self.path);ledger.reserve(request(1),self.now)
  with self.assertRaises(ConsultCharged):ledger.reserve(request(1),self.now+1)
 def test_http_failure_after_reservation_is_charged(self):
  op=Opener(error=TimeoutError());ledger=SpendLedger(self.path)
  runner=MiniMaxConsult(ledger,lambda:'fixture',opener=op,clock=lambda:self.now)
  with self.assertRaises(ConsultCharged):runner.run('x',request(1))
 def test_credential_failure_is_not_charged(self):
  op=Opener();ledger=SpendLedger(self.path)
  runner=MiniMaxConsult(ledger,lambda:'',opener=op,clock=lambda:self.now)
  with self.assertRaises(ConsultUnavailable) as ctx:runner.run('x',request(1))
  self.assertNotIsInstance(ctx.exception,ConsultCharged)
  self.assertEqual(ledger.db.execute('select count(*) from spends').fetchone()[0],0)
 def test_invalid_reservation_key_is_not_charged(self):
  ledger=SpendLedger(self.path)
  with self.assertRaises(ConsultUnavailable) as ctx:ledger.reserve('not-a-key',self.now)
  self.assertNotIsInstance(ctx.exception,ConsultCharged)
  self.assertEqual(ledger.db.execute('select count(*) from spends').fetchone()[0],0)
 def test_ledger_write_failure_is_not_charged(self):
  class BadLedger:
   def reserve(self,key,now): raise sqlite3.OperationalError('database is locked')
  op=Opener();runner=MiniMaxConsult(BadLedger(),lambda:'fixture',opener=op,clock=lambda:self.now)
  with self.assertRaises(ConsultUnavailable) as ctx:runner.run('x',request(1))
  self.assertNotIsInstance(ctx.exception,ConsultCharged)

class ScopedPathTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'token';self.path.write_text('st.fixture-token');self.path.chmod(0o600)
 def tearDown(self):self.tmp.cleanup()
 def test_narrow_path_scope_accepted_and_enforced(self):
  meta={'workspace':'project','scopes':[{'environment':'prod','secretPath':'/bridge-minimax/'}],'permissions':['read'],'expiresAt':None}
  op=KeyOpener(meta=meta)
  reader=ScopedKeyReader(str(self.path),'project',secret_path='/bridge-minimax/',opener=op,clock=lambda:1793990000)
  self.assertEqual(reader(),'fixture-key-value')
  self.assertIn('secretPath=%2Fbridge-minimax%2F',op.calls[1])
 def test_root_scoped_token_rejected_when_narrow_path_required(self):
  op=KeyOpener()
  reader=ScopedKeyReader(str(self.path),'project',secret_path='/bridge-minimax/',opener=op,clock=lambda:1793990000)
  with self.assertRaises(ConsultUnavailable):reader()
 def test_binding_ready_is_local_only(self):
  op=KeyOpener()
  self.assertTrue(ScopedKeyReader(str(self.path),'project',opener=op).binding_ready())
  self.assertEqual(op.calls,[])
  self.assertFalse(ScopedKeyReader('','project',opener=op).binding_ready())
  self.assertFalse(ScopedKeyReader(str(self.path),'',opener=op).binding_ready())
  self.assertEqual(op.calls,[])
