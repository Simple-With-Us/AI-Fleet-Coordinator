import unittest, importlib.util
from uuid import UUID
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('loader',Path(__file__).resolve().parents[1] / 'server' / 'infisical_env.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
# Deterministic synthetic fixture, never a live project identifier.
PROJECT = str(UUID(int=1))
class Tests(unittest.TestCase):
 def env(self,**kw):return dict(INFISICAL_CLIENT_ID='offline',INFISICAL_CLIENT_SECRET='offline',INFISICAL_PROJECT_ID='primary',INFISICAL_LISTENER_READ_PROJECT_ID=PROJECT,MA_ROUTINE_KEY='stale'*12,**kw)
 def run_case(self,secondary):
  calls=[]
  def fetch(env,**kw):
   calls.append((env.copy(),kw.copy()))
   if env['INFISICAL_PROJECT_ID']==PROJECT:
    if isinstance(secondary,Exception):raise secondary
    return secondary
   return {'ZULIP_MA_EMAIL':'own@example.test','MA_ROUTINE_KEY':'primary'*12,'JET_ROUTINE_KEY':'jet'*15,'PATH':'untrusted'} if kw.get('path') is None else {}
  with patch.object(m,'server_codes',return_value={'MA','GB_FIXER','JET'}),patch.object(m,'login',return_value='same-token'),patch.object(m,'fetch',side_effect=fetch):out,line=m.build_env(self.env())
  return out,line,calls
 def test_isolated_native_secondary(self):
  out,line,calls=self.run_case({'MA_ROUTINE_KEY':'own'*15,'GB_FIXER_ROUTINE_KEY':'fixer'*10,'JET_ROUTINE_KEY':'foreign'*10,'PATH':'bad','ZULIP_MA_EMAIL':'foreign@example.test','GB_TRADER_ROUTINE_KEY':'notheld'*10})
  self.assertEqual(out['MA_ROUTINE_KEY'],'own'*15);self.assertEqual(out['JET_ROUTINE_KEY'],'jet'*15);self.assertEqual(out['ZULIP_MA_EMAIL'],'own@example.test');self.assertNotIn('GB_TRADER_ROUTINE_KEY',out);self.assertNotIn('PATH',out)
  second=calls[-1];self.assertEqual(second[0]['INFISICAL_ENVIRONMENT'],'prod');self.assertEqual(second[1]['path'],'/zulip');self.assertEqual(second[1]['token'],'same-token');self.assertEqual(calls[0][0]['INFISICAL_PROJECT_ID'],'primary');self.assertNotIn('own'*15,line)
  self.assertNotIn('INFISICAL_CLIENT_SECRET',out)
 def test_failure_no_stale_or_primary_fallback(self):
  out,line,_=self.run_case(m.LoadError('read listener keys: HTTP 403'));self.assertNotIn('MA_ROUTINE_KEY',out);self.assertEqual(out['JET_ROUTINE_KEY'],'jet'*15);self.assertIn('not loaded',line)
 def test_missing_short_reference_keys_not_loaded(self):
  out,_,_=self.run_case({'MA_ROUTINE_KEY':'${foreign.secret}'*4,'GB_FIXER_ROUTINE_KEY':'short'});self.assertNotIn('MA_ROUTINE_KEY',out);self.assertNotIn('GB_FIXER_ROUTINE_KEY',out)
 def test_invalid_project_never_fetches(self):
  with patch.object(m,'fetch') as f:
   keys,line=m.listener_read_keys({m.LISTENER_READ_PROJECT:'bad'}, {'MA'}, 'token');self.assertEqual(keys,{});f.assert_not_called()
 def test_optout_unchanged(self):
  with patch.object(m,'fetch') as f:self.assertEqual(m.listener_read_keys({}, {'MA'}, 'token'),({},''));f.assert_not_called()
 def test_strict_secondary_listing_rejects_duplicates_and_truncation(self):
  cases=[{'secrets':[{'secretKey':'MA_ROUTINE_KEY','secretValue':'a'*40}]*2},
         {'secrets':[],'totalCount':1},{'secrets':[{'secretKey':'MA_ROUTINE_KEY','secretValue':2}]}]
  for response in cases:
   with patch.object(m,'_request',return_value=response):
    with self.assertRaises(m.LoadError):m.fetch(self.env(),token='same',strict=True)
 def test_login_failure_drops_secondary_stale_but_preserves_main_env(self):
  with patch.object(m,'server_codes',return_value={'MA'}),patch.object(m,'login',side_effect=m.LoadError('login: HTTP 403')):
   out,line=m.build_env(dict(self.env(),JET_ROUTINE_KEY='keep'))
  self.assertNotIn('MA_ROUTINE_KEY',out);self.assertEqual(out['JET_ROUTINE_KEY'],'keep');self.assertNotIn('INFISICAL_CLIENT_SECRET',out)
 def test_no_eligible_server_seats_never_fetches_secondary(self):
  for codes in [set(), {'JET', 'ECHO', 'BF-ENGINEER'}]:
   with patch.object(m, 'fetch') as fetch:
    keys, line = m.listener_read_keys(self.env(), codes, 'same-token')
    self.assertEqual(keys, {})
    self.assertIn('no eligible server seats', line)
    fetch.assert_not_called()
if __name__=='__main__':unittest.main()
